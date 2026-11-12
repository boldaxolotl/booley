"""Memory Reservations in the shared slot store (#3700, ADR 0087).

A memory claim holds while the reservations of all holders fit its budget. The
store names the fields it needs through ``job_slots`` at call time, so a missing
feature fails inside each test body rather than at import.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.runtime import job_slots
from booley.runtime.job_slots import (
    CLASS_HEAVY,
    HOLDING,
    LOST,
    QUEUED,
    ROLE_BACKGROUND,
    ROLE_INTERACTIVE,
    QueueFullError,
    SlotCaps,
    SlotStore,
    TokenState,
)
from tests.test_job_slots import FakeWorld, make_store, spawn

BUDGET_MB = 1000.0


@pytest.fixture()
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "slots"


def claim(
    reservation_mb: float | None = None, *, run_alone: bool = False, budget_mb: float = BUDGET_MB
):
    """A memory claim against the shared test budget."""
    return job_slots.MemoryClaim(
        budget_mb=budget_mb, reservation_mb=reservation_mb, run_alone=run_alone
    )


def join(store: SlotStore, world: FakeWorld, pid: int, memory, *, role: str = ROLE_INTERACTIVE):
    """Start simulated process *pid* and submit its memory claim."""
    spawn(world, pid)
    return store.submit(job_slots.CLASS_MEMORY, pid=pid, memory=memory, role=role)


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
    holders = store.describe_holders(job_slots.CLASS_MEMORY)
    assert "pid 101" in holders and "pid 102" in holders
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


def test_full_queue_refuses(root, world):
    store = make_store(root, world, SlotCaps(queue_max=1))
    holder = join(store, world, 101, claim(800))
    assert store.refresh(holder).state == HOLDING
    join(store, world, 102, claim(400))
    with pytest.raises(QueueFullError, match="memory queue is full"):
        join(store, world, 103, claim(100))


def test_claims_that_fit_never_count_as_queued(root, world):
    store = make_store(root, world, SlotCaps(queue_max=0))
    first = join(store, world, 101, claim(100))
    second = join(store, world, 102, claim(100))  # waits only for a claim that fits
    assert store.refresh(first).state == HOLDING
    assert store.refresh(second).state == HOLDING


def test_racing_submit_cannot_overfill_the_queue(root, world, monkeypatch):
    caps = SlotCaps(queue_max=1)
    store = make_store(root, world, caps)
    rival = make_store(root, world, caps)
    holder = join(store, world, 101, claim(800))
    assert store.refresh(holder).state == HOLDING
    real_create = store._create_entry
    rivals = []

    def create_after_rival(request):
        # An interactive claim slips in after the background claim's bound check.
        rivals.append(join(rival, world, 103, claim(400)))
        return real_create(request)

    monkeypatch.setattr(store, "_create_entry", create_after_rival)
    with pytest.raises(QueueFullError):
        join(store, world, 102, claim(400), role=ROLE_BACKGROUND)
    assert rivals[0].path.exists()
    assert [path.name for path in (root / "memory").glob("w-*.json")] == [rivals[0].path.name]


def test_promotion_rereads_holders_inside_the_gate(root, world, monkeypatch):
    store = make_store(root, world)
    rival = make_store(root, world)
    head = join(store, world, 101, claim(600), role=ROLE_BACKGROUND)
    real_gate = store._promotion_gate
    winners = []

    def gate_after_rival_promotes(token):
        # Another process takes the budget between B's check and B's gate.
        rival_token = join(rival, world, 102, claim(600))
        winners.append(rival.refresh(rival_token))
        return real_gate(token)

    monkeypatch.setattr(store, "_promotion_gate", gate_after_rival_promotes)
    assert store.refresh(head).state == QUEUED
    assert winners == [TokenState(HOLDING)]
    assert sorted(path.name[0] for path in (root / "memory").glob("*.json")) == ["h", "w"]


def test_unreadable_holder_blocks_promotion(root, world, monkeypatch):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(100))
    assert store.refresh(holder).state == HOLDING
    waiter = join(store, world, 102, claim(100))
    real_load = store._load_token
    unreadable = True

    def load(path):
        if unreadable and path == holder.path:
            raise OSError("sharing violation")
        return real_load(path)

    monkeypatch.setattr(store, "_load_token", load)
    assert store.refresh(waiter).state == QUEUED  # unknown reservation: fail closed
    unreadable = False
    assert store.refresh(waiter).state == HOLDING


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param(
            lambda store: store.submit(job_slots.CLASS_MEMORY, pid=1), id="memory-without-claim"
        ),
        pytest.param(
            lambda store: store.submit(CLASS_HEAVY, pid=1, memory=claim(1)), id="heavy-with-claim"
        ),
        pytest.param(lambda _store: claim(1, run_alone=True), id="reservation-and-run-alone"),
        pytest.param(lambda _store: claim(), id="neither-reservation-nor-run-alone"),
        pytest.param(lambda _store: claim(2000), id="reservation-above-budget"),
        pytest.param(lambda _store: claim(0), id="zero-reservation"),
        pytest.param(lambda _store: claim(1, budget_mb=float("nan")), id="non-finite-budget"),
    ],
)
def test_claim_shape_is_validated(root, world, attempt):
    with pytest.raises(ValueError):
        attempt(make_store(root, world))


def test_zero_budget_admits_only_run_alone_claims(root, world):
    store = make_store(root, world)
    alone = join(store, world, 101, claim(run_alone=True, budget_mb=0.0))
    assert store.refresh(alone).state == HOLDING


@pytest.mark.parametrize(
    "malformed",
    [
        {"budget_mb": "lots", "reservation_mb": 1, "run_alone": False},
        {"reservation_mb": 1},
        "junk",
    ],
    ids=["bad-budget", "missing-budget", "not-a-table"],
)
def test_malformed_memory_field_is_read_as_no_claim(root, world, malformed):
    store = make_store(root, world)
    holder = join(store, world, 101, claim(100))
    assert store.refresh(holder).state == HOLDING
    payload = json.loads(holder.path.read_text(encoding="utf-8"))
    payload["memory"] = malformed
    holder.path.write_text(json.dumps(payload), encoding="utf-8")
    reloaded = store._load_token(holder.path)
    assert reloaded is not None and reloaded.memory is None
    waiter = join(store, world, 102, claim(100))
    assert store.refresh(waiter).state == QUEUED  # a holder with no readable claim blocks
