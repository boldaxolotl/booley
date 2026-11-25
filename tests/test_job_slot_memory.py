"""Memory Reservations in the shared slot store (#3700, ADR 0087).

A memory claim holds while the reservations of all holders fit its budget.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from booley.runtime import job_slots
from booley.runtime.job_slots import (
    HOLDING,
    LOST,
    QUEUED,
    ROLE_BACKGROUND,
    ROLE_INTERACTIVE,
    SlotStore,
    TokenState,
)
from booley.runtime.timefmt import rfc3339_from_epoch
from tests.sharing_error_support import hold_opens
from tests.test_job_slots import (
    PROMOTION_GATE,
    FakeWorld,
    held_gate,
    interleave,
    is_lease_gate,
    make_store,
    spawn,
)

BUDGET_MB = 1000.0


@pytest.fixture()
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "slots"


def claim(
    reservation_mb: float | None = None,
    *,
    run_alone: bool = False,
    budget_mb: float = BUDGET_MB,
    wall_s: float | None = None,
    companion: bool = False,
):
    """A memory claim against the shared test budget."""
    return job_slots.MemoryClaim(
        budget_mb=budget_mb,
        reservation_mb=reservation_mb,
        run_alone=run_alone,
        wall_s=wall_s,
        companion=companion,
    )


def companion(reservation_mb: float, *, budget_mb: float = BUDGET_MB):
    """A Specialist's or coordinator's companion claim."""
    return claim(reservation_mb, budget_mb=budget_mb, companion=True)


def join(
    store: SlotStore,
    world: FakeWorld,
    pid: int,
    memory,
    *,
    role: str = ROLE_INTERACTIVE,
    job=None,
):
    """Start simulated process *pid* and submit its memory claim (with its *job* label)."""
    spawn(world, pid)
    return store.submit(job_slots.CLASS_MEMORY, pid=pid, memory=memory, role=role, job=job)


def seated(reservation_mb: float, cap: int, tool: str = "vivado"):
    """A claim that also holds one *tool* seat, judged against a Seat Cap of *cap*."""
    return job_slots.MemoryClaim(
        budget_mb=BUDGET_MB, reservation_mb=reservation_mb, seats=((tool, cap),)
    )


def stored(store: SlotStore, token: job_slots.SlotToken) -> job_slots.SlotToken:
    """*token*'s entry as the store reads it from disk now."""
    holders, waiters = store.snapshot(job_slots.CLASS_MEMORY)
    return next(t for t in holders + waiters if t.lease_id == token.lease_id)


def label(run_id: str, flow: str, target: str):
    """The label of a queue report's named Job."""
    return job_slots.JobLabel(run_id, flow, target)


def test_claims_hold_while_reservations_fit_the_budget(root, world):
    store = make_store(root, world)
    first = join(store, world, 101, claim(400))
    second = join(store, world, 102, claim(500))
    assert store.refresh(first).state == HOLDING
    assert store.refresh(second).state == HOLDING
    # A lease renewal rewrites the entry: it must keep the reservation.
    assert store.renew(first) and store.renew(second)
    third = join(store, world, 103, claim(200))
    assert store.refresh(third) == TokenState(QUEUED, position=0)
    store.release(first)
    assert store.refresh(third).state == HOLDING


def test_a_claim_that_does_not_fit_blocks_later_claims(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800))
    assert store.refresh(holder).state == HOLDING
    blocked = join(store, world, 102, claim(400))
    fits = join(store, world, 103, claim(100))
    assert store.refresh(blocked) == TokenState(QUEUED, position=0)
    assert store.refresh(fits) == TokenState(QUEUED, position=1)


def test_interactive_claim_overtakes_queued_background_claims(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800), role=ROLE_BACKGROUND)
    assert store.refresh(holder).state == HOLDING
    queued = join(store, world, 102, claim(400), role=ROLE_BACKGROUND)
    interactive = join(store, world, 103, claim(100))
    assert store.refresh(queued).state == QUEUED
    assert store.refresh(interactive).state == HOLDING
    assert store.refresh(holder).state == HOLDING


def test_run_alone_claim_waits_for_an_empty_class_and_holds_alone(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(100))
    assert store.refresh(holder).state == HOLDING
    alone = join(store, world, 102, claim(run_alone=True))
    assert store.refresh(alone).state == QUEUED
    store.release(holder)
    assert store.refresh(alone).state == HOLDING
    small = join(store, world, 103, claim(1))
    assert store.refresh(small).state == QUEUED
    store.release(alone)
    assert store.refresh(small).state == HOLDING


def test_dead_holder_is_reaped_and_its_reservation_freed(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800))
    waiter = join(store, world, 102, claim(400))
    assert store.refresh(holder).state == HOLDING
    assert store.refresh(waiter).state == QUEUED
    del world.alive[101]
    assert store.refresh(waiter).state == HOLDING
    assert not holder.path.exists()


def test_queued_claim_is_reported_and_withdrawn_by_pid(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800))
    waiter = join(store, world, 102, claim(400))
    assert store.refresh(holder).state == HOLDING
    assert store.state_for_pid(102) == TokenState(QUEUED, position=0)
    assert store.cancel_waiter(102) is True
    assert store.refresh(waiter).state == LOST


def test_a_long_memory_queue_refuses_no_claim(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800))
    assert store.refresh(holder).state == HOLDING
    waiters = [join(store, world, 102 + n, claim(400)) for n in range(12)]
    assert [store.refresh(waiter).state for waiter in waiters] == [QUEUED] * 12
    assert [store.refresh(waiter).position for waiter in waiters] == list(range(12))


def test_promotion_rereads_holders_inside_the_gate(root, world, monkeypatch):
    store = make_store(root, world)
    rival = make_store(root, world)
    head = join(store, world, 101, claim(600), role=ROLE_BACKGROUND)
    winners = []

    def rival_promotes():
        # Another process takes the budget between B's check and B's gate.
        rival_token = join(rival, world, 102, claim(600))
        winners.append(rival.refresh(rival_token))

    interleave(
        monkeypatch, "hardlink_to", lambda path: path.name == PROMOTION_GATE, rival_promotes
    )
    assert store.refresh(head).state == QUEUED
    assert winners == [TokenState(HOLDING)]
    assert sorted(path.name[0] for path in (root / "memory").glob("*.json")) == ["h", "w"]


def test_unreadable_holder_blocks_promotion(root, world, monkeypatch):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(100))
    assert store.refresh(holder).state == HOLDING
    waiter = join(store, world, 102, claim(100))
    held = {holder.path: 10**6}
    hold_opens(monkeypatch, held)
    assert store.refresh(waiter).state == QUEUED  # unknown reservation: fail closed
    held[holder.path] = 0
    assert store.refresh(waiter).state == HOLDING


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param(
            lambda store: store.submit(job_slots.CLASS_MEMORY, pid=1), id="memory-without-claim"
        ),
        pytest.param(lambda _store: claim(1, run_alone=True), id="reservation-and-run-alone"),
        pytest.param(lambda _store: claim(), id="neither-reservation-nor-run-alone"),
        pytest.param(lambda _store: claim(2000), id="reservation-above-budget"),
        pytest.param(
            lambda store: store.submit(job_slots.CLASS_MEMORY, pid=1, memory=claim(0)),
            id="new-zero-reservation",
        ),
        pytest.param(lambda _store: claim(-1), id="negative-reservation"),
        pytest.param(lambda _store: claim(run_alone=cast(Any, 1)), id="non-bool-run-alone"),
        pytest.param(
            lambda store: store.submit("warp", pid=1, memory=claim(1)), id="unknown-class"
        ),
        pytest.param(
            lambda _store: job_slots.MemoryClaim(budget_mb=10, run_alone=True, companion=True),
            id="companion-run-alone",
        ),
        *[
            pytest.param(
                lambda _store, cap=cap: seated(100, cast(Any, cap)), id=f"seat-cap-{cap!r}"
            )
            for cap in (0, -1, True, 1.5, "1")
        ],
        pytest.param(lambda _store: claim(1, budget_mb=float("nan")), id="non-finite-budget"),
        pytest.param(lambda _store: claim(1, wall_s=-1.0), id="negative-wall"),
        pytest.param(lambda _store: claim(1, wall_s=float("inf")), id="non-finite-wall"),
    ],
)
def test_claim_shape_is_validated(root, world, attempt):
    with pytest.raises(ValueError):
        attempt(make_store(root, world))


def test_zero_budget_admits_only_run_alone_claims(root, world):
    store = make_store(root, world)
    alone = join(store, world, 101, claim(run_alone=True, budget_mb=0.0))
    assert store.refresh(alone).state == HOLDING


def _memory_field(value):
    return lambda payload: payload.__setitem__("memory", value)


def _seats_field(value):
    return lambda payload: payload["memory"].__setitem__("seats", value)


def _job_field(value):
    return lambda payload: payload.__setitem__("job", value)


def _queue_reason(value):
    return lambda payload: payload.__setitem__("queue_reason", value)


# id: (claim, job label, edit of the entry, attribute read, value read back). A
# foreign or old writer's entry reads as no claim, no label or no reason, never an error.
MALFORMED_FIELDS = {
    "bad-budget": (
        claim(100),
        None,
        _memory_field({"budget_mb": "lots", "reservation_mb": 1, "run_alone": False}),
        "memory",
        None,
    ),
    "missing-budget": (claim(100), None, _memory_field({"reservation_mb": 1}), "memory", None),
    "unconvertible-budget": (
        claim(100),
        None,
        _memory_field({"budget_mb": 10**400, "reservation_mb": 1, "run_alone": False}),
        "memory",
        None,
    ),
    "bad-wall": (
        claim(100),
        None,
        _memory_field(
            {"budget_mb": 10, "reservation_mb": 1, "run_alone": False, "wall_s": "slow"}
        ),
        "memory",
        None,
    ),
    "not-a-table": (claim(100), None, _memory_field("junk"), "memory", None),
    **{
        f"seats-{value!r}": (seated(100, 2), None, _seats_field(value), "memory", None)
        for value in ({"vivado": 0}, {"vivado": True}, {"vivado": "two"}, ["vivado"])
    },
    "written-before-seats": (
        seated(100, 2),
        None,
        lambda payload: payload["memory"].pop("seats"),
        "memory",
        claim(100),
    ),
    **{
        f"queue-reason-{value!r}": (claim(100), None, _queue_reason(value), "queue_reason", None)
        for value in (7, ["x"], {"a": 1}, True)
    },
    "label-not-a-table": (claim(100), label("r", "sim", "a"), _job_field("run-1"), "job", None),
    "label-bad-run-id": (
        claim(100),
        label("r", "sim", "a"),
        _job_field({"run_id": 1, "flow": "sim", "target": "a"}),
        "job",
        None,
    ),
    "label-missing-fields": (
        claim(100),
        label("r", "sim", "a"),
        _job_field({"run_id": "r"}),
        "job",
        None,
    ),
    "label-bad-work-item": (
        claim(100),
        label("r", "sim", "a"),
        _job_field({"run_id": "r", "flow": "sim", "target": "a", "work_item": "yes"}),
        "job",
        None,
    ),
}


@pytest.mark.parametrize("row", sorted(MALFORMED_FIELDS))
def test_a_malformed_entry_field_reads_as_absent(root, world, row):
    memory, job, edit, attribute, expected = MALFORMED_FIELDS[row]
    store = make_store(root, world)
    holder = join(store, world, 101, memory, job=job)
    assert store.refresh(holder).state == HOLDING
    payload = json.loads(holder.path.read_text(encoding="utf-8"))
    edit(payload)
    holder.path.write_text(json.dumps(payload), encoding="utf-8")

    assert getattr(stored(store, holder), attribute) == expected
    if attribute == "memory" and expected is None:
        waiter = join(store, world, 102, claim(100))
        assert store.refresh(waiter).state == QUEUED  # a holder with no readable claim blocks


def test_only_the_first_waiter_promotes(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(500))
    assert store.refresh(holder).state == HOLDING
    first = join(store, world, 102, claim(400))
    second = join(store, world, 103, claim(100))
    # Both fit, but the later one waits for the claim ranked ahead of it.
    assert store.refresh(second).state == QUEUED
    assert store.refresh(first).state == HOLDING
    assert store.refresh(second).state == HOLDING


def test_unreadable_waiter_blocks_the_claim_behind_it(root, world, monkeypatch):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(100))
    assert store.refresh(holder).state == HOLDING
    unreadable = join(store, world, 102, claim(100))
    behind = join(store, world, 103, claim(100))
    hold_opens(monkeypatch, {unreadable.path: 10**6})
    assert store.refresh(behind) == TokenState(
        QUEUED, position=1
    )  # blocked: its waiter is unknown


def test_vanished_holder_does_not_hold_back_a_claim_that_fits(root, world, monkeypatch):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(1000))
    assert store.refresh(holder).state == HOLDING
    # private: the holder must vanish between the ranking's listing and its read; reap reads the
    # same entry first, so no file-level hook isolates that window.
    real_read = store._read_memory_entry
    released = False

    def read_after_release(path):
        # The holder releases between the listing and the read of its entry.
        nonlocal released
        if path == holder.path and not released:
            released = True
            store.release(holder)
        return real_read(path)

    monkeypatch.setattr(store, "_read_memory_entry", read_after_release)
    assert store.refresh(join(store, world, 102, claim(100))).state == HOLDING


# --- Seat Caps for licensed EDA tools (#3244, ADR 0087) ------------------------------


def test_seat_cap_queues_a_claim_that_fits_memory_while_the_seats_are_taken(root, world):
    store = make_store(root, world)
    first = join(store, world, 101, seated(100, 1))
    second = join(store, world, 102, seated(100, 1))
    other_tool = join(store, world, 103, claim(100))
    assert store.refresh(first).state == HOLDING
    # Memory fits (200 of 1,000 MiB) but the only vivado seat is held.
    assert store.refresh(second) == TokenState(QUEUED, position=0)
    # The seat-blocked claim blocks the claims behind it, as a memory-blocked one does.
    assert store.refresh(other_tool) == TokenState(QUEUED, position=1)
    store.release(first)
    assert store.refresh(second).state == HOLDING
    assert store.refresh(other_tool).state == HOLDING


def test_seat_cap_of_two_holds_two_claims_together(root, world):
    store = make_store(root, world)
    first = join(store, world, 101, seated(100, 2))
    second = join(store, world, 102, seated(100, 2))
    third = join(store, world, 103, seated(100, 2))
    assert store.refresh(first).state == HOLDING
    assert store.refresh(second).state == HOLDING
    assert store.refresh(third) == TokenState(QUEUED, position=0)
    # A lease renewal rewrites the entry: it must keep the seat.
    assert store.renew(first) and store.renew(second)
    assert store.refresh(third) == TokenState(QUEUED, position=0)
    store.release(second)
    assert store.refresh(third).state == HOLDING


def test_seat_blocked_front_starts_when_the_seat_holder_finishes_so_a_short_claim_jumps(
    root, world
):
    store = make_store(root, world)
    holder = job_slots.MemoryClaim(
        budget_mb=BUDGET_MB, reservation_mb=100, wall_s=600.0, seats=(("vivado", 1),)
    )
    hold(store, world, 101, holder)
    front = join(store, world, 102, seated(100, 1))  # memory fits; only the seat blocks it
    short = join(store, world, 103, claim(100, wall_s=60.0))  # needs no seat, ends first
    long = join(store, world, 104, claim(100, wall_s=900.0))  # outlasts the seat holder
    assert store.refresh(front) == TokenState(QUEUED, position=0)
    assert store.refresh(long).state == QUEUED
    assert store.refresh(short).state == HOLDING


def _on_disk_reservation(token) -> float | None:
    """The reservation the token's entry file carries right now."""
    payload = json.loads(token.path.read_text(encoding="utf-8"))
    return payload["memory"]["reservation_mb"]


def test_shrunk_holder_admits_a_waiter_that_fits_only_after_the_shrink(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800))
    assert store.refresh(holder).state == HOLDING
    waiter = join(store, world, 102, claim(400))
    assert store.refresh(waiter) == TokenState(QUEUED, position=0)
    assert store.shrink_reservation(holder, 300)
    assert holder.memory == claim(300)
    assert _on_disk_reservation(holder) == 300
    assert store.refresh(waiter).state == HOLDING
    # The shrunk claim survives a lease renewal.
    assert store.renew(holder)
    assert _on_disk_reservation(holder) == 300


def test_shrink_never_grows_a_reservation(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(500))
    assert store.refresh(holder).state == HOLDING
    before = holder.path.read_bytes()
    for value in (500, 700, -1):
        assert not store.shrink_reservation(holder, value)
    assert holder.path.read_bytes() == before
    assert holder.memory == claim(500)
    # A lease recovery has taken the entry: the holder may no longer rewrite it.
    on_disk = json.loads(before)
    on_disk["lease_state"] = job_slots.LEASE_CANCELLING
    holder.path.write_text(json.dumps(on_disk) + "\n", encoding="utf-8")
    taken_bytes = holder.path.read_bytes()
    assert not store.shrink_reservation(holder, 100)
    assert holder.path.read_bytes() == taken_bytes


def test_shrink_refuses_a_run_alone_or_vanished_entry(root, world):
    store = make_store(root, world)
    alone = join(store, world, 101, claim(run_alone=True))
    assert store.refresh(alone).state == HOLDING
    assert not store.shrink_reservation(alone, 100)
    store.release(alone)
    assert not store.shrink_reservation(alone, 100)


def test_holder_drops_to_zero_from_a_reservation_or_run_alone(root, world):
    store = make_store(root, world)
    sized = join(store, world, 101, claim(500))
    assert store.refresh(sized).state == HOLDING
    assert store.shrink_reservation(sized, 0)
    assert _on_disk_reservation(sized) == 0
    assert not store.shrink_reservation(sized, 0)  # already zero
    assert not store.shrink_reservation(sized, 100)  # never grows back
    assert store.renew(sized)
    assert _on_disk_reservation(sized) == 0
    store.release(sized)
    alone = join(store, world, 102, claim(run_alone=True, wall_s=30.0))
    assert store.refresh(alone).state == HOLDING
    assert not store.shrink_reservation(alone, 100)
    assert store.shrink_reservation(alone, 0)
    on_disk = json.loads(alone.path.read_text(encoding="utf-8"))["memory"]
    assert (on_disk["reservation_mb"], on_disk["run_alone"], on_disk["wall_s"]) == (0, False, 30.0)
    assert on_disk["budget_mb"] == BUDGET_MB


def test_zero_holder_blocks_no_claim(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(500))
    assert store.refresh(holder).state == HOLDING
    assert store.shrink_reservation(holder, 0)
    # A full budget, then a run-alone claim, both start beside the zero holder.
    full = join(store, world, 102, claim(1000))
    assert store.refresh(full).state == HOLDING
    store.release(full)
    alone = join(store, world, 103, claim(run_alone=True))
    assert store.refresh(alone).state == HOLDING
    assert holder.path.exists()  # the zero holder keeps its lease
    with pytest.raises(ValueError, match="must reserve memory or run alone"):
        store.submit(job_slots.CLASS_MEMORY, pid=1, memory=claim(0))


def test_a_requeued_claim_queues_behind_a_long_queue(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(run_alone=True))
    assert store.refresh(holder).state == HOLDING
    blocked = [join(store, world, 110 + n, claim(run_alone=True)) for n in range(12)]
    spawn(world, 102)
    requeued = store.submit(
        job_slots.CLASS_MEMORY,
        pid=102,
        memory=claim(run_alone=True),
        queue_reason=REQUEUE_REASON,
    )
    assert store.state_for_pid(102) == TokenState(QUEUED, position=12, reason=REQUEUE_REASON)
    store.release(holder)
    for token in blocked:
        assert store.refresh(token).state == HOLDING
        store.release(token)
    assert store.refresh(requeued).state == HOLDING
    assert store.state_for_pid(102) == TokenState(HOLDING, reason=REQUEUE_REASON)


def test_shrink_keeps_the_claims_seats_and_wall_clock_estimate(root, world):
    """A shrink lowers only the reservation: the seat stays held and the estimate stays."""
    store = make_store(root, world)
    holder = join(
        store,
        world,
        101,
        job_slots.MemoryClaim(
            budget_mb=BUDGET_MB, reservation_mb=800, seats=(("vivado", 1),), wall_s=60.0
        ),
    )
    assert store.refresh(holder).state == HOLDING
    waiter = join(store, world, 102, seated(100, 1))
    assert store.shrink_reservation(holder, 300)
    on_disk = json.loads(holder.path.read_text(encoding="utf-8"))["memory"]
    assert on_disk["reservation_mb"] == 300
    assert on_disk["seats"] == {"vivado": 1}
    assert on_disk["wall_s"] == 60.0
    # Memory now fits the waiter, but the shrunk holder still owns the only seat.
    assert store.refresh(waiter) == TokenState(QUEUED, position=0)
    store.release(holder)
    assert store.refresh(waiter).state == HOLDING


# ------------------------------------------------------------- jumping ahead


def hold(store: SlotStore, world: FakeWorld, pid: int, memory) -> job_slots.SlotToken:
    """Submit *memory* as process *pid* and promote it to a holder."""
    token = join(store, world, pid, memory)
    assert store.refresh(token).state == HOLDING
    return token


def start_age(token: job_slots.SlotToken, world: FakeWorld, seconds: float) -> None:
    """Rewrite a holder's promotion stamp to *seconds* before now (the clock stays put)."""
    payload = json.loads(token.path.read_text(encoding="utf-8"))
    payload["promoted_at"] = rfc3339_from_epoch(world.clock - seconds)
    token.path.write_text(json.dumps(payload), encoding="utf-8")


def test_jumpers_start_in_queue_order(root, world):
    store = make_store(root, world)
    hold(store, world, 101, claim(800, wall_s=600.0))
    join(store, world, 102, claim(400))
    first = join(store, world, 103, claim(50, wall_s=60.0))
    second = join(store, world, 104, claim(50, wall_s=60.0))
    # Both may jump, but the later one waits for the one ranked ahead of it.
    assert store.refresh(second).state == QUEUED
    assert store.refresh(first).state == HOLDING
    assert store.refresh(second).state == HOLDING


JUMP_CASES = {
    # A frees its 800 MiB at 10 min; the front (400) fits then, not now.
    "short-starts": (
        [(800, 600.0)],
        {"reservation_mb": 400},
        {"reservation_mb": 100, "wall_s": 180.0},
        HOLDING,
    ),
    "long-waits": (
        [(800, 600.0)],
        {"reservation_mb": 400},
        {"reservation_mb": 100, "wall_s": 720.0},
        QUEUED,
    ),
    # A frees 300 MiB at 5 min, B 500 MiB at 10 min; the front (400) fits after A.
    "early": (
        [(300, 300.0), (500, 600.0)],
        {"reservation_mb": 400},
        {"reservation_mb": 100, "wall_s": 180.0},
        HOLDING,
    ),
    "late": (
        [(300, 300.0), (500, 600.0)],
        {"reservation_mb": 400},
        {"reservation_mb": 100, "wall_s": 420.0},
        QUEUED,
    ),
    "finishes-as-the-front-starts": (
        [(300, 300.0), (500, 600.0)],
        {"reservation_mb": 400},
        {"reservation_mb": 100, "wall_s": 300.0},
        QUEUED,
    ),
    # A run-alone front waits for the latest finish (10 min).
    "run-alone-front-early": (
        [(300, 300.0), (500, 600.0)],
        {"run_alone": True},
        {"reservation_mb": 100, "wall_s": 420.0},
        HOLDING,
    ),
    "run-alone-front-late": (
        [(300, 300.0), (500, 600.0)],
        {"run_alone": True},
        {"reservation_mb": 100, "wall_s": 720.0},
        QUEUED,
    ),
    # The front needs A, which has no wall-clock estimate: its start is unknown.
    "front-needs-an-unknown-finish": (
        [(500, None), (300, 300.0)],
        {"reservation_mb": 600},
        {"reservation_mb": 100, "wall_s": 180.0},
        QUEUED,
    ),
    "front-needs-only-a-known-finish": (
        [(500, None), (300, 300.0)],
        {"reservation_mb": 400},
        {"reservation_mb": 100, "wall_s": 180.0},
        HOLDING,
    ),
    # Short enough to jump, but it does not fit beside the holders now.
    "short-but-does-not-fit": (
        [(300, 300.0), (500, 600.0)],
        {"reservation_mb": 400},
        {"reservation_mb": 300, "wall_s": 100.0},
        QUEUED,
    ),
    # The front's own 1000 MiB budget applies, not the jumper's 750 MiB one.
    "front-judged-by-its-own-budget": (
        [(300, 300.0), (200, 600.0)],
        {"reservation_mb": 600},
        {"reservation_mb": 250, "wall_s": 420.0, "budget_mb": 750.0},
        QUEUED,
    ),
}


@pytest.mark.parametrize(
    ("holders", "front", "jumper", "state"), JUMP_CASES.values(), ids=list(JUMP_CASES)
)
def test_jump_never_delays_the_front_claim(root, world, holders, front, jumper, state):
    store = make_store(root, world)
    for pid, (reservation_mb, wall_s) in enumerate(holders, start=101):
        hold(store, world, pid, claim(reservation_mb, wall_s=wall_s))
    assert store.refresh(join(store, world, 201, claim(**front))) == TokenState(QUEUED, position=0)
    behind = TokenState(HOLDING) if state == HOLDING else TokenState(QUEUED, position=1)
    assert store.refresh(join(store, world, 202, claim(**jumper))) == behind


def test_jump_past_a_holder_overdue_by_its_estimate_waits(root, world):
    store = make_store(root, world)
    overdue = hold(store, world, 101, claim(500, wall_s=300.0))
    hold(store, world, 102, claim(300, wall_s=600.0))
    start_age(overdue, world, 900.0)  # estimated to be done 10 min ago
    join(store, world, 201, claim(400))
    assert store.refresh(join(store, world, 202, claim(100, wall_s=180.0))).state == QUEUED


def test_unreadable_waiter_behind_the_front_ends_jumping(root, world, monkeypatch):
    store = make_store(root, world)
    hold(store, world, 101, claim(800, wall_s=600.0))
    join(store, world, 102, claim(400))
    unreadable = join(store, world, 103, claim(50, wall_s=60.0))
    behind = join(store, world, 104, claim(100, wall_s=60.0))
    held = {unreadable.path: 10**6}
    hold_opens(monkeypatch, held)
    assert store.refresh(behind).state == QUEUED
    held[unreadable.path] = 0
    assert store.refresh(behind).state == QUEUED  # the claim ahead of it still has to start


def test_wall_clock_survives_the_entry_file(root, world):
    store = make_store(root, world)
    token = hold(store, world, 101, claim(100, wall_s=42.5))
    assert store.renew(token)
    assert stored(store, token).memory == claim(100, wall_s=42.5)


def test_front_that_can_start_now_under_its_own_budget_is_not_jumped(root, world):
    store = make_store(root, world)
    hold(store, world, 101, claim(500, wall_s=600.0))
    front = join(store, world, 102, claim(400))  # fits its own 1000 MiB budget, not 800 MiB
    jumper = join(store, world, 103, claim(50, wall_s=60.0, budget_mb=800.0))
    assert store.refresh(jumper).state == QUEUED  # the front starts first, not later
    assert store.refresh(front).state == HOLDING


REQUEUE_REASON = "requeued after out-of-memory"


def test_requeued_claim_reports_its_queue_reason(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800))
    spawn(world, 102)
    waiter = store.submit(
        job_slots.CLASS_MEMORY,
        pid=102,
        memory=claim(400),
        role=ROLE_INTERACTIVE,
        queue_reason=REQUEUE_REASON,
    )
    # Queued: the reason is readable from the store that wrote it and from a fresh one.
    assert store.refresh(holder).state == HOLDING
    expected = TokenState(QUEUED, position=0, reason=REQUEUE_REASON)
    assert store.state_for_pid(102) == expected
    assert make_store(root, world).state_for_pid(102) == expected
    # The reason lives in the entry file itself.
    assert json.loads(waiter.path.read_text(encoding="utf-8"))["queue_reason"] == REQUEUE_REASON

    # Promoted: the holder entry keeps carrying the reason.
    store.release(holder)
    assert store.refresh(waiter).state == HOLDING
    held = TokenState(HOLDING, reason=REQUEUE_REASON)
    assert store.state_for_pid(102) == held
    assert make_store(root, world).state_for_pid(102) == held
    store.release(waiter)

    # A first claim writes no key and reads no reason.
    plain = join(store, world, 103, claim(100))
    assert "queue_reason" not in json.loads(plain.path.read_text(encoding="utf-8"))
    state = store.state_for_pid(103)
    assert state is not None and state.reason is None


def test_reason_of_a_later_holder_entry_of_one_pid_is_reported(root, world):
    store = make_store(root, world)
    spawn(world, 101)
    # A Simulation Record's work-item claims share one PID; only the later one is a requeue.
    first = store.submit(job_slots.CLASS_MEMORY, pid=101, memory=claim(100), role=ROLE_INTERACTIVE)
    second = store.submit(
        job_slots.CLASS_MEMORY,
        pid=101,
        memory=claim(100),
        role=ROLE_INTERACTIVE,
        queue_reason=REQUEUE_REASON,
    )
    # Promotion happens on refresh; both reservations fit the budget.
    assert store.refresh(first).state == HOLDING
    assert store.refresh(second).state == HOLDING
    assert store.state_for_pid(101) == TokenState(HOLDING, reason=REQUEUE_REASON)


@pytest.mark.parametrize("foreign", [7, ["x"], {"a": 1}, True])
def test_non_string_queue_reason_reads_as_none(root, world, foreign):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800))
    waiter = join(store, world, 102, claim(400))
    # Rewrite the waiter's entry with a foreign-typed reason, as another writer might.
    payload = json.loads(waiter.path.read_text(encoding="utf-8"))
    payload["queue_reason"] = foreign
    waiter.path.write_text(json.dumps(payload), encoding="utf-8")
    state = store.state_for_pid(102)
    assert state is not None and state.state == QUEUED and state.reason is None
    assert store.refresh(holder).state == HOLDING


# --- queue reports: what a queued claim waits for (#3245) ----------------------------


def test_queued_claim_reports_its_need_free_memory_position_and_largest_holders(root, world):
    job_label = label("run-1", "sim", "a")
    store = make_store(root, world)
    small = join(store, world, 101, claim(200), job=job_label)
    large = join(store, world, 102, claim(500), job=label("run-2", "synth", "b"))
    waiting = join(store, world, 103, claim(400))
    behind = join(store, world, 104, claim(100))
    assert store.refresh(small).state == HOLDING and store.refresh(large).state == HOLDING
    assert store.refresh(waiting) == TokenState(QUEUED, position=0)
    assert store.refresh(behind) == TokenState(QUEUED, position=1)

    wait = store.memory_wait(103)

    assert wait is not None
    assert wait.position == 1
    assert wait.describe() == (
        "needs 400 MiB; 300 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-2 (synth, b) 500 MiB, run-1 (sim, a) 200 MiB"
    )
    behind_wait = store.memory_wait(104)
    assert behind_wait is not None and behind_wait.position == 2
    # Holders and non-waiters have no wait to report.
    assert store.memory_wait(101) is None
    assert store.memory_wait(999) is None


def test_waiters_being_admitted_at_a_release_count_as_holders(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(800))
    assert store.refresh(holder).state == HOLDING
    for pid, size, name in ((102, 700, "a"), (103, 50, "b"), (104, 300, "c"), (105, 100, "d")):
        join(store, world, pid, claim(size), job=label(f"run-{name}", "sim", name))
    store.release(holder)  # no waiter refreshed yet: A and B are about to be admitted

    second = store.memory_wait(103)
    third = store.memory_wait(104)
    fourth = store.memory_wait(105)

    # B starts right after A: position 1, waiting only on A's place in queue order.
    assert second is not None and second.position == 1
    assert second.describe() == (
        "waits in queue order behind run-a (sim, a); needs 50 MiB; "
        "300 MiB free of the 1,000 MiB Memory Budget; largest holders: run-a (sim, a) 700 MiB"
    )

    assert third is not None and third.position == 1
    assert third.describe() == (
        "needs 300 MiB; 250 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-a (sim, a) 700 MiB, run-b (sim, b) 50 MiB"
    )
    assert fourth is not None and fourth.position == 2
    assert fourth.describe() == (
        "waits in queue order behind run-c (sim, c); needs 100 MiB; "
        "250 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-a (sim, a) 700 MiB, run-b (sim, b) 50 MiB"
    )


def test_queued_run_alone_claim_says_it_waits_to_run_alone(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(500), job=label("run-1", "sim", "a"))
    alone = join(store, world, 102, claim(run_alone=True))
    unsized = join(store, world, 103, claim(run_alone=True, budget_mb=0.0))
    unmeasured = join(
        store,
        world,
        104,
        job_slots.MemoryClaim(
            budget_mb=BUDGET_MB, run_alone=True, unmeasured="synth on Target chip"
        ),
    )
    assert store.refresh(holder).state == HOLDING
    assert store.refresh(alone).state == QUEUED and store.refresh(unsized).state == QUEUED
    assert store.refresh(unmeasured).state == QUEUED
    spawn(world, 105)
    requeued = store.submit(
        job_slots.CLASS_MEMORY,
        pid=105,
        memory=claim(run_alone=True),
        role=ROLE_INTERACTIVE,
        queue_reason=REQUEUE_REASON,
    )
    assert store.refresh(requeued).state == QUEUED

    wait = store.memory_wait(102)
    no_budget = store.memory_wait(103)
    no_measurement = store.memory_wait(104)
    oom_requeue = store.memory_wait(105)

    assert wait is not None and no_budget is not None and no_measurement is not None
    assert oom_requeue is not None
    assert no_measurement.describe() == (
        "waits to run alone (no memory measurement yet for synth on Target chip); "
        "waits in queue order behind pid 102; largest holders: run-1 (sim, a) 500 MiB"
    )
    assert wait.describe() == (
        "waits to run alone (memory estimate unknown or above the Memory Budget); "
        "largest holders: run-1 (sim, a) 500 MiB"
    )
    assert no_budget.describe() == (
        "waits to run alone (Memory Budget unavailable); "
        "waits in queue order behind pid 102; largest holders: run-1 (sim, a) 500 MiB"
    )
    assert oom_requeue.describe() == (
        "waits to run alone (requeued after out-of-memory); "
        "waits in queue order behind pid 102; largest holders: run-1 (sim, a) 500 MiB"
    )


def test_seat_blocked_claim_names_the_tool_it_waits_for(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, seated(100, 1), job=label("run-1", "fpga", "a"))
    waiting = join(store, world, 102, seated(100, 1))
    behind = join(store, world, 103, seated(950, 1))
    assert store.refresh(holder).state == HOLDING
    assert store.refresh(waiting).state == QUEUED

    wait = store.memory_wait(102)
    behind_wait = store.memory_wait(103)

    assert wait is not None and behind_wait is not None
    assert store.refresh(behind).state == QUEUED
    assert wait.describe() == (
        "waits for a vivado seat (1 of 1 held); "
        "needs 100 MiB; 900 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-1 (fpga, a) 100 MiB"
    )
    assert behind_wait.describe() == (
        "needs 950 MiB; 900 MiB free of the 1,000 MiB Memory Budget; "
        "waits for a vivado seat (1 of 1 held); waits in queue order behind pid 102; "
        "largest holders: run-1 (fpga, a) 100 MiB"
    )


def test_run_alone_holder_leaves_nothing_free_and_is_named_whole_budget(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(run_alone=True), job=label("", "lint", ""))
    waiting = join(store, world, 102, claim(100))
    assert store.refresh(holder).state == HOLDING

    wait = store.memory_wait(102)

    assert wait is not None
    assert wait.describe() == (
        "needs 100 MiB; 0 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: pid 101 (lint) whole budget"
    )
    assert store.refresh(waiting).state == QUEUED


def test_unlabelled_holder_is_named_by_its_pid_and_at_most_three_are_listed(root, world):
    store = make_store(root, world)
    for pid, size in ((101, 100), (102, 400), (103, 300), (104, 150)):
        assert store.refresh(join(store, world, pid, claim(size))).state == HOLDING
    join(store, world, 105, claim(400))

    wait = store.memory_wait(105)

    assert wait is not None
    assert wait.describe() == (
        "needs 400 MiB; 50 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: pid 102 400 MiB, pid 103 300 MiB, pid 104 150 MiB"
    )


def test_holders_that_reserve_nothing_are_not_named_largest(root, world):
    store = make_store(root, world)
    flow = join(store, world, 101, claim(300), job=label("run-3", "sim", "a"))
    assert store.refresh(flow).state == HOLDING
    assert store.shrink_reservation(flow, 0)
    coordinator = join(store, world, 102, companion(0), job=label("run-3", "sim", "b"))
    big = join(store, world, 103, claim(600), job=label("run-3", "sim", "c"))
    join(store, world, 104, claim(500))
    assert store.refresh(coordinator).state == HOLDING and store.refresh(big).state == HOLDING

    wait = store.memory_wait(104)

    assert wait is not None
    assert wait.describe() == (
        "needs 500 MiB; 400 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-3 (sim, c) 600 MiB"
    )


def test_job_label_survives_promotion(root, world):
    store = make_store(root, world)
    token = join(store, world, 101, claim(100), job=label("run-1", "sim", "a"))
    assert store.refresh(token).state == HOLDING
    (holders, _waiters) = store.snapshot(job_slots.CLASS_MEMORY)
    assert holders[0].job == label("run-1", "sim", "a")

    item = join(
        store,
        world,
        102,
        claim(100),
        job=label("run-1", "sim", "a, b").for_work_item("a", ["t1", "t2", "t3", "t4"]),
    )
    assert store.refresh(item).state == HOLDING
    (holders, _waiters) = store.snapshot(job_slots.CLASS_MEMORY)
    assert holders[1].job == job_slots.JobLabel("run-1", "sim", "a", "t1,t2,t3,+1", True)
    assert job_slots.JobLabel.from_payload(holders[1].job.to_payload()) == holders[1].job
    waiting = join(store, world, 103, claim(900))
    wait = store.memory_wait(103)
    assert store.refresh(waiting).state == QUEUED
    assert wait is not None and "run-1 (sim, a, t1,t2,t3,+1) 100 MiB" in wait.describe()


SPECIALIST_MB = 452.5


def test_a_run_alone_claim_starts_beside_companion_holders(root, world):
    store = make_store(root, world)
    specialist = join(store, world, 101, companion(SPECIALIST_MB))
    coordinator = join(store, world, 102, companion(0))
    assert store.refresh(specialist).state == HOLDING
    assert store.refresh(coordinator).state == HOLDING
    own_simulation = join(store, world, 103, claim(run_alone=True))
    assert store.refresh(own_simulation).state == HOLDING
    # A companion does not start beside a run-alone holder, so it never overcommits one.
    second = join(store, world, 104, companion(SPECIALIST_MB))
    assert store.refresh(second).state == QUEUED


def test_a_later_work_item_starts_while_its_specialist_holds(root, world):
    store = make_store(root, world)
    for pid in (101, 102):
        assert store.refresh(join(store, world, pid, companion(500))).state == HOLDING
    flow = join(store, world, 103, claim(300))
    assert (
        store.refresh(flow).state == HOLDING
    )  # only companions held: the first other claim starts
    assert store.shrink_reservation(flow, 0)
    first = join(store, world, 104, claim(run_alone=True))
    second = join(store, world, 105, claim(200))
    assert store.refresh(first).state == HOLDING
    assert store.refresh(second) == TokenState(QUEUED, position=0)
    store.release(first)
    assert store.refresh(second).state == HOLDING


def test_specialists_beyond_the_budget_queue_until_one_releases(root, world):
    store = make_store(root, world)
    tokens = [join(store, world, 101 + n, companion(SPECIALIST_MB)) for n in range(3)]
    assert [store.refresh(token).state for token in tokens] == [HOLDING, HOLDING, QUEUED]
    wait = store.memory_wait(103)
    assert wait is not None and wait.describe().startswith("needs 452 MiB")
    store.release(tokens[0])
    assert store.refresh(tokens[2]).state == HOLDING


def test_a_waiting_specialist_does_not_block_the_claims_behind_it(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(700))
    waiting = join(store, world, 102, companion(SPECIALIST_MB))
    behind = join(store, world, 103, claim(200))
    assert store.refresh(holder).state == HOLDING
    assert store.refresh(waiting).state == QUEUED
    assert store.refresh(behind).state == HOLDING
    store.release(holder)
    assert store.refresh(waiting).state == HOLDING


def test_a_waiting_specialist_judged_by_its_own_budget_does_not_block_a_sized_claim(root, world):
    store = make_store(root, world)
    holder = join(store, world, 101, companion(SPECIALIST_MB, budget_mb=SPECIALIST_MB))
    waiting = join(store, world, 102, companion(SPECIALIST_MB, budget_mb=SPECIALIST_MB))
    behind = join(store, world, 103, claim(100))
    assert store.refresh(holder).state == HOLDING
    assert store.refresh(waiting).state == QUEUED
    assert store.refresh(behind).state == HOLDING


def test_specialists_filling_the_budget_let_the_first_other_claim_start(root, world):
    store = make_store(root, world)
    for pid in (101, 102):
        assert store.refresh(join(store, world, pid, companion(500))).state == HOLDING
    first = join(store, world, 103, claim(500))
    second = join(store, world, 104, claim(500))
    assert store.refresh(first).state == HOLDING
    assert store.refresh(second).state == QUEUED


def test_the_coordinator_does_not_reduce_how_many_specialists_fit(root, world):
    budget = 3 * SPECIALIST_MB
    store = make_store(root, world)
    coordinator = join(store, world, 101, companion(0, budget_mb=budget))
    assert store.refresh(coordinator).state == HOLDING
    specialists = [
        join(store, world, 102 + n, companion(SPECIALIST_MB, budget_mb=budget)) for n in range(3)
    ]
    assert [store.refresh(token).state for token in specialists] == [HOLDING] * 3
    assert coordinator.memory is not None
    assert (
        job_slots.MemoryClaim.from_payload(coordinator.memory.to_payload()) == coordinator.memory
    )


def test_entry_creation_exhaustion_names_the_memory_reservation(root, world, monkeypatch):
    spawn(world, 100)
    store = make_store(root, world)

    def always_taken(_path, _target):
        raise FileExistsError

    monkeypatch.setattr(Path, "hardlink_to", always_taken)
    with pytest.raises(RuntimeError, match="unique Memory Reservation entry"):
        store.submit(job_slots.CLASS_MEMORY, memory=claim(1.0), pid=100)


def take_over(store: SlotStore, holder: job_slots.SlotToken, memory) -> job_slots.SlotToken:
    """Submit *memory* from the holder's own process, taking over the holder's place (#4482)."""
    return store.submit(job_slots.CLASS_MEMORY, pid=holder.pid, memory=memory, succeeds=holder)


@pytest.mark.parametrize(("wall_s", "expected"), [(180.0, HOLDING), (720.0, QUEUED)])
def test_a_short_later_claim_still_jumps_a_successor_that_cannot_start(
    root, world, wall_s, expected
):
    store = make_store(root, world)
    hold(store, world, 101, claim(300, wall_s=600.0))  # frees its memory in 10 min
    flow = hold(store, world, 102, claim(500))
    later = join(store, world, 103, claim(300, wall_s=wall_s))
    successor = take_over(store, flow, claim(800))
    assert store.refresh(successor) == TokenState(QUEUED, position=0)
    assert store.refresh(later).state == expected
    assert stored(store, flow).memory.reservation_mb == 0


def test_a_claim_takes_over_only_a_holder_that_still_reserves(root, world):
    store = make_store(root, world)
    flow = hold(store, world, 101, claim(500))
    later = join(store, world, 102, claim(100))
    assert store.shrink_reservation(flow, 0)
    behind = take_over(store, flow, claim(100))  # nothing to take over: queues at the back
    assert store.refresh(later).state == HOLDING
    assert store.refresh(behind).state == HOLDING
    assert behind.seq > later.seq
    # A holder whose lease is gone refuses the takeover and leaves no waiter behind.
    lost = hold(store, world, 103, claim(100))
    store.release(lost)
    with pytest.raises(job_slots.ClaimLostError):
        take_over(store, lost, claim(100))
    assert [token.pid for token in store.observe(job_slots.CLASS_MEMORY)[1]] == []


def test_no_waiter_ranks_while_a_claim_takes_over_a_holder(root, world, monkeypatch):
    store = make_store(root, world)
    rival = make_store(root, world)
    flow = hold(store, world, 101, claim(None, run_alone=True))
    later = join(rival, world, 102, claim(100))
    # The takeover shrinks the holder under the lease gate: a ranking may not start then.
    seen: list[tuple[bool, str]] = []
    interleave(
        monkeypatch,
        "hardlink_to",
        is_lease_gate,
        lambda: seen.append(
            ((flow.path.parent / PROMOTION_GATE).exists(), rival.refresh(later).state)
        ),
    )
    successor = take_over(store, flow, claim(None, run_alone=True))
    assert seen == [(True, QUEUED)]
    assert rival.refresh(later).state == QUEUED
    assert store.refresh(successor).state == HOLDING


def test_a_busy_promotion_gate_leaves_a_takeover_unsubmitted(root, world):
    store = make_store(root, world)
    rival = make_store(root, world)
    flow = hold(store, world, 101, claim(None, run_alone=True))
    join(rival, world, 102, claim(100))
    with held_gate(flow.path.parent / PROMOTION_GATE, 102, world.capture_identity(102)):
        with pytest.raises(job_slots.TakeoverBusyError):
            take_over(store, flow, claim(None, run_alone=True))
        with pytest.raises(job_slots.ClaimAbortedError):
            store.acquire(
                job_slots.CLASS_MEMORY,
                pid=101,
                memory=claim(None, run_alone=True),
                succeeds=flow,
                should_abort=lambda: True,
            )
    assert stored(store, flow).memory.run_alone
    assert [token.pid for token in store.observe(job_slots.CLASS_MEMORY)[1]] == [102]
    # Once the gate is free the takeover goes through at the holder's place.
    successor = store.acquire(
        job_slots.CLASS_MEMORY,
        pid=101,
        memory=claim(None, run_alone=True),
        succeeds=flow,
    )
    assert successor.seq == flow.seq
