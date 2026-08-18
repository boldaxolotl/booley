"""Tests for the shared on-disk slot store (ADR 0028 job admission).

The store is pure + injectable, so multi-process interleavings are simulated
deterministically: each simulated process is its own SlotStore over the same
directory, with its own fake PID, and the test controls which PIDs are
"alive" and what /proc reports for them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime import job_slots
from booley.runtime.job_slots import (
    CLASS_HEAVY,
    CLASS_LIGHT,
    CLASS_TICKET,
    HOLDING,
    LOST,
    QUEUED,
    ROLE_INTERACTIVE,
    ROLE_TICKET,
    QueueFullError,
    SlotCaps,
    SlotStore,
)


class FakeWorld:
    """Shared liveness/clock oracle for a set of simulated processes."""

    def __init__(self):
        self.alive: dict[int, list[str] | None] = {}  # pid -> argv (None = unknown)
        self.clock = 1_000_000.0

    def is_pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def read_cmdline(self, pid: int) -> list[str] | None:
        return self.alive.get(pid)

    def now(self) -> float:
        return self.clock


@pytest.fixture()
def world():
    return FakeWorld()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "slots"


def make_store(root: Path, world: FakeWorld, caps: SlotCaps | None = None) -> SlotStore:
    return SlotStore(
        root,
        caps,
        is_pid_alive=world.is_pid_alive,
        read_cmdline=world.read_cmdline,
        now=world.now,
        sleep=lambda s: None,
    )


def spawn(world: FakeWorld, pid: int, argv: list[str] | None = None):
    world.alive[pid] = argv


@pytest.mark.parametrize(("last_error", "expected"), [(5, True), (87, False)])
def test_windows_pid_liveness_only_treats_invalid_pid_as_dead(monkeypatch, last_error, expected):
    import ctypes

    class FakeCall:
        def __init__(self, result):
            self.result = result

        def __call__(self, *_args):
            return self.result

    class FakeKernel32:
        OpenProcess = FakeCall(0)
        GetExitCodeProcess = FakeCall(0)
        CloseHandle = FakeCall(1)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)

    assert job_slots._windows_pid_alive(1234) is expected


class TestSingleProcess:
    def test_first_claim_holds_immediately(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_HEAVY, pid=100, role=ROLE_INTERACTIVE)
        assert store.refresh(tok).state == HOLDING
        assert tok.is_holder

    def test_release_frees_the_slot(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        a = store.submit(CLASS_HEAVY, pid=100)
        assert store.refresh(a).state == HOLDING
        b = store.submit(CLASS_HEAVY, pid=100)
        assert store.refresh(b).state == QUEUED
        store.release(a)
        assert store.refresh(b).state == HOLDING

    def test_release_is_idempotent(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_HEAVY, pid=100)
        store.release(tok)
        store.release(tok)  # must not raise

    def test_release_retries_transient_permission_error(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_HEAVY, pid=100)
        original_unlink = Path.unlink
        attempts = 0

        def flaky_unlink(path, *args, **kwargs):
            nonlocal attempts
            if path == tok.path and attempts < 2:
                attempts += 1
                raise PermissionError("file is temporarily in use")
            attempts += 1
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)
        store.release(tok)

        assert attempts == 3
        assert not tok.path.exists()

    def test_cap_bounds_holders(self, root, world):
        spawn(world, 100)
        store = make_store(root, world, SlotCaps(max_light=3))
        toks = [store.submit(CLASS_LIGHT, pid=100) for _ in range(4)]
        states = [store.refresh(t).state for t in toks]
        assert states == [HOLDING, HOLDING, HOLDING, QUEUED]

    def test_promotion_retries_transient_permission_error(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_HEAVY, pid=100)
        original_rename = Path.rename
        attempts = 0

        def flaky_rename(path, target):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError("file is temporarily in use")
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", flaky_rename)

        assert store.refresh(tok).state == HOLDING
        assert attempts == 3

    def test_promotion_gate_prevents_a_second_process_from_promoting(
        self, root, world, monkeypatch
    ):
        spawn(world, 100)
        spawn(world, 200)
        first = make_store(root, world)
        second = make_store(root, world)
        first_token = first.submit(CLASS_HEAVY, pid=100)
        second_token = second.submit(CLASS_HEAVY, pid=200)

        # Model the stale-rank interleaving directly: the second claimant had
        # already observed rank zero when the first claimant entered the gate.
        with monkeypatch.context() as patch:
            patch.setattr(second, "_rank", lambda _token: 0)
            with first._promotion_gate(first_token) as acquired:
                assert acquired is True
                assert second.refresh(second_token).state == QUEUED

        assert first.refresh(first_token).state == HOLDING
        assert second.refresh(second_token).state == QUEUED

    def test_queue_positions_are_zero_based_and_ordered(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)  # max_heavy=1
        toks = [store.submit(CLASS_HEAVY, pid=100) for _ in range(3)]
        assert store.refresh(toks[0]).state == HOLDING
        assert store.refresh(toks[1]).position == 0
        assert store.refresh(toks[2]).position == 1

    def test_queue_full_raises(self, root, world):
        spawn(world, 100)
        store = make_store(root, world, SlotCaps(max_heavy=1, queue_max=2))
        store.submit(CLASS_HEAVY, pid=100)  # holder
        store.submit(CLASS_HEAVY, pid=100)  # waiter 1
        store.submit(CLASS_HEAVY, pid=100)  # waiter 2
        with pytest.raises(QueueFullError):
            store.submit(CLASS_HEAVY, pid=100)

    def test_unknown_class_rejected(self, root, world):
        store = make_store(root, world)
        with pytest.raises(ValueError):
            store.submit("warp", pid=100)

    def test_classes_are_independent(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        heavy = store.submit(CLASS_HEAVY, pid=100)
        ticket = store.submit(CLASS_TICKET, pid=100)
        assert store.refresh(heavy).state == HOLDING
        assert store.refresh(ticket).state == HOLDING


class TestPriorityAndPreemption:
    def test_interactive_overtakes_queued_ticket_work(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        store_t = make_store(root, world)
        store_i = make_store(root, world)
        holder = store_t.submit(CLASS_HEAVY, pid=200, role=ROLE_TICKET)
        assert store_t.refresh(holder).state == HOLDING
        queued_ticket = store_t.submit(CLASS_HEAVY, pid=200, role=ROLE_TICKET)
        interactive = store_i.submit(CLASS_HEAVY, pid=100, role=ROLE_INTERACTIVE)
        # The later interactive submit is ahead of the earlier ticket waiter.
        assert store_i.refresh(interactive).position == 0
        assert store_t.refresh(queued_ticket).position == 1

    def test_no_preemption_of_running_ticket_work(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        store_t = make_store(root, world)
        store_i = make_store(root, world)
        holder = store_t.submit(CLASS_HEAVY, pid=200, role=ROLE_TICKET)
        assert store_t.refresh(holder).state == HOLDING
        interactive = store_i.submit(CLASS_HEAVY, pid=100, role=ROLE_INTERACTIVE)
        # Interactive priority queues ahead of other waiters but never
        # displaces a holder.
        assert store_i.refresh(interactive).state == QUEUED
        assert store_t.refresh(holder).state == HOLDING

    def test_fifo_within_same_priority(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        store.submit(CLASS_HEAVY, pid=100)  # holder
        first = store.submit(CLASS_HEAVY, pid=100, role=ROLE_TICKET)
        second = store.submit(CLASS_HEAVY, pid=100, role=ROLE_TICKET)
        assert store.refresh(first).position == 0
        assert store.refresh(second).position == 1


class TestTwoProcessRaces:
    """Simulated interleavings of two claimant processes over one directory."""

    def test_both_race_for_last_slot_only_one_wins(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        b = make_store(root, world)
        # Both submit before either refreshes — the classic race window.
        tok_a = a.submit(CLASS_HEAVY, pid=100)
        tok_b = b.submit(CLASS_HEAVY, pid=200)
        state_a = a.refresh(tok_a)
        state_b = b.refresh(tok_b)
        states = sorted([state_a.state, state_b.state])
        assert states == [HOLDING, QUEUED]

    def test_loser_promotes_after_winner_releases(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        b = make_store(root, world)
        tok_a = a.submit(CLASS_HEAVY, pid=100)
        tok_b = b.submit(CLASS_HEAVY, pid=200)
        winner, loser, winner_store, loser_store = (
            (tok_a, tok_b, a, b) if a.refresh(tok_a).state == HOLDING else (tok_b, tok_a, b, a)
        )
        assert loser_store.refresh(loser).state == QUEUED
        winner_store.release(winner)
        assert loser_store.refresh(loser).state == HOLDING

    def test_seq_ties_have_total_order(self, root, world):
        # Two processes can allocate the same seq (scan-max race); the
        # pid in the entry name keeps the order total and deterministic.
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        b = make_store(root, world)
        tok_a = a.submit(CLASS_HEAVY, pid=100)
        tok_b = b.submit(CLASS_HEAVY, pid=200)
        # Force identical seq by construction check: if they differ this
        # test still holds — the point is exactly one holder either way.
        assert (a.refresh(tok_a).state == HOLDING) != (b.refresh(tok_b).state == HOLDING)


class TestGhostReaping:
    def test_dead_pid_holder_is_reaped_and_slot_recovered(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        dead_tok = a.submit(CLASS_HEAVY, pid=100)
        assert a.refresh(dead_tok).state == HOLDING
        del world.alive[100]  # process 100 dies holding the slot
        b = make_store(root, world)
        tok = b.submit(CLASS_HEAVY, pid=200)
        assert b.refresh(tok).state == HOLDING

    def test_dead_waiter_is_reaped_out_of_the_queue(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_HEAVY, pid=200)
        assert a.refresh(holder).state == HOLDING
        ghost_waiter = a.submit(CLASS_HEAVY, pid=100)
        assert a.refresh(ghost_waiter).state == QUEUED
        del world.alive[100]
        b = make_store(root, world)
        tok = b.submit(CLASS_HEAVY, pid=200)
        # The dead waiter no longer counts toward ordering.
        assert b.refresh(tok).position == 0

    def test_recycled_pid_argv_mismatch_is_reaped(self, root, world):
        spawn(world, 100, argv=["python", "-m", "booley.flows.sim"])
        a = make_store(root, world)
        tok = a.submit(CLASS_HEAVY, pid=100, argv=["python", "-m", "booley.flows.sim"])
        assert a.refresh(tok).state == HOLDING
        # PID 100 is recycled by an unrelated process.
        world.alive[100] = ["sshd"]
        spawn(world, 200)
        b = make_store(root, world)
        tok_b = b.submit(CLASS_HEAVY, pid=200)
        assert b.refresh(tok_b).state == HOLDING

    def test_unknown_cmdline_is_trusted_alive(self, root, world):
        spawn(world, 100, argv=None)  # /proc unavailable → cannot judge
        a = make_store(root, world)
        tok = a.submit(CLASS_HEAVY, pid=100, argv=["python", "-m", "x"])
        assert a.refresh(tok).state == HOLDING
        assert a.reap(CLASS_HEAVY) == []

    def test_holder_past_deadline_is_reaped(self, root, world):
        spawn(world, 100)
        a = make_store(root, world)
        tok = a.submit(CLASS_HEAVY, pid=100, timeout_s=600)
        assert a.refresh(tok).state == HOLDING
        world.clock += 600 + job_slots.DEADLINE_SLACK_SECONDS + 1
        spawn(world, 200)
        b = make_store(root, world)
        tok_b = b.submit(CLASS_HEAVY, pid=200)
        assert b.refresh(tok_b).state == HOLDING

    def test_waiter_gets_no_deadline(self, root, world):
        spawn(world, 100)
        a = make_store(root, world)
        holder = a.submit(CLASS_HEAVY, pid=100, timeout_s=600)
        assert a.refresh(holder).state == HOLDING
        waiter = a.submit(CLASS_HEAVY, pid=100, timeout_s=600)
        world.clock += 100_000  # far past any budget — queueing is legitimate
        assert a.refresh(waiter).state in (QUEUED, HOLDING)

    def test_reaped_token_reads_lost(self, root, world):
        spawn(world, 100)
        a = make_store(root, world)
        tok = a.submit(CLASS_HEAVY, pid=100)
        tok.path.unlink()  # someone judged us stale
        assert a.refresh(tok).state == LOST

    def test_fresh_junk_is_kept_old_junk_is_reaped(self, root, world):
        spawn(world, 100)
        a = make_store(root, world)
        cls_dir = root / CLASS_HEAVY
        cls_dir.mkdir(parents=True)
        junk = cls_dir / "not-an-entry.json"
        junk.write_text("{oops", encoding="utf-8")
        assert a.reap(CLASS_HEAVY) == []  # too young to condemn
        world.clock = junk.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
        assert a.reap(CLASS_HEAVY) == ["not-an-entry.json"]


class TestAcquire:
    def test_acquire_blocks_until_slot_frees(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_HEAVY, pid=100)
        assert a.refresh(holder).state == HOLDING

        b = make_store(root, world)
        positions: list[int] = []
        released = {"done": False}

        def sleep_then_release(_seconds):
            if not released["done"]:
                a.release(holder)
                released["done"] = True

        b._sleep = sleep_then_release
        tok = b.acquire(CLASS_HEAVY, pid=200, poll_interval=0.01, on_queued=positions.append)
        assert tok.is_holder
        assert positions == [0]

    def test_narration_repeats_while_position_is_unchanged(self, root, world):
        """A stuck queue must keep saying so, not narrate once and go quiet (F-27)."""
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_HEAVY, pid=100)
        assert a.refresh(holder).state == HOLDING

        b = make_store(root, world)
        positions: list[int] = []
        polls = {"n": 0}

        def tick(_seconds):
            polls["n"] += 1
            world.clock += 10  # 10s per poll
            if polls["n"] >= 8:  # 80s of waiting, then let it through
                a.release(holder)

        b._sleep = tick
        b.acquire(CLASS_HEAVY, pid=200, on_queued=positions.append, narrate_interval_s=30.0)

        # Position never moved, so without the periodic repeat this would be [0].
        assert positions == [0, 0, 0]

    def test_describe_holders_names_pid_flow_and_age(self, root, world):
        """The narration must identify the blocker, not just the wait (F-27)."""
        argv = ["/usr/bin/booley", "flow", "sim", "--target", "default"]
        spawn(world, 100, argv)
        store = make_store(root, world)
        tok = store.submit(CLASS_HEAVY, pid=100, argv=argv)
        assert store.refresh(tok).state == HOLDING

        world.clock += 613  # 10m13s later
        described = store.describe_holders(CLASS_HEAVY)
        assert "pid 100" in described
        assert "(sim)" in described
        assert "held 10m13s" in described

    def test_describe_holders_on_empty_class_does_not_raise(self, root, world):
        store = make_store(root, world)
        assert "holder unknown" in store.describe_holders(CLASS_HEAVY)

    def test_acquire_raises_when_entry_is_cancelled(self, root, world):
        # A live waiter's entry only vanishes via deliberate cancellation
        # (booley_cancel); re-queueing would undo it, so acquire must raise.
        spawn(world, 100)
        a = make_store(root, world)
        orig_submit = a.submit

        def submit_then_cancel(*args, **kwargs):
            tok = orig_submit(*args, **kwargs)
            tok.path.unlink()  # what cancel_waiter does from the server
            return tok

        a.submit = submit_then_cancel
        with pytest.raises(job_slots.ClaimLostError):
            a.acquire(CLASS_HEAVY, pid=100, poll_interval=0.01)

    def test_cancel_waiter_removes_queued_entry_only(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_HEAVY, pid=100)
        assert a.refresh(holder).state == HOLDING
        waiter = a.submit(CLASS_HEAVY, pid=200)
        assert a.refresh(waiter).state == QUEUED
        # Holder pid is refused; waiter pid is cancelled.
        assert a.cancel_waiter(100) is False
        assert a.cancel_waiter(200) is True
        assert a.refresh(waiter).state == LOST
        assert a.refresh(holder).state == HOLDING


def _race_worker(root_str: str, worker_id: int, rounds: int) -> None:
    """Real-process worker: repeatedly acquire the single heavy slot.

    While holding, drop a marker file and check for a rival's marker — a
    second concurrent holder means mutual exclusion is broken. Any violation
    is recorded as a file the parent asserts on (exceptions in spawned
    children don't propagate usefully).
    """
    import os
    import time as _time
    from pathlib import Path as _Path

    from booley.runtime.job_slots import CLASS_HEAVY, SlotStore

    root = _Path(root_str)
    store = SlotStore(root / "slots")
    active_dir = root / "active"
    for i in range(rounds):
        token = store.acquire(CLASS_HEAVY, pid=os.getpid(), poll_interval=0.005)
        marker = active_dir / f"active-{worker_id}"
        try:
            rivals = [p.name for p in active_dir.glob("active-*")]
            marker.write_text(str(i), encoding="utf-8")
            if rivals:
                (root / f"violation-{worker_id}-{i}").write_text(
                    ",".join(rivals), encoding="utf-8"
                )
            _time.sleep(0.01)  # widen the overlap window
        finally:
            marker.unlink(missing_ok=True)
            store.release(token)


class TestRealProcessRace:
    """The one non-simulated test: genuine processes hammering one slot."""

    def test_single_slot_is_mutually_exclusive_across_processes(self, tmp_path):
        import multiprocessing as mp

        (tmp_path / "active").mkdir()
        ctx = mp.get_context("spawn")
        workers = [ctx.Process(target=_race_worker, args=(str(tmp_path), w, 3)) for w in range(4)]
        for p in workers:
            p.start()
        try:
            for p in workers:
                p.join(timeout=60)
                assert not p.is_alive(), "race worker wedged — admission deadlock?"
                assert p.exitcode == 0
        finally:
            for p in workers:
                if p.is_alive():
                    p.terminate()
            for p in workers:
                p.join(timeout=5)
        violations = list(tmp_path.glob("violation-*"))
        assert violations == [], [v.read_text() for v in violations]


class TestSnapshot:
    def test_snapshot_orders_holders_then_waiters(self, root, world):
        spawn(world, 100)
        store = make_store(root, world, SlotCaps(max_heavy=1))
        holder = store.submit(CLASS_HEAVY, pid=100, argv=["sim"])
        assert store.refresh(holder).state == HOLDING
        store.submit(CLASS_HEAVY, pid=100, argv=["synth"], role=ROLE_TICKET)
        holders, waiters = store.snapshot(CLASS_HEAVY)
        assert [t.argv for t in holders] == [["sim"]]
        assert [t.argv for t in waiters] == [["synth"]]
        assert waiters[0].role == ROLE_TICKET

    def test_snapshot_of_empty_class(self, root, world):
        store = make_store(root, world)
        assert store.snapshot(CLASS_HEAVY) == ([], [])


class TestSlotCaps:
    def test_defaults_preserve_single_flight(self):
        caps = SlotCaps()
        assert caps.cap_for(CLASS_HEAVY) == 1
        assert caps.cap_for(CLASS_LIGHT) == 3
        assert caps.cap_for(CLASS_TICKET) == 2
        assert caps.queue_max == 8

    def test_zero_cap_is_clamped_to_one(self):
        caps = SlotCaps(max_heavy=0)
        assert caps.cap_for(CLASS_HEAVY) == 1


class TestJobsConfigParsing:
    def test_defaults_when_section_absent(self):
        from booley.config.agent import _parse_jobs_config

        caps = _parse_jobs_config({})
        assert caps == SlotCaps()

    def test_explicit_values(self):
        from booley.config.agent import _parse_jobs_config

        caps = _parse_jobs_config(
            {
                "jobs": {
                    "max_heavy": 2,
                    "max_light": 5,
                    "max_tickets": 4,
                    "queue_max": 16,
                }
            }
        )
        assert caps == SlotCaps(max_heavy=2, max_light=5, max_tickets=4, queue_max=16)

    def test_invalid_values_keep_defaults(self):
        from booley.config.agent import _parse_jobs_config

        caps = _parse_jobs_config(
            {
                "jobs": {
                    "max_heavy": 0,  # below floor
                    "max_light": True,  # bool is not an int here
                    "queue_max": -1,  # below floor
                }
            }
        )
        assert caps == SlotCaps()

    def test_queue_max_zero_is_allowed(self):
        # queue_max=0 means "never queue, refuse at cap" — a legal posture.
        from booley.config.agent import _parse_jobs_config

        caps = _parse_jobs_config({"jobs": {"queue_max": 0}})
        assert caps.queue_max == 0

    def test_non_table_section_uses_defaults(self):
        from booley.config.agent import _parse_jobs_config

        assert _parse_jobs_config({"jobs": "nope"}) == SlotCaps()


class TestPromotionDeadline:
    """Holder deadlines anchor at PROMOTION, never at entry creation.

    Queue wait must not count against the run budget: an entry can
    legitimately queue for longer than its own timeout_s, and charging that
    wait would reap a live holder — freeing an occupied slot (overcommit).
    """

    def test_promotion_stamps_promoted_at(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_HEAVY, pid=100, timeout_s=100)
        assert tok.promoted_at is None
        assert store.refresh(tok).state == HOLDING
        assert tok.promoted_at is not None
        # The stamp is durable: another store instance reads it back.
        reader = make_store(root, world)
        holders, _waiters = reader.snapshot(CLASS_HEAVY)
        assert holders[0].promoted_at == tok.promoted_at

    def test_queue_wait_does_not_count_against_holder_deadline(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        store = make_store(root, world)
        holder = store.submit(CLASS_HEAVY, pid=100)
        assert store.refresh(holder).state == HOLDING
        waiter = store.submit(CLASS_HEAVY, pid=200, timeout_s=100)
        assert store.refresh(waiter).state == QUEUED

        # Queue far past the waiter's own budget + slack: waiters have no
        # deadline, and the wait must not pre-charge the future holder.
        world.clock += 1_000
        assert store.reap(CLASS_HEAVY) == []
        store.release(holder)
        assert store.refresh(waiter).state == HOLDING

        # Freshly promoted: inside its (promotion-anchored) budget.
        world.clock += 100
        assert store.reap(CLASS_HEAVY) == []
        assert waiter.path.exists()

        # Now genuinely past promotion + budget + slack: reaped.
        world.clock += job_slots.DEADLINE_SLACK_SECONDS + 1
        assert store.reap(CLASS_HEAVY) == [waiter.path.name]

    def test_holder_without_promotion_stamp_has_no_deadline(self, root, world):
        # A holder that crashed between the rename and the payload rewrite
        # (or a pre-stamp writer) keeps no deadline — PID guards still apply.
        import json as _json

        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_HEAVY, pid=100, timeout_s=100)
        assert store.refresh(tok).state == HOLDING
        payload = _json.loads(tok.path.read_text(encoding="utf-8"))
        del payload["promoted_at"]
        tok.path.write_text(_json.dumps(payload) + "\n", encoding="utf-8")

        world.clock += 1_000_000
        assert store.reap(CLASS_HEAVY) == []


class TestAcquireWithdrawsOnFailure:
    def test_should_abort_raises_and_withdraws_entry(self, root, world):
        # Shutdown hook: a queued acquire must end (entry withdrawn) when the
        # caller's should_abort fires — the Runner's Ctrl+C path.
        spawn(world, 100)
        spawn(world, 200)
        store = make_store(root, world)
        holder = store.submit(CLASS_HEAVY, pid=100)
        assert store.refresh(holder).state == HOLDING

        aborts = iter([False, True])
        with pytest.raises(job_slots.ClaimAbortedError):
            store.acquire(CLASS_HEAVY, pid=200, should_abort=lambda: next(aborts))
        _holders, waiters = store.snapshot(CLASS_HEAVY)
        assert waiters == []

    def test_unexpected_error_mid_wait_withdraws_entry(self, root, world):
        # An exception escaping the poll loop (here: a narration hook blowing
        # up) must withdraw our own entry — a claim without a caller-held
        # token is unreleasable and, with the process alive, unreapable.
        spawn(world, 100)
        spawn(world, 200)
        store = make_store(root, world)
        holder = store.submit(CLASS_HEAVY, pid=100)
        assert store.refresh(holder).state == HOLDING

        def explode(_position):
            raise RuntimeError("narration boom")

        with pytest.raises(RuntimeError, match="narration boom"):
            store.acquire(CLASS_HEAVY, pid=200, on_queued=explode)
        _holders, waiters = store.snapshot(CLASS_HEAVY)
        assert waiters == []


class TestSlotsDir:
    """Where the store lives. `.booley_project/runtime/` here is DOTLESS on
    purpose (init_cmd F-6): `.runtime/` is the multi-GB EDA scratch tree,
    `runtime/` is container-lifetime bookkeeping (doctor stamp, developer
    probe, slot store) that must survive a scratch wipe. Nothing else pinned
    this path, so "deduping" the two would have gone green."""

    def test_is_project_scoped_and_dotless(self, tmp_path, monkeypatch):
        from booley.runtime.project_dir import reset_cache

        monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
        reset_cache()
        try:
            assert job_slots.slots_dir() == tmp_path / "runtime" / "jobs" / "slots"
        finally:
            reset_cache()

    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "elsewhere"))
        assert job_slots.slots_dir() == tmp_path / "elsewhere"

    def test_resolution_never_creates_the_dir(self, tmp_path, monkeypatch):
        # Readers (mcp_server._job_phase) call this on hot paths; it must not
        # have runtime_dir()'s mkdir side effect.
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "elsewhere"))
        job_slots.slots_dir()
        assert not (tmp_path / "elsewhere").exists()
