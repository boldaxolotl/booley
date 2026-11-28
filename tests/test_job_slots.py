"""Tests for the shared on-disk slot store (ADR 0028 job admission).

The store is pure + injectable, so multi-process interleavings are simulated
deterministically: each simulated process is its own SlotStore over the same
directory, with its own fake PID, and the test controls which PIDs are
"alive" and what /proc reports for them.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from booley.core.atomic_files import atomic_write_json
from booley.core.sharing_retry import read_file_bytes, write_file_text
from booley.runtime import booley_dir as runtime_booley_dir
from booley.runtime import execution_records, job_slots
from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    RUNTIME_EXECUTION_ENV,
    execution_paths,
    read_json,
)
from booley.runtime.job_slots import (
    CLASS_MEMORY,
    HOLDING,
    LOST,
    QUEUED,
    ROLE_INTERACTIVE,
    SlotStore,
)
from booley.runtime.pid import DEAD, REUSED, RUNNING, ProcessIdentity, ProcessObservation
from tests.job_slot_steps import PROMOTION_GATE, held_gate, lease_gate
from tests.sharing_error_support import hold_opens, hold_unlinks
from tests.time_budget import hang_guard_s


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

    def capture_identity(self, pid: int) -> ProcessIdentity | None:
        if pid not in self.alive:
            return None
        return ProcessIdentity(pid, "fake", self._start_ticks(pid))

    def observe_identity(self, identity: ProcessIdentity) -> ProcessObservation:
        if identity.pid not in self.alive:
            return ProcessObservation(DEAD)
        current = self.capture_identity(identity.pid)
        state = RUNNING if current == identity else REUSED
        return ProcessObservation(state)

    def _start_ticks(self, pid: int) -> int:
        argv = self.alive[pid]
        text = "\0".join(argv) if argv is not None else "unknown"
        return sum((index + 1) * ord(character) for index, character in enumerate(text))


@pytest.fixture()
def world():
    return FakeWorld()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "slots"


def windows_sharing_error() -> PermissionError:
    """The error Windows raises when a handle without FILE_SHARE_DELETE races a replace."""
    exc = PermissionError(13, "The process cannot access the file")
    exc.winerror = 32  # type: ignore[attr-defined]  # POSIX lacks the field
    return exc


class BackoffClock:
    """Accumulates the sleeps a store requests so a fault can end after N seconds."""

    def __init__(self) -> None:
        self.elapsed = 0.0

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def deny_until(
    monkeypatch, method: str, target: Path, clock: BackoffClock, seconds: float
) -> None:
    """Make ``Path.<method>`` on *target* raise a Windows sharing error until the
    store has backed off for *seconds*."""
    original = getattr(Path, method)

    def flaky(path, *args, **kwargs):
        denied = path == target or (args and args[0] == target)
        if denied and clock.elapsed < seconds:
            raise windows_sharing_error()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, flaky)


# Far past the store's age rule for unreadable entries and owner-less gates.
OLD_S = 3600.0
# A gate a dead process left behind: a bare PID, as gates were written before
# they named their owner's identity. The PID is above the kernel limit.
_DEAD_GATE_PID = 4_194_305


def is_lease_gate(path: Path) -> bool:
    return path.name.endswith(".lease.lock")


def is_rewrite_tmp(path: Path) -> bool:
    return path.name.endswith(".rewrite.tmp")


def interleave(monkeypatch, method: str, matches, action, *, after: bool = False) -> list[Path]:
    """Run *action* once, around the first ``Path.<method>`` call on a path *matches* accepts.

    The action runs before the call, or with ``after`` once it has returned or
    raised. Returns the paths the action fired on.
    """
    original = getattr(Path, method)
    fired: list[Path] = []

    def hooked(path, *args, **kwargs):
        if fired or not matches(path):
            return original(path, *args, **kwargs)
        fired.append(path)
        if not after:
            action()
            return original(path, *args, **kwargs)
        try:
            return original(path, *args, **kwargs)
        finally:
            action()

    monkeypatch.setattr(Path, method, hooked)
    return fired


def verified_inheritance(monkeypatch, world: FakeWorld, ancestors: set[tuple[int, int]]) -> None:
    """Make the fake world's ancestry decide which owners inherit an execution ID.

    Patched because the default verifier asks the host's real process ancestry,
    which the fake world cannot reach.
    """
    monkeypatch.setattr(
        job_slots,
        "verify_inherited_execution_id",
        lambda raw, *, booley_dir, owner_pid: execution_records.verify_inherited_execution_id(
            raw,
            booley_dir=booley_dir,
            owner_pid=owner_pid,
            observe_identity=world.observe_identity,
            ancestor_check=lambda owner, ancestor: (owner, ancestor) in ancestors,
        ),
    )


@pytest.fixture()
def booley_dir_cache():
    """A fresh Booley Directory resolution before and after the test."""
    runtime_booley_dir.reset_cache()
    yield
    runtime_booley_dir.reset_cache()


_ONE_MIB_CLAIM = job_slots.MemoryClaim(budget_mb=1024, reservation_mb=1)
# One holder at a time: a second claim waits behind it.
_ALONE = job_slots.MemoryClaim(budget_mb=1024, run_alone=True)
# Two claims hold together; a third waits.
_HALF = job_slots.MemoryClaim(budget_mb=2, reservation_mb=1)


def make_store(
    root: Path, world: FakeWorld, *, sleep=None, capture_identity=None, getpid=None, recovery=None
) -> SlotStore:
    """A simulated process. Its lease gates name the process *getpid* returns (the real
    test process by default): a test that holds a lease gate while another store looks
    at it names a live PID of the world."""
    return SlotStore(
        root,
        is_pid_alive=world.is_pid_alive,
        read_cmdline=world.read_cmdline,
        now=world.now,
        sleep=sleep or (lambda _seconds: None),
        capture_identity=capture_identity or world.capture_identity,
        observe_identity=world.observe_identity,
        recovery=recovery or job_slots.SlotRecovery(recover_process_owner=lambda _identity: True),
        getpid=getpid or os.getpid,
    )


def make_execution_store(
    root: Path,
    world: FakeWorld,
    *,
    execution_terminal,
    cancellations: list[str],
    inherited_execution_verifier=None,
    sleep=None,
) -> SlotStore:
    kwargs = {}
    if inherited_execution_verifier is not None:
        kwargs["inherited_execution_verifier"] = inherited_execution_verifier
    return SlotStore(
        root,
        is_pid_alive=world.is_pid_alive,
        read_cmdline=world.read_cmdline,
        now=world.now,
        sleep=sleep or (lambda _seconds: None),
        capture_identity=world.capture_identity,
        observe_identity=world.observe_identity,
        recovery=job_slots.SlotRecovery(
            execution_is_terminal=execution_terminal,
            cancel_execution=cancellations.append,
            recover_execution=lambda _execution_id: False,
            recover_process_owner=lambda _identity: False,
        ),
        **kwargs,
    )


def spawn(world: FakeWorld, pid: int, argv: list[str] | None = None):
    world.alive[pid] = argv


def advance_renewing(world: FakeWorld, store: SlotStore, holder, seconds: int) -> None:
    """Advance the clock by *seconds*, renewing *holder*'s lease at the production interval."""
    step = int(job_slots.LEASE_RENEW_INTERVAL_SECONDS)
    for _ in range(seconds // step):
        world.clock += step
        assert store.renew(holder)


def active_execution_record(supervisor: ProcessIdentity) -> dict:
    return {
        "schema_version": PROTOCOL_VERSION,
        "state": "running",
        "runtime_identity": supervisor.identity_scope,
        "supervisor": supervisor.to_payload(),
        "leader": None,
        "exit_code": None,
        "tree_terminal": False,
        "terminal_cause": None,
        "updated_at": "1970-01-01T00:00:00Z",
    }


GATE_ROWS = {  # gate, content, old, unreadable, recycled PID, state of the refresh
    "lease-gate-live": ("lease", "live", False, False, False, QUEUED),
    "promotion-gate-live": ("promotion", "live", False, False, False, QUEUED),
    "promotion-gate-unreadable-live": ("promotion", "live", True, True, False, QUEUED),
    "promotion-gate-recycled-pid": ("promotion", "live", False, False, True, HOLDING),
    "junk-bytes-young": ("promotion", b"\xff\xfe junk\n", False, False, False, QUEUED),
    "junk-bytes-old": ("promotion", b"\xff\xfe junk\n", True, False, False, HOLDING),
    "junk-text-old": ("promotion", b"not json\n", True, False, False, HOLDING),
}


class TestSingleProcess:
    def test_first_claim_holds_immediately(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, role=ROLE_INTERACTIVE)
        assert store.refresh(tok).state == HOLDING
        assert tok.is_holder

    @pytest.mark.parametrize(
        ("seconds", "renewed"), [(0.5, True), (1000.0, False)], ids=["brief", "lasting"]
    )
    def test_renew_through_a_denied_rewrite(self, root, world, monkeypatch, seconds, renewed):
        spawn(world, 100)
        clock = BackoffClock()
        store = make_store(root, world, sleep=clock.sleep)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        deny_until(monkeypatch, "replace", token.path, clock, seconds=seconds)

        assert store.renew(token) is renewed
        assert token.lease_health.lost.is_set() is not renewed
        if renewed:
            assert store.snapshot(CLASS_MEMORY)[0][0].lease_generation == 1

    def test_renew_retries_transient_lease_gate_permission_error(self, root, world, monkeypatch):
        # Windows denies a new gate while the previous gate's delete is pending (#2024).
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        original_link = os.link
        denials = 0

        def flaky_link(source, target, *args, **kwargs):
            nonlocal denials
            if Path(target).name.endswith(".lease.lock") and denials < 2:
                denials += 1
                raise PermissionError("delete pending")
            return original_link(source, target, *args, **kwargs)

        monkeypatch.setattr(os, "link", flaky_link)

        assert store.renew(token) is True
        assert denials == 2
        assert not token.lease_health.lost.is_set()

    def test_unreadable_live_entry_is_not_absent(self, root, world, monkeypatch):
        # A lock outlasting every retry still must not read as cancellation (#2024).
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        held = {token.path: 10**6}
        hold_opens(monkeypatch, held)

        assert store.token_absent(token) is False
        held[token.path] = 0
        store.release(token)
        assert store.token_absent(token) is True

    def test_renew_survives_windows_read_sharing_error_within_budget(
        self, root, world, monkeypatch
    ):
        # Longer than the old private 0.2 s loop, inside the shared helper's budget (#1905).
        spawn(world, 100)
        clock = BackoffClock()
        store = make_store(root, world, sleep=clock.sleep)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        deny_until(monkeypatch, "open", token.path, clock, seconds=0.5)

        assert store.renew(token) is True
        assert not token.lease_health.lost.is_set()
        assert store.token_absent(token) is False

    def test_unreadable_entry_is_not_treated_as_a_live_lease(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        token.execution_id = "a" * 32  # recovery only claims execution-bound entries
        hold_opens(monkeypatch, {token.path: 10**6})

        assert store.renew(token) is False  # unreadable past the budget: lease lost
        assert token.lease_health.lost.is_set()
        assert store.begin_token_recovery(token) is False
        store.release(token)  # still our own entry to release
        assert not token.path.exists()

    @staticmethod
    def _queued_waiter(root, world):
        """A store with a holder and one queued waiter whose backoff sleeps are simulated."""
        spawn(world, 100)
        spawn(world, 101)
        clock = BackoffClock()
        store = make_store(root, world, sleep=clock.sleep)
        holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(holder).state == HOLDING  # so the next entry stays queued
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=101)
        return store, waiter, clock

    @pytest.mark.parametrize(
        ("withdraw", "fault", "withdrawn"),
        [
            ("pid", "brief", True),
            ("token", "brief", True),
            ("pid", "lasting", False),
            ("token", "lasting", False),
            ("token", "unreadable", False),
        ],
    )
    def test_withdrawal_is_refused_or_retried(
        self, root, world, monkeypatch, caplog, withdraw, fault, withdrawn
    ):
        store, waiter, clock = self._queued_waiter(root, world)
        if fault == "unreadable":
            hold_opens(monkeypatch, {waiter.path: 10**6})
        else:
            seconds = 0.005 if fault == "brief" else 1000.0
            deny_until(monkeypatch, "unlink", waiter.path, clock, seconds=seconds)

        with caplog.at_level(logging.WARNING, logger="booley.runtime.job_slots"):
            if withdraw == "pid":
                result = store.cancel_waiter(waiter.pid)
            else:
                result = store.cancel_waiter_token(waiter)

        assert result is withdrawn
        assert waiter.path.exists() is not withdrawn
        if fault == "lasting":
            assert [t.lease_id for t in store.snapshot(CLASS_MEMORY)[1]] == [waiter.lease_id]
            assert "Refusing to withdraw" in caplog.text
            assert waiter.path.name in caplog.text

    def test_renew_does_not_resurrect_a_deleted_holder(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        interleave(monkeypatch, "write_text", is_rewrite_tmp, token.path.unlink)

        assert store.renew(token) is False
        assert not token.path.exists()

    @pytest.mark.parametrize("variant", ["promotion", "recovery"])
    def test_cancel_in_a_rewrite_window_never_leaves_a_cancelled_entry(
        self, root, world, monkeypatch, variant
    ):
        spawn(world, 100)
        spawn(world, os.getpid())  # the process the lease gates name
        cancellations: list[str] = []
        store = make_execution_store(
            root,
            world,
            execution_terminal=lambda _execution_id: False,
            cancellations=cancellations,
        )
        canceller = make_store(root, world)
        execution_id = "d" * 32
        if variant == "recovery":
            blocker = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
            assert store.refresh(blocker).state == HOLDING
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, execution_id=execution_id)
        cancelled: list[bool] = []
        interleave(
            monkeypatch,
            "replace",
            is_rewrite_tmp,
            lambda: cancelled.append(canceller.cancel_waiter_token(waiter)),
        )

        if variant == "promotion":
            assert store.refresh(waiter).state == HOLDING
        else:
            copy = next(t for t in store.observe(CLASS_MEMORY)[1] if t.lease_id == waiter.lease_id)
            assert store.begin_token_recovery(copy) is True

        assert cancelled == [False]
        holders, waiters = store.observe(CLASS_MEMORY)
        entries = [t for t in holders + waiters if t.lease_id == waiter.lease_id]
        assert len(entries) == 1
        if variant == "promotion":
            assert entries[0].is_holder
        else:
            assert entries[0].lease_state == job_slots.LEASE_CANCELLING

    def test_promotion_after_a_successful_cancel_reports_lost(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        canceller = make_store(root, world)
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        cancelled: list[bool] = []
        interleave(
            monkeypatch,
            "hardlink_to",
            is_lease_gate,
            lambda: cancelled.append(canceller.cancel_waiter_token(waiter)),
        )

        assert store.refresh(waiter).state == LOST
        assert cancelled == [True]
        holders, waiters = store.observe(CLASS_MEMORY)
        assert [t for t in holders + waiters if t.lease_id == waiter.lease_id] == []

    @pytest.mark.parametrize("row", sorted(GATE_ROWS))
    def test_promotion_waits_for_a_live_gate_and_breaks_a_stale_one(
        self, root, world, monkeypatch, row
    ):
        kind, content, old, unreadable, recycled, expected = GATE_ROWS[row]
        spawn(world, 100)
        spawn(world, 300)
        store = make_store(root, world)
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        gate = lease_gate(waiter) if kind == "lease" else waiter.path.parent / PROMOTION_GATE
        if content == "live":
            owner = {"pid": 300, "owner_identity": world.capture_identity(300).to_payload()}
            content = json.dumps(owner).encode("utf-8") + b"\n"
        gate.write_bytes(content)
        if old:
            world.clock = gate.stat().st_mtime + OLD_S + 1
        if recycled:
            world.alive[300] = ["another", "process"]  # PID 300 is live again, as a new process
        if unreadable:
            real_read = Path.read_bytes

            def scanned(path, *args, **kwargs):
                if path == gate:
                    raise windows_sharing_error()  # a scanner holds the gate file
                return real_read(path, *args, **kwargs)

            monkeypatch.setattr(Path, "read_bytes", scanned)

        assert store.refresh(waiter).state == expected
        if expected == QUEUED:
            assert waiter.path.exists()
            assert not waiter.is_holder
            monkeypatch.undo()
            gate.unlink()
            assert store.refresh(waiter).state == HOLDING

    def test_promotion_of_an_unreadable_waiter_stays_queued(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        hold_opens(monkeypatch, {waiter.path: 10**6})

        assert store.refresh(waiter).state == QUEUED
        assert waiter.path.exists()
        assert not waiter.is_holder

    def test_cancel_refuses_a_holder_token(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING

        assert store.cancel_waiter_token(token) is False
        assert token.path.exists()

    def test_promotion_never_reactivates_a_recovery_claimed_waiter(self, root, world):
        spawn(world, 100)
        store = make_execution_store(
            root, world, execution_terminal=lambda _execution_id: False, cancellations=[]
        )
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, execution_id="e" * 32)
        copy = store.observe(CLASS_MEMORY)[1][0]
        assert store.begin_token_recovery(copy) is True

        assert store.refresh(waiter).state == QUEUED

        holders, waiters = store.observe(CLASS_MEMORY)
        assert holders == []
        assert [t.lease_state for t in waiters] == [job_slots.LEASE_CANCELLING]

    def _release_threads(self, token: job_slots.SlotToken) -> list[threading.Thread]:
        name = f"booley-slot-release-{token.lease_id[:8]}"
        return [t for t in threading.enumerate() if t.name == name]

    def test_release_without_a_thread_logs_the_give_up(self, root, world, caplog, monkeypatch):
        spawn(world, 100)
        spawn(world, 300)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        caplog.set_level(logging.WARNING, logger="booley.runtime.job_slots")

        def no_thread(self_thread):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", no_thread)
        with held_gate(lease_gate(token), 300, world.capture_identity(300)):
            store.release(token)

        assert any(
            "Could not release Memory Reservation" in r.getMessage() for r in caplog.records
        )

    def test_release_of_a_removed_entry_starts_no_retry(self, root, world, caplog):
        spawn(world, 100)
        spawn(world, 300)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        caplog.set_level(logging.WARNING, logger="booley.runtime.job_slots")

        with held_gate(lease_gate(token), 300, world.capture_identity(300)):
            token.path.unlink()
            store.release(token)
            assert lease_gate(token).exists()

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert self._release_threads(token) == []

    @pytest.mark.parametrize("how", ["release-brief", "release-lasting", "reap-lasting"])
    def test_entry_removal_retries_and_reports_a_lasting_sharing_violation(
        self, root, world, monkeypatch, caplog, how
    ):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        reap = how == "reap-lasting"
        if reap:
            del world.alive[100]
        held = {token.path: 2 if how == "release-brief" else 10**6}
        hold_unlinks(monkeypatch, held)

        def remove():
            return store.reap(CLASS_MEMORY) if reap else store.release(token)

        with caplog.at_level(logging.WARNING, logger="booley.runtime.job_slots"):
            first = remove()

        if how == "release-brief":
            assert not token.path.exists()
            assert "Could not remove" not in caplog.text
            return
        assert token.path.exists()
        assert [r.getMessage() for r in caplog.records] == [
            f"Could not remove Memory Reservation after retries: {token.path.name}"
        ]
        held[token.path] = 0  # the next attempt finds the entry released
        second = remove()
        assert not token.path.exists()
        if reap:
            assert (first, second) == ([], [token.path.name])

    def test_undecodable_entry_reads_as_absent(self, root, world):
        spawn(world, 100)
        store = make_execution_store(
            root, world, execution_terminal=lambda _execution_id: False, cancellations=[]
        )
        execution_id = "b" * 32
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, execution_id=execution_id)
        assert store.execution_token_absent(CLASS_MEMORY, execution_id) is False
        tok.path.write_bytes(b"\xff")

        assert store.snapshot(CLASS_MEMORY) == ([], [])
        assert store.execution_token_absent(CLASS_MEMORY, execution_id) is True

    def test_promotion_retries_transient_permission_error(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
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

    def test_queue_positions_are_zero_based_and_ordered(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        toks = [store.submit(CLASS_MEMORY, memory=_ALONE, pid=100) for _ in range(3)]
        assert store.refresh(toks[0]).state == HOLDING
        assert store.refresh(toks[1]).position == 0
        assert store.refresh(toks[2]).position == 1

    def test_a_waiter_without_a_claim_never_starts(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        waiter.memory = None
        state = store.refresh(waiter)
        assert (state.state, state.position) == (QUEUED, 0)


def test_racing_claims_admit_one_and_the_loser_follows(root, world):
    spawn(world, 100)
    spawn(world, 200)
    a = make_store(root, world)
    b = make_store(root, world)
    # Both submit before either refreshes: the classic race window.
    tok_a = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
    tok_b = b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
    winner, loser, winner_store, loser_store = (
        (tok_a, tok_b, a, b) if a.refresh(tok_a).state == HOLDING else (tok_b, tok_a, b, a)
    )
    assert loser_store.refresh(loser).state == QUEUED
    assert winner_store.refresh(winner).state == HOLDING
    winner_store.release(winner)
    winner_store.release(winner)  # idempotent
    assert loser_store.refresh(loser).state == HOLDING


@pytest.mark.parametrize(
    ("same_store", "promote_before_publication"),
    [(True, False), (False, False), (False, True)],
    ids=["same-store", "other-store-waiter", "other-store-holder"],
)
def test_interleaved_publication_keeps_distinct_entries(
    root, world, monkeypatch, same_store, promote_before_publication
):
    spawn(world, 100)
    first_store = make_store(root, world)
    second_store = first_store if same_store else make_store(root, world)
    seconds: list[job_slots.SlotToken] = []

    def second_submission() -> None:
        # Both submissions may select the same sequence before either entry is published.
        second = second_store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
        if promote_before_publication:
            assert second_store.refresh(second).state == HOLDING
        seconds.append(second)

    interleave(monkeypatch, "hardlink_to", lambda path: path.suffix == ".json", second_submission)
    first = first_store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    (second,) = seconds
    assert second.seq == first.seq
    # Equal sequence and PID: the lease suffix orders the entries, and a waiter promotes behind it.
    for token, owner in sorted(
        ((first, first_store), (second, second_store)), key=lambda pair: pair[0].path.name
    ):
        assert owner.refresh(token).state == HOLDING
    assert first_store.renew(first) and second_store.renew(second)
    assert first.path != second.path
    holders, waiters = first_store.snapshot(CLASS_MEMORY)
    assert {token.lease_id for token in holders} == {first.lease_id, second.lease_id}
    assert not waiters
    first_store.release(first)
    second_store.release(second)
    assert first_store.snapshot(CLASS_MEMORY) == ([], [])


def test_cancelled_publication_never_deletes_replacement(root, world, monkeypatch):
    spawn(world, 100)
    first_store = make_store(root, world)
    replacement_store = make_store(root, world)
    replacements: list[job_slots.SlotToken] = []

    def cancel_and_replace() -> None:
        assert first_store.cancel_waiter(100)
        replacements.append(replacement_store.submit(CLASS_MEMORY, memory=_HALF, pid=100))

    interleave(
        monkeypatch,
        "hardlink_to",
        lambda path: path.suffix == ".json",
        cancel_and_replace,
        after=True,
    )
    cancelled = first_store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    (replacement,) = replacements
    assert first_store.refresh(cancelled).state == LOST
    assert replacement.path != cancelled.path
    assert replacement_store.refresh(replacement).state == HOLDING
    assert replacement_store.renew(replacement)
    replacement_store.release(replacement)
    assert first_store.snapshot(CLASS_MEMORY) == ([], [])


def test_legacy_counter_names_coexist_with_lease_names(root, world):
    spawn(world, 100)
    store = make_store(root, world)
    current = store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    legacy = store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    # The persisted pre-lease name older releases wrote, at the same sequence as `current`.
    old_name = f"w-p{legacy.priority}-s{current.seq:010d}-{legacy.pid:010d}-0001.json"
    legacy.path.rename(legacy.path.with_name(old_name))
    loaded = next(t for t in store.observe(CLASS_MEMORY)[1] if t.lease_id == legacy.lease_id)
    assert loaded.n == 1 and loaded.seq == current.seq
    assert store.refresh(loaded).state == HOLDING
    assert store.refresh(current).state == HOLDING
    holders, waiters = store.snapshot(CLASS_MEMORY)
    assert [token.lease_id for token in holders] == [legacy.lease_id, current.lease_id]
    assert not waiters
    assert store.renew(loaded) and store.renew(current)
    store.release(loaded)
    store.release(current)
    assert store.snapshot(CLASS_MEMORY) == ([], [])


class TestTwoProcessRaces:
    """Simulated interleavings of two claimant processes over one directory."""

    def test_an_earlier_claim_arriving_inside_the_gate_is_not_overtaken(
        self, root, world, monkeypatch
    ):
        """Submission takes no gate, so the decision is repeated inside it."""
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        b = make_store(root, world)
        background = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100, role=job_slots.ROLE_BACKGROUND)
        arrivals: list[job_slots.SlotToken] = []
        # Interactive work overtakes the queued background claim after its first look.
        interleave(
            monkeypatch,
            "hardlink_to",
            lambda path: path.name == PROMOTION_GATE,
            lambda: arrivals.append(b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)),
        )
        assert a.refresh(background).state == QUEUED
        (interactive,) = arrivals
        assert b.refresh(interactive).state == HOLDING
        assert a.refresh(background).state == QUEUED


class TestGhostReaping:
    def test_expired_lease_claims_recovery_without_freeing_capacity(self, root, world):
        execution_id = "b" * 32
        terminal = False
        cancellations: list[str] = []
        spawn(world, 100)
        store = make_execution_store(
            root,
            world,
            execution_terminal=lambda _execution_id: terminal,
            cancellations=cancellations,
        )
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, execution_id=execution_id)
        assert store.refresh(token).state == HOLDING
        world.clock += job_slots.LEASE_DURATION_SECONDS + 1

        assert store.reap(CLASS_MEMORY) == []
        holder = store.snapshot(CLASS_MEMORY)[0][0]
        assert holder.lease_state == job_slots.LEASE_CANCELLING
        assert cancellations == [execution_id]
        assert store.renew(token) is False
        assert token.lease_health.lost.is_set()
        store.release(token)
        assert token.path.exists()

        terminal = True
        store.release(token)
        assert not token.path.exists()

    def test_execution_id_is_inherited_and_persisted(
        self, tmp_path, world, monkeypatch, booley_dir_cache
    ):
        execution_id = "c" * 32
        project = tmp_path / ".booley"
        root = project / "runtime" / "jobs" / "slots"
        spawn(world, 50)
        spawn(world, 100)
        supervisor = world.capture_identity(50)
        assert supervisor is not None
        atomic_write_json(
            execution_paths(execution_id, booley_dir=project).record,
            active_execution_record(supervisor),
        )
        monkeypatch.setenv("BOOLEY_DIR", str(project))
        monkeypatch.setenv("BOOLEY_RUNTIME_EXECUTION_ID", execution_id)
        verified_inheritance(monkeypatch, world, {(100, 50)})
        terminal = False
        cancellations: list[str] = []
        store = make_execution_store(
            root,
            world,
            execution_terminal=lambda _execution_id: terminal,
            cancellations=cancellations,
        )
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING

        holder = store.snapshot(CLASS_MEMORY)[0][0]
        assert holder.execution_id == execution_id
        assert holder.owner_kind == "execution"
        assert holder.lease_id
        assert holder.lease_expires_at is not None

        del world.alive[100]
        assert store.reap(CLASS_MEMORY) == []
        assert cancellations == [execution_id]
        assert store.snapshot(CLASS_MEMORY)[0][0].lease_state == job_slots.LEASE_CANCELLING

        terminal = True
        assert store.reap(CLASS_MEMORY) == [token.path.name]

    def test_one_runtime_execution_can_own_multiple_job_leases(self, tmp_path, world, monkeypatch):
        execution_id = "e" * 32
        project = tmp_path / ".booley"
        root = project / "runtime" / "jobs" / "slots"
        monkeypatch.setenv("BOOLEY_RUNTIME_EXECUTION_ID", execution_id)
        spawn(world, 50)
        spawn(world, 100)
        supervisor = world.capture_identity(50)
        assert supervisor is not None
        atomic_write_json(
            execution_paths(execution_id, booley_dir=project).record,
            active_execution_record(supervisor),
        )
        store = make_execution_store(
            root,
            world,
            execution_terminal=lambda _execution_id: False,
            cancellations=[],
            inherited_execution_verifier=lambda raw, _root, pid: (
                execution_records.verify_inherited_execution_id(
                    raw,
                    booley_dir=project,
                    owner_pid=pid,
                    observe_identity=world.observe_identity,
                    ancestor_check=lambda owner, ancestor: (owner, ancestor) == (100, 50),
                )
            ),
        )

        first = store.submit(CLASS_MEMORY, pid=100, memory=_ONE_MIB_CLAIM)
        second = store.submit(CLASS_MEMORY, pid=100, memory=_ONE_MIB_CLAIM)
        assert store.refresh(first).state == HOLDING
        assert store.refresh(second).state == HOLDING

        holders = store.snapshot(CLASS_MEMORY)[0]
        assert {token.execution_id for token in holders} == {execution_id}
        assert len({token.lease_id for token in holders}) == 2

    @pytest.mark.parametrize(
        "case",
        ["foreign-project", "non-ancestor", "project-unresolvable", "noncanonical-root", "none"],
    )
    def test_inherited_execution_id_is_refused(
        self, tmp_path, world, monkeypatch, booley_dir_cache, case
    ):
        execution_id = "7" * 32
        project = tmp_path / "current" / ".booley"
        project.mkdir(parents=True)
        root = (
            tmp_path / "override-slots"
            if case == "noncanonical-root"
            else (project / "runtime" / "jobs" / "slots")
        )
        record_project = tmp_path / "foreign" / ".booley" if case == "foreign-project" else project
        record_project.parent.mkdir(exist_ok=True)
        spawn(world, 50, ["supervisor"])
        spawn(world, 100, ["owner"])
        spawn(world, 200, ["waiter"])
        supervisor = world.capture_identity(50)
        assert supervisor is not None
        atomic_write_json(
            execution_paths(execution_id, booley_dir=record_project).record,
            active_execution_record(supervisor),
        )
        if case == "project-unresolvable":
            monkeypatch.delenv("BOOLEY_DIR", raising=False)
        else:
            monkeypatch.setenv("BOOLEY_DIR", str(project))
        if case == "none":
            monkeypatch.delenv(RUNTIME_EXECUTION_ENV, raising=False)
        else:
            monkeypatch.setenv(RUNTIME_EXECUTION_ENV, execution_id)
        # Ancestry holds for every case but the owner that is no descendant.
        verified_inheritance(monkeypatch, world, set() if case == "non-ancestor" else {(100, 50)})
        if case == "project-unresolvable":
            # No file-level setup makes project resolution fail; the patch is the only route.

            def fail_project_resolution():
                raise RuntimeError("project unavailable")

            monkeypatch.setattr(runtime_booley_dir, "resolve_booley_dir", fail_project_resolution)
        cancellations: list[str] = []
        store = make_store(
            root,
            world,
            recovery=job_slots.SlotRecovery(
                execution_is_terminal=lambda _execution_id: False,
                cancel_execution=cancellations.append,
                recover_execution=lambda _execution_id: False,
                recover_process_owner=lambda _identity: True,
            ),
        )
        owner = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, argv=["owner"])
        assert store.refresh(owner).state == HOLDING
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=200, argv=["waiter"])
        assert store.refresh(waiter).state == QUEUED

        assert owner.execution_id is None
        assert owner.owner_kind == "process"
        del world.alive[100]
        assert store.refresh(waiter).state == HOLDING
        assert not owner.path.exists()
        assert cancellations == []

    @pytest.mark.parametrize("owner", ["execution", "process"])
    def test_promotion_metadata_missing_falls_back_to_the_lease_deadline(self, root, world, owner):
        execution_id = "d" * 32 if owner == "execution" else None
        cancellations: list[str] = []
        spawn(world, 100)
        if owner == "process":
            store = make_store(root, world)
        else:
            store = make_execution_store(
                root,
                world,
                execution_terminal=lambda _execution_id: False,
                cancellations=cancellations,
            )
        token = store.submit(
            CLASS_MEMORY, memory=_ALONE, pid=100, timeout_s=None, execution_id=execution_id
        )
        assert store.refresh(token).state == HOLDING
        payload = json.loads(token.path.read_text(encoding="utf-8"))
        for key in ("promoted_at", "lease_expires_at"):
            payload.pop(key)
        token.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        world.clock += job_slots.LEASE_DURATION_SECONDS + 1

        if owner == "process":
            assert store.reap(CLASS_MEMORY) == [token.path.name]
            return
        assert store.reap(CLASS_MEMORY) == []
        assert cancellations == [execution_id]
        assert store.snapshot(CLASS_MEMORY)[0][0].lease_state == job_slots.LEASE_CANCELLING

    @pytest.mark.skipif(sys.platform != "linux", reason="recovery scans Linux /proc")
    @pytest.mark.parametrize("owner", ["execution-missing", "execution-unrecoverable", "process"])
    def test_expired_lease_recovers_only_its_owner(
        self, tmp_path, monkeypatch, booley_dir_cache, owner
    ):
        project = tmp_path / ".booley"
        project.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(project))
        execution = owner != "process"
        # Unique: real recovery signals every host process carrying this ID (xdist workers too).
        execution_id = uuid.uuid4().hex
        clean_env = {
            key: value for key, value in os.environ.items() if key != RUNTIME_EXECUTION_ENV
        }
        sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
        process = subprocess.Popen(
            sleeper,
            env={**clean_env, RUNTIME_EXECUTION_ENV: execution_id} if execution else clean_env,
        )
        unrelated = subprocess.Popen(sleeper, env=clean_env)
        store = SlotStore(project / "runtime" / "jobs" / "slots")
        claim = {"execution_id": execution_id} if execution else {"timeout_s": None}
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=process.pid, **claim)
        assert store.refresh(token).state == HOLDING
        paths = execution_paths(execution_id, booley_dir=project)
        if owner == "execution-unrecoverable":
            atomic_write_json(
                paths.record,
                {
                    "schema_version": PROTOCOL_VERSION,
                    "state": "unrecoverable",
                    "tree_terminal": False,
                },
            )
        edit_entry(token, lease_expires_at="1970-01-01T00:00:00Z")
        try:
            assert store.reap(CLASS_MEMORY) == [token.path.name]
            assert process.wait(timeout=hang_guard_s()) != 0
            assert unrelated.poll() is None
            if execution:
                terminal = read_json(paths.record)
                assert terminal["state"] == "terminal"
                assert terminal["tree_terminal"] is True
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=hang_guard_s())
            unrelated.kill()
            unrelated.wait(timeout=hang_guard_s())


class TestAcquire:
    def test_acquire_blocks_until_slot_frees(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert a.refresh(holder).state == HOLDING

        positions: list[int] = []
        released = {"done": False}

        def sleep_then_release(_seconds):
            if not released["done"]:
                a.release(holder)
                released["done"] = True

        b = make_store(root, world, sleep=sleep_then_release)
        tok = b.acquire(
            CLASS_MEMORY, memory=_ALONE, pid=200, poll_interval=0.01, on_queued=positions.append
        )
        assert tok.is_holder
        assert positions == [0]

    def test_narration_repeats_while_position_is_unchanged(self, root, world):
        """A stuck queue must keep saying so, not narrate once and go quiet (F-27)."""
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert a.refresh(holder).state == HOLDING

        positions: list[int] = []
        polls = {"n": 0}

        def tick(_seconds):
            polls["n"] += 1
            world.clock += 10  # 10s per poll
            a.renew(holder)
            if polls["n"] >= 8:  # 80s of waiting, then let it through
                a.release(holder)

        b = make_store(root, world, sleep=tick)
        b.acquire(
            CLASS_MEMORY,
            memory=_ALONE,
            pid=200,
            on_queued=positions.append,
            narrate_interval_s=30.0,
        )

        # Position never moved, so without the periodic repeat this would be [0].
        assert positions == [0, 0, 0]

    def test_acquire_raises_when_entry_is_cancelled(self, root, world):
        # A live waiter's entry only vanishes via deliberate cancellation
        # (booley_cancel); re-queueing would undo it, so acquire must raise.
        spawn(world, 100)
        a = make_store(root, world)
        with pytest.raises(job_slots.ClaimLostError):
            a.acquire(
                CLASS_MEMORY,
                memory=_ALONE,
                pid=100,
                poll_interval=0.01,
                on_submitted=a.cancel_waiter_token,
            )


class TestSnapshot:
    def test_snapshot_orders_holders_then_waiters(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, argv=["sim"])
        assert store.refresh(holder).state == HOLDING
        store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, argv=["synth"], role="background")
        holders, waiters = store.snapshot(CLASS_MEMORY)
        assert [t.argv for t in holders] == [["sim"]]
        assert [t.argv for t in waiters] == [["synth"]]
        assert waiters[0].role == "background"


def edit_entry(token: job_slots.SlotToken, **changes: object) -> None:
    """Rewrite fields of a slot entry's JSON in place, as a foreign or future writer might."""
    payload = json.loads(read_file_bytes(token.path))
    payload.update(changes)
    write_file_text(token.path, json.dumps(payload))


def _holder(store, world, **kwargs) -> job_slots.SlotToken:
    token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, **kwargs)
    store.refresh(token)
    return token


def _dead_owner(**changes):
    """A holder whose process died, its entry edited by *changes*."""

    def setup(store, world, _root, _terminal):
        token = _holder(store, world)
        if changes:
            edit_entry(token, **changes)
        del world.alive[100]
        return token.path

    return setup


def _stray(name: str, age_s: float):
    """A file that is not a readable entry, last written *age_s* before the simulated now."""

    def setup(_store, world, root, _terminal):
        directory = root / CLASS_MEMORY
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        write_file_text(path, "{not json")
        old = world.clock - age_s
        os.utime(path, (old, old))
        return path

    return setup


def _dead_execution_owner(store, world, _root, _terminal):
    token = _holder(store, world, execution_id="a" * 32)
    del world.alive[100]
    return token.path


def _dead_waiter(store, world, _root, _terminal):
    spawn(world, 200)
    holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
    assert store.refresh(holder).state == HOLDING
    ghost = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
    del world.alive[100]
    return ghost.path


def _recycled_pid(store, world, _root, _terminal):
    spawn(world, 100, ["python", "-m", "booley.flows.sim"])
    token = _holder(store, world, argv=["python", "-m", "booley.flows.sim"])
    world.alive[100] = ["sshd"]  # PID 100 is recycled by an unrelated process
    return token.path


def _unknown_cmdline(store, world, _root, _terminal):
    spawn(world, 100, None)  # /proc unavailable: cannot judge
    return _holder(store, world, argv=["python", "-m", "x"]).path


def _terminal_execution(store, world, _root, terminal):
    token = _holder(store, world, execution_id="b" * 32)
    terminal["value"] = True
    return token.path


def _holder_past_lease(store, world, _root, _terminal):
    token = _holder(store, world)
    world.clock += job_slots.LEASE_DURATION_SECONDS + 1
    return token.path


_ENTRY_NAME = "h-p0-s0000000001-0000000100-0001.json"  # the persisted name format
FUTURE_SCHEMA = job_slots.SLOT_SCHEMA_VERSION + 1

# id: (setup, reaped, executions cancelled). A reap removes an entry only when its
# owner is provably gone; every other row survives it.
REAP_ROWS = {
    "live-holder": (lambda store, world, _r, _t: _holder(store, world).path, False, []),
    "dead-process-owner": (_dead_owner(), True, []),
    "dead-waiter": (_dead_waiter, True, []),
    "recycled-pid-argv": (_recycled_pid, True, []),
    "unknown-cmdline": (_unknown_cmdline, False, []),
    "dead-owner-recovery-fails": (_dead_execution_owner, False, ["a" * 32]),
    "terminal-execution": (_terminal_execution, True, []),
    "execution-without-id": (_dead_owner(owner_kind="execution", execution_id=None), False, []),
    "future-schema": (_dead_owner(schema_version=FUTURE_SCHEMA), False, []),
    "future-string-schema": (_dead_owner(schema_version="future"), False, []),
    "corrupt-young": (_stray(_ENTRY_NAME, 0), False, []),
    "corrupt-old": (_stray(_ENTRY_NAME, OLD_S), True, []),
    "junk-young": (_stray("not-an-entry.json", 0), False, []),
    "junk-old": (_stray("not-an-entry.json", OLD_S), True, []),
    "holder-past-lease-recovery-fails": (_holder_past_lease, False, []),
}


@pytest.mark.parametrize("row", sorted(REAP_ROWS))
def test_reap_removes_only_provably_stale_entries(root, world, row):
    setup, reaped, cancelled = REAP_ROWS[row]
    spawn(world, 100)
    terminal = {"value": False}
    cancellations: list[str] = []
    store = make_execution_store(
        root,
        world,
        execution_terminal=lambda _execution_id: terminal["value"],
        cancellations=cancellations,
    )
    path = setup(store, world, root, terminal)

    assert store.reap(CLASS_MEMORY) == ([path.name] if reaped else [])
    assert path.exists() is not reaped
    assert cancellations == cancelled


@pytest.mark.parametrize(
    ("age_s", "owner_alive", "reaped"),
    [(0, False, False), (OLD_S, False, True), (OLD_S, True, False)],
    ids=["young-dead-owner", "old-dead-owner", "old-live-owner"],
)
def test_an_unreadable_entry_is_reaped_only_when_old_and_its_owner_is_dead(
    root, world, monkeypatch, age_s, owner_alive, reaped
):
    # Unreadable is not missing: a live owner's entry survives the junk rule (#1905).
    spawn(world, 100)
    store = make_store(root, world)
    token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
    old = world.clock - age_s
    os.utime(token.path, (old, old))
    if not owner_alive:
        del world.alive[100]
    hold_opens(monkeypatch, {token.path: 10**6})

    assert store.reap(CLASS_MEMORY) == ([token.path.name] if reaped else [])


@pytest.mark.parametrize("broken", ["directory", "bytes"])
def test_job_budget_config_read_failure(tmp_path, monkeypatch, caplog, booley_dir_cache, broken):
    from booley.runtime import shared_infra

    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))
    # private: the CWD config cache has no public reset; without this an earlier test's config answers.
    monkeypatch.setattr(shared_infra, "_TOML_CACHE", None)
    config = tmp_path / "booley.toml"
    if broken == "directory":
        config.mkdir()  # an OSError, not a missing file
        assert shared_infra.load_job_budget_config() == {}
        assert "Failed to load the shared Project job budget" in caplog.text
    else:
        config.write_bytes(b"\xff\xfe")  # not UTF-8: no handler turns it into a warning
        with pytest.raises(UnicodeDecodeError):
            shared_infra.load_job_budget_config()


class TestPromotionDeadline:
    """Holder deadlines anchor at PROMOTION, never at entry creation.

    Queue wait must not count against the run budget: an entry can
    legitimately queue for longer than its own timeout_s, and charging that
    wait would reap a live holder — freeing an occupied slot (overcommit).
    """

    def test_promotion_stamps_promoted_at(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, timeout_s=100)
        assert tok.promoted_at is None
        assert store.refresh(tok).state == HOLDING
        assert tok.promoted_at is not None
        # The stamp is durable: another store instance reads it back.
        reader = make_store(root, world)
        holders, _waiters = reader.snapshot(CLASS_MEMORY)
        assert holders[0].promoted_at == tok.promoted_at

    def test_queue_wait_does_not_count_against_holder_deadline(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        store = make_store(root, world)
        holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(holder).state == HOLDING
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=200, timeout_s=100)
        assert store.refresh(waiter).state == QUEUED

        # Queue far past the waiter's own budget + slack: waiters have no
        # deadline, and the wait must not pre-charge the future holder.
        advance_renewing(world, store, holder, 1_000)
        assert store.reap(CLASS_MEMORY) == []
        store.release(holder)
        assert store.refresh(waiter).state == HOLDING

        # Freshly promoted: inside its (promotion-anchored) budget.
        advance_renewing(world, store, waiter, 100)
        assert store.reap(CLASS_MEMORY) == []
        assert waiter.path.exists()

        # Now genuinely past promotion + budget + slack: reaped.
        world.clock += job_slots.DEADLINE_SLACK_SECONDS + 1
        assert store.reap(CLASS_MEMORY) == [waiter.path.name]


@pytest.mark.parametrize("failure", ["should-abort", "narration-raises"])
def test_failed_wait_withdraws_the_entry(root, world, failure):
    # Shutdown hook (the Runner's Ctrl+C path) or a narration hook blowing up: either must
    # withdraw our own entry, since a claim without a caller-held token is unreleasable
    # and, with the process alive, unreapable.
    spawn(world, 100)
    spawn(world, 200)
    store = make_store(root, world)
    holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
    assert store.refresh(holder).state == HOLDING
    aborts = iter([False, True])

    def explode(_position):
        raise RuntimeError("narration boom")

    if failure == "should-abort":
        expected, kwargs = job_slots.ClaimAbortedError, {"should_abort": lambda: next(aborts)}
    else:
        expected, kwargs = RuntimeError, {"on_queued": explode}
    with pytest.raises(expected):
        store.acquire(CLASS_MEMORY, memory=_ALONE, pid=200, **kwargs)
    holders, waiters = store.snapshot(CLASS_MEMORY)
    assert [token.lease_id for token in holders] == [holder.lease_id]
    assert waiters == []


class TestSlotsDir:
    """Where the store lives. `.booley/runtime/` here is DOTLESS on
    purpose (init_cmd F-6): `.runtime/` is the multi-GB EDA scratch tree,
    `runtime/` is container-lifetime bookkeeping (doctor stamp, developer
    probe, slot store) that must survive a scratch wipe. Nothing else pinned
    this path, so "deduping" the two would have gone green."""

    def test_is_project_scoped_and_dotless(self, tmp_path, monkeypatch):
        from booley.runtime.booley_dir import reset_cache

        monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
        monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))
        reset_cache()
        try:
            assert job_slots.slots_dir() == tmp_path / "runtime" / "jobs" / "slots"
        finally:
            reset_cache()

    def test_env_override_wins_and_resolving_it_creates_nothing(self, tmp_path, monkeypatch):
        # Readers (mcp_server._job_phase) call this on hot paths; it must not
        # have runtime_dir()'s mkdir side effect.
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "elsewhere"))
        assert job_slots.slots_dir() == tmp_path / "elsewhere"
        assert not (tmp_path / "elsewhere").exists()


@pytest.mark.parametrize("retired", ["heavy", "light"])
def test_retired_class_holders_are_outside_admission(tmp_path, world, retired):
    """Entries an older version left in a retired class directory are never admitted or withdrawn."""
    root = tmp_path / "slots"
    for pid in (100, 200, 300):
        spawn(world, pid)
    store = make_store(root, world)
    alone = job_slots.MemoryClaim(budget_mb=1024, run_alone=True)
    holder = store.submit(CLASS_MEMORY, pid=100, memory=alone)
    waiter = store.submit(CLASS_MEMORY, pid=200, memory=alone)
    assert store.refresh(holder).state == HOLDING
    assert store.refresh(waiter).state == QUEUED
    (root / CLASS_MEMORY).rename(root / retired)
    reloaded = make_store(root, world)
    assert reloaded.state_for_pid(100) is None
    assert reloaded.state_for_pid(200) is None
    assert reloaded.cancel_waiter(200) is False
    assert waiter.path.name in {p.name for p in (root / retired).iterdir()}
    fresh = tmp_path / "fresh"
    with pytest.raises(ValueError):
        make_store(fresh, world).submit(retired, pid=300)
    assert not (fresh / retired).exists()


def test_retired_class_directory_is_untouched_by_live_classes(tmp_path, world):
    """Old claims are outside the memory admission and cancellation surface."""
    roots = [tmp_path / "clean" / "slots", tmp_path / "legacy" / "slots"]
    legacy = roots[1] / "ticket"
    legacy.mkdir(parents=True)
    (legacy / "H-000-00001-100-0.json").write_bytes(b'{"role":"ticket","pid":100}\n')
    (legacy / "opaque.bin").write_bytes(b"\x00legacy claim\xff")
    before = {p.relative_to(legacy): p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
    for pid in (100, 200, 300):
        spawn(world, pid)
    results = []
    for root in roots:
        store = make_store(root, world)
        first = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        memory = store.submit(CLASS_MEMORY, pid=200, memory=_ONE_MIB_CLAIM)
        states = [store.refresh(first).state, store.refresh(memory).state]
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=300)
        reloaded = make_store(root, world)
        holders, waiting = reloaded.snapshot(CLASS_MEMORY)
        states += [(t.pid, t.role) for t in holders + waiting]
        states += [
            reloaded.state_for_pid(100),
            reloaded.state_for_pid(200),
            reloaded.state_for_pid(300),
        ]
        states += [reloaded.cancel_waiter(300), reloaded.refresh(waiter).state]
        results.append(states)
    assert results[0] == results[1]
    assert {
        p.relative_to(legacy): p.read_bytes() for p in legacy.rglob("*") if p.is_file()
    } == before


def test_persisted_legacy_role_reloads_behind_interactive(root, world):
    for pid in (100, 200, 300):
        spawn(world, pid)
    store = make_store(root, world)
    holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, argv=["sim"])
    assert store.refresh(holder).state == HOLDING
    legacy = store.submit(CLASS_MEMORY, memory=_ALONE, pid=200, role="ticket", argv=["synth"])
    interactive = store.submit(
        CLASS_MEMORY, memory=_ALONE, pid=300, role=ROLE_INTERACTIVE, argv=["lint"]
    )
    reloaded = make_store(root, world)
    holders, waiting = reloaded.snapshot(CLASS_MEMORY)
    assert [(t.pid, t.argv) for t in holders] == [(100, ["sim"])]
    assert [(t.pid, t.role, t.argv) for t in waiting] == [
        (interactive.pid, ROLE_INTERACTIVE, ["lint"]),
        (legacy.pid, "ticket", ["synth"]),
    ]


def test_default_recovery_keeps_a_cancelling_holder_while_its_record_is_unreadable(
    tmp_path, monkeypatch, booley_dir_cache
) -> None:
    """A lasting sharing error keeps the slot: release() must never raise."""
    project = tmp_path / ".booley"
    project.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(project))
    execution_id = "f" * 32
    store = SlotStore(project / "runtime" / "jobs" / "slots")
    token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=os.getpid(), execution_id=execution_id)
    assert store.refresh(token).state == HOLDING
    assert store.begin_token_recovery(token) is True  # the default cancel writes the real request
    record = execution_paths(execution_id, booley_dir=project).record
    atomic_write_json(
        record, {"schema_version": PROTOCOL_VERSION, "state": "terminal", "tree_terminal": True}
    )
    hold_opens(monkeypatch, {record: 10**6})

    store.release(token)

    assert token.path.exists()


class TestStaleGateBreaking:
    """Stale gates: who may break one, and the process a gate names."""

    def test_unusable_break_lock_is_reported_once(self, root, world, caplog):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        gate = root / CLASS_MEMORY / PROMOTION_GATE
        gate.write_text(f"{_DEAD_GATE_PID}\n", encoding="utf-8")
        world.clock = gate.stat().st_mtime + OLD_S + 1
        (root / CLASS_MEMORY / ".gate-break.lock").mkdir()  # cannot be opened as a file

        with caplog.at_level(logging.WARNING, logger="booley.runtime.job_slots"):
            for _attempt in range(2):
                assert store.refresh(token).state == QUEUED
        assert gate.exists()
        assert caplog.text.count("Cannot take the gate break lock") == 1

    def test_lease_gate_names_the_current_process(self, root, world, monkeypatch):
        current = [500]
        store = make_store(root, world, getpid=lambda: current[0])
        spawn(world, 100)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING

        def gate_during_renew() -> dict:
            """The lease gate's text, read while a renewal holds the gate."""
            seen: list[dict] = []
            interleave(
                monkeypatch,
                "replace",
                is_rewrite_tmp,
                lambda: seen.append(json.loads(read_file_bytes(lease_gate(token)))),
            )
            assert store.renew(token)
            return seen[0]

        # This process is not in the fake world yet: the gate names it, with no identity.
        assert gate_during_renew() == {"pid": 500, "owner_identity": None}
        # Once it is, the next renewal retries the failed capture.
        spawn(world, 500)
        named = gate_during_renew()
        assert named["pid"] == 500 and named["owner_identity"] is not None
        # After a fork the child's PID is the one the gate names.
        current[0] = 501
        spawn(world, 501)
        forked = gate_during_renew()
        assert forked["pid"] == 501 and forked["owner_identity"] is not None


class TestLeaseRenewal:
    @pytest.mark.parametrize(
        ("elapsed", "lost"), [(10, False), (job_slots.LEASE_DURATION_SECONDS + 1, True)]
    )
    def test_missed_lease_gate_is_lost_only_after_expiry(self, root, world, elapsed, lost):
        spawn(world, 100)
        spawn(world, 300)  # the process holding the other gate
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        world.clock += elapsed
        with held_gate(lease_gate(token), 300, world.capture_identity(300)):
            assert store.renew(token) is False  # not extended this time
        assert token.lease_health.lost.is_set() is lost
        assert store.renew(token) is not lost
        assert token.lease_generation == (0 if lost else 1)

    def test_dead_reapers_lease_gate_does_not_cost_the_holder_its_lease(self, root, world):
        spawn(world, 100)
        spawn(world, 300)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        with held_gate(lease_gate(token), 300, world.capture_identity(300)):
            del world.alive[300]  # the reaper dies holding the gate; no time passes

            assert store.renew(token) is True
        assert not token.lease_health.lost.is_set()

    def test_holder_resuming_after_expiry_reports_the_lease_lost(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        world.clock += job_slots.LEASE_DURATION_SECONDS + 1  # stalled; no reaper ran yet

        assert store.renew(token) is False
        assert token.lease_health.lost.is_set()
        assert store.snapshot(CLASS_MEMORY) == ([], [])  # the next reap recovers it
