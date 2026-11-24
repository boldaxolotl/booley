"""Tests for the shared on-disk slot store (ADR 0028 job admission).

The store is pure + injectable, so multi-process interleavings are simulated
deterministically: each simulated process is its own SlotStore over the same
directory, with its own fake PID, and the test controls which PIDs are
"alive" and what /proc reports for them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from booley.runtime import booley_dir as runtime_booley_dir
from booley.runtime import execution_records, job_slots
from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    RUNTIME_EXECUTION_ENV,
    atomic_write_json,
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
from tests.sharing_error_support import hold_unlinks
from tests.time_budget import hang_guard_s, scaled


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


_ONE_MIB_CLAIM = job_slots.MemoryClaim(budget_mb=1024, reservation_mb=1)
# One holder at a time: a second claim waits behind it.
_ALONE = job_slots.MemoryClaim(budget_mb=1024, run_alone=True)
# Two claims hold together; a third waits.
_HALF = job_slots.MemoryClaim(budget_mb=2, reservation_mb=1)


def make_store(root: Path, world: FakeWorld) -> SlotStore:
    """A simulated process. Its lease gates name the real test process: a test that
    holds a lease gate while another store looks at it spawns ``os.getpid()``."""
    return SlotStore(
        root,
        is_pid_alive=world.is_pid_alive,
        read_cmdline=world.read_cmdline,
        now=world.now,
        sleep=lambda s: None,
        capture_identity=world.capture_identity,
        observe_identity=world.observe_identity,
        recovery=job_slots.SlotRecovery(recover_process_owner=lambda _identity: True),
    )


def make_execution_store(
    root: Path,
    world: FakeWorld,
    *,
    execution_terminal,
    cancellations: list[str],
    inherited_execution_verifier=None,
) -> SlotStore:
    kwargs = {}
    if inherited_execution_verifier is not None:
        kwargs["inherited_execution_verifier"] = inherited_execution_verifier
    return SlotStore(
        root,
        is_pid_alive=world.is_pid_alive,
        read_cmdline=world.read_cmdline,
        now=world.now,
        sleep=lambda _seconds: None,
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


class TestSingleProcess:
    def test_first_claim_holds_immediately(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, role=ROLE_INTERACTIVE)
        assert store.refresh(tok).state == HOLDING
        assert tok.is_holder

    def test_acquire_renews_production_lease_until_release(self, root, monkeypatch):
        monkeypatch.setattr(job_slots, "LEASE_DURATION_SECONDS", scaled(2.0))
        monkeypatch.setattr(job_slots, "LEASE_RENEW_INTERVAL_SECONDS", 0.02)
        store = SlotStore(root)
        token = store.acquire(CLASS_MEMORY, memory=_ALONE, pid=os.getpid(), poll_interval=0.01)
        try:
            deadline = time.monotonic() + hang_guard_s()
            while token.lease_generation < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            holder = store.snapshot(CLASS_MEMORY)[0][0]
            assert holder.lease_generation >= 2
            assert holder.lease_state == job_slots.LEASE_ACTIVE
        finally:
            store.release(token)

    def test_renew_retries_transient_permission_error(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        original_replace = Path.replace
        attempts = 0

        def flaky_replace(path, target):
            nonlocal attempts
            if target == token.path and attempts < 2:
                attempts += 1
                raise windows_sharing_error()
            attempts += 1
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", flaky_replace)

        assert store.renew(token) is True
        assert attempts == 3
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

    def test_live_holder_survives_transient_read_permission_error(self, root, world, monkeypatch):
        # A reader racing a rewrite on Windows must not see the lease as gone,
        # or the run is reported cancelled (#2024).
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        original_open = Path.open
        denials = 0

        def flaky_open(path, *args, **kwargs):
            nonlocal denials
            if path == token.path and denials < 2:
                denials += 1
                raise windows_sharing_error()
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", flaky_open)

        assert store.token_absent(token) is False
        assert denials == 2

    def test_unreadable_live_entry_is_not_absent(self, root, world, monkeypatch):
        # A lock outlasting every retry still must not read as cancellation (#2024).
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        original_open = Path.open

        def locked_open(path, *args, **kwargs):
            if path == token.path:
                raise windows_sharing_error()
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", locked_open)

        assert store.token_absent(token) is False
        monkeypatch.setattr(Path, "open", original_open)
        store.release(token)
        assert store.token_absent(token) is True

    def test_renew_survives_windows_read_sharing_error_within_budget(
        self, root, world, monkeypatch
    ):
        # Longer than the old private 0.2 s loop, inside the shared helper's budget (#1905).
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        clock = BackoffClock()
        store._sleep = clock.sleep
        deny_until(monkeypatch, "open", token.path, clock, seconds=0.5)

        assert store.renew(token) is True
        assert not token.lease_health.lost.is_set()
        assert store.token_absent(token) is False

    def test_renew_survives_windows_rewrite_sharing_error_within_budget(
        self, root, world, monkeypatch
    ):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        clock = BackoffClock()
        store._sleep = clock.sleep
        deny_until(monkeypatch, "replace", token.path, clock, seconds=0.5)

        assert store.renew(token) is True
        assert not token.lease_health.lost.is_set()
        assert store.snapshot(CLASS_MEMORY)[0][0].lease_generation == 1

    @staticmethod
    def _lock_reads(monkeypatch, target: Path) -> None:
        """Make *target* permanently unreadable (a scanner holds it)."""
        original_open = Path.open

        def locked_open(path, *args, **kwargs):
            if path == target:
                raise PermissionError(13, "held by a scanner")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", locked_open)

    def test_unreadable_entry_is_not_treated_as_a_live_lease(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        token.execution_id = "a" * 32  # recovery only claims execution-bound entries
        self._lock_reads(monkeypatch, token.path)

        assert store.renew(token) is False  # unreadable past the budget: lease lost
        assert token.lease_health.lost.is_set()
        assert store._begin_execution_recovery(token, request_cancel=False) is False
        store.release(token)  # still our own entry to release
        assert not token.path.exists()

    def test_unreadable_waiter_is_not_withdrawn(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        self._lock_reads(monkeypatch, waiter.path)

        assert store.cancel_waiter_token(waiter) is False
        assert waiter.path.exists()

    @staticmethod
    def _queued_waiter(root, world):
        """A store with a holder and one queued waiter whose backoff sleeps are simulated."""
        spawn(world, 100)
        spawn(world, 101)
        store = make_store(root, world)
        holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(holder).state == HOLDING  # so the next entry stays queued
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=101)
        clock = BackoffClock()
        store._sleep = clock.sleep
        return store, waiter, clock

    @staticmethod
    def _withdraw(store, waiter, withdraw: str) -> bool:
        if withdraw == "pid":
            return store.cancel_waiter(waiter.pid)
        return store.cancel_waiter_token(waiter)

    @pytest.mark.parametrize("withdraw", ["pid", "token"])
    def test_withdrawal_retries_windows_sharing_error(self, root, world, monkeypatch, withdraw):
        store, waiter, clock = self._queued_waiter(root, world)
        deny_until(monkeypatch, "unlink", waiter.path, clock, seconds=0.005)

        assert self._withdraw(store, waiter, withdraw) is True
        assert not waiter.path.exists()

    @pytest.mark.parametrize("withdraw", ["pid", "token"])
    def test_withdrawal_refused_when_sharing_error_persists(
        self, root, world, monkeypatch, caplog, withdraw
    ):
        store, waiter, clock = self._queued_waiter(root, world)
        deny_until(monkeypatch, "unlink", waiter.path, clock, seconds=1000.0)

        with caplog.at_level(logging.WARNING, logger="booley.runtime.job_slots"):
            assert self._withdraw(store, waiter, withdraw) is False
        assert waiter.path.exists()
        assert [t.lease_id for t in store.snapshot(CLASS_MEMORY)[1]] == [waiter.lease_id]
        assert "Refusing to withdraw" in caplog.text
        assert waiter.path.name in caplog.text

    def test_renew_fails_when_rewrite_stays_denied(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        clock = BackoffClock()
        store._sleep = clock.sleep
        deny_until(monkeypatch, "replace", token.path, clock, seconds=1000.0)

        assert store.renew(token) is False
        assert token.lease_health.lost.is_set()

    def test_unreadable_old_waiter_is_not_reaped(self, root, world, monkeypatch):
        # Unreadable is not missing: a live owner's entry survives the junk rule,
        # a dead owner's does not (#1905).
        spawn(world, 100)
        store = make_store(root, world)
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        world.clock = waiter.path.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
        original_open = Path.open

        def locked_open(path, *args, **kwargs):
            if path == waiter.path:
                raise PermissionError(13, "held by a scanner")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", locked_open)

        assert store.reap(CLASS_MEMORY) == []
        assert waiter.path.exists()
        del world.alive[100]
        assert store.reap(CLASS_MEMORY) == [waiter.path.name]

    def test_renew_does_not_resurrect_a_deleted_holder(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        original_rewrite = store._rewrite_token

        def delete_before_rewrite(current):
            current.path.unlink()
            return original_rewrite(current)

        monkeypatch.setattr(store, "_rewrite_token", delete_before_rewrite)

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
        real_replace = job_slots.replace_file

        def cancel_then_replace(src, dst, **kwargs):
            if not cancelled:
                cancelled.append(canceller.cancel_waiter_token(waiter))
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(job_slots, "replace_file", cancel_then_replace)

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
        real_promote = store._promote

        def cancel_then_promote(token):
            cancelled.append(canceller.cancel_waiter_token(waiter))
            return real_promote(token)

        monkeypatch.setattr(store, "_promote", cancel_then_promote)

        assert store.refresh(waiter).state == LOST
        assert cancelled == [True]
        holders, waiters = store.observe(CLASS_MEMORY)
        assert [t for t in holders + waiters if t.lease_id == waiter.lease_id] == []

    def test_promotion_is_refused_while_a_cancel_holds_the_gate(self, root, world):
        spawn(world, 100)
        spawn(world, os.getpid())  # the process the lease gates name
        store = make_store(root, world)
        canceller = make_store(root, world)
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)

        with canceller._lease_gate(waiter) as held:
            assert held
            state = store.refresh(waiter)

        assert state.state == QUEUED
        assert not waiter.is_holder
        holders, waiters = store.observe(CLASS_MEMORY)
        assert holders == []
        assert [t.lease_id for t in waiters] == [waiter.lease_id]

    def test_promotion_of_an_unreadable_waiter_stays_queued(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        real_promote = store._promote

        def unreadable_promote(token):
            def unreadable(_path):
                raise PermissionError("entry is locked")

            monkeypatch.setattr(store, "_load_token", unreadable)
            return real_promote(token)

        monkeypatch.setattr(store, "_promote", unreadable_promote)

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
            root,
            world,
            execution_terminal=lambda _execution_id: False,
            cancellations=[],
        )
        waiter = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, execution_id="e" * 32)
        copy = store.observe(CLASS_MEMORY)[1][0]
        assert store.begin_token_recovery(copy) is True

        assert store.refresh(waiter).state == QUEUED

        holders, waiters = store.observe(CLASS_MEMORY)
        assert holders == []
        assert [t.lease_state for t in waiters] == [job_slots.LEASE_CANCELLING]

    def test_cancel_after_a_finished_promotion_spares_the_holder(self, root, world, monkeypatch):
        spawn(world, 100)
        owner = make_store(root, world)
        canceller = make_store(root, world)
        token = owner.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        real_gate = canceller._lease_gate
        promoted: list[bool] = []

        @contextlib.contextmanager
        def promote_while_waiting(tok):
            if not promoted:
                promoted.append(owner.refresh(token).state == HOLDING)
            with real_gate(tok) as acquired:
                yield acquired

        monkeypatch.setattr(canceller, "_lease_gate", promote_while_waiting)

        assert canceller.cancel_waiter_token(token) is False
        assert promoted == [True]
        assert token.path.exists()
        assert token.is_holder
        assert owner.renew(token) is True

    def _release_threads(self, token: job_slots.SlotToken) -> list[threading.Thread]:
        name = f"booley-slot-release-{token.lease_id[:8]}"
        return [t for t in threading.enumerate() if t.name == name]

    def test_release_retries_until_the_lease_gate_frees(self, root, world, caplog):
        spawn(world, 100)
        spawn(world, os.getpid())  # the process the lease gates name
        store = make_store(root, world)
        other = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        caplog.set_level(logging.WARNING, logger="booley.runtime.job_slots")
        gate_free = threading.Event()
        test_thread = threading.current_thread()

        def sleep(_seconds: float) -> None:
            if threading.current_thread() is not test_thread:
                gate_free.wait(timeout=2 * hang_guard_s())
                time.sleep(0.01)  # a real backoff: Windows delays a freed gate's rmdir

        store._sleep = sleep
        try:
            with other._lease_gate(token) as held:
                assert held
                store.release(token)
                assert token.path.exists()
        finally:
            gate_free.set()
        for thread in self._release_threads(token):
            thread.join(timeout=hang_guard_s())

        assert self._release_threads(token) == []
        assert not token.path.exists()
        assert any(
            "deferred" in r.getMessage() and token.path.name in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    def test_release_logs_when_the_lease_gate_never_frees(self, root, world, caplog):
        spawn(world, 100)
        spawn(world, os.getpid())  # the process the lease gates name
        store = make_store(root, world)
        other = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        caplog.set_level(logging.WARNING, logger="booley.runtime.job_slots")
        sleeps: list[float] = []
        store._sleep = sleeps.append

        with other._lease_gate(token) as held:
            assert held
            store.release(token)
            for thread in self._release_threads(token):
                thread.join(timeout=hang_guard_s())
            assert self._release_threads(token) == []
            assert token.path.exists()

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "Could not release Memory Reservation" in m and token.path.name in m for m in messages
        )
        assert sleeps.count(job_slots._RELEASE_RETRY_SECONDS) == job_slots._RELEASE_RETRY_ATTEMPTS

    def test_release_without_a_thread_logs_the_give_up(self, root, world, caplog, monkeypatch):
        spawn(world, 100)
        spawn(world, os.getpid())  # the process the lease gates name
        store = make_store(root, world)
        other = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        caplog.set_level(logging.WARNING, logger="booley.runtime.job_slots")

        def no_thread(self_thread):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", no_thread)
        with other._lease_gate(token):
            store.release(token)

        assert any(
            "Could not release Memory Reservation" in r.getMessage() for r in caplog.records
        )

    def test_release_of_a_removed_entry_starts_no_retry(self, root, world, caplog):
        spawn(world, 100)
        spawn(world, os.getpid())  # the process the lease gates name
        store = make_store(root, world)
        other = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        caplog.set_level(logging.WARNING, logger="booley.runtime.job_slots")

        with other._lease_gate(token):
            token.path.unlink()
            store.release(token)
            assert (token.path.parent / f".{token.lease_id}.lease.lock").exists()

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert self._release_threads(token) == []

    def test_release_frees_the_slot(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        a = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(a).state == HOLDING
        b = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(b).state == QUEUED
        holders, waiters = store.snapshot(CLASS_MEMORY)
        assert [token.lease_id for token in holders] == [a.lease_id]
        assert [token.lease_id for token in waiters] == [b.lease_id]
        store.release(a)
        holders, waiters = store.snapshot(CLASS_MEMORY)
        assert holders == []
        assert [token.lease_id for token in waiters] == [b.lease_id]
        assert store.refresh(b).state == HOLDING
        holders, waiters = store.snapshot(CLASS_MEMORY)
        assert [token.lease_id for token in holders] == [b.lease_id]
        assert waiters == []

    def test_release_is_idempotent(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        store.release(tok)
        store.release(tok)  # must not raise

    def test_release_retries_transient_permission_error(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        original_unlink = Path.unlink
        attempts = 0

        def flaky_unlink(path, *args, **kwargs):
            nonlocal attempts
            if path == tok.path:
                attempts += 1
                if attempts < 3:
                    raise PermissionError("file is temporarily in use")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)
        store.release(tok)

        assert attempts == 3
        assert not tok.path.exists()

    @staticmethod
    def _spy_on_entry_io_retry(store, monkeypatch) -> list[dict]:
        """Wrap the shared retry helper; record each invocation's callback, how
        often that callback ran, the ``Path.rename``/``Path.unlink`` calls made
        inside it, and its result."""
        real_retry = store._retry_entry_io
        invocations: list[dict] = []
        current: list[dict] = []

        def spy(operation, *, on_exhausted=None):
            record = {
                "on_exhausted": on_exhausted,
                "exhausted_runs": 0,
                "calls": [],
                "result": None,
            }

            def counted_on_exhausted():
                record["exhausted_runs"] += 1
                on_exhausted()

            invocations.append(record)
            current.append(record)
            try:
                record["result"] = real_retry(
                    operation,
                    on_exhausted=None if on_exhausted is None else counted_on_exhausted,
                )
            finally:
                current.pop()
            return record["result"]

        def recording(name):
            original = getattr(Path, name)

            def wrapper(path, *args, **kwargs):
                if current:
                    current[-1]["calls"].append((name, path))
                return original(path, *args, **kwargs)

            return wrapper

        monkeypatch.setattr(store, "_retry_entry_io", spy)
        monkeypatch.setattr(Path, "rename", recording("rename"))
        monkeypatch.setattr(Path, "unlink", recording("unlink"))
        return invocations

    def test_rename_and_unlink_share_entry_io_retry(self, root, world, monkeypatch):
        spawn(world, 100)
        store = make_store(root, world)
        invocations = self._spy_on_entry_io_retry(store, monkeypatch)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        waiter_path = tok.path
        assert store.refresh(tok).state == HOLDING
        holder_path = tok.path
        store.release(tok)

        renames = [i for i in invocations if ("rename", waiter_path) in i["calls"]]
        unlinks = [i for i in invocations if ("unlink", holder_path) in i["calls"]]
        assert len(renames) == 1
        assert renames[0]["on_exhausted"] is None
        assert len(unlinks) == 1
        assert unlinks[0]["on_exhausted"] is not None

    def test_unlink_warns_once_when_permission_error_outlasts_retries(
        self, root, world, monkeypatch, caplog
    ):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        original_unlink = Path.unlink

        def denied_unlink(path, *args, **kwargs):
            if path == tok.path:
                raise PermissionError("file is permanently in use")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", denied_unlink)
        invocations = self._spy_on_entry_io_retry(store, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="booley.runtime.job_slots"):
            store.release(tok)

        entry_unlinks = [i for i in invocations if ("unlink", tok.path) in i["calls"]]
        assert tok.path.exists()
        assert [i["result"] for i in entry_unlinks] == [False]
        assert [i["exhausted_runs"] for i in entry_unlinks] == [1]
        assert [r.getMessage() for r in caplog.records] == [
            f"Could not remove Memory Reservation after retries: {tok.path.name}"
        ]

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

    def test_promotion_gate_prevents_a_second_process_from_promoting(
        self, root, world, monkeypatch
    ):
        spawn(world, 100)
        spawn(world, 200)
        first = make_store(root, world)
        second = make_store(root, world)
        first_token = first.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        second_token = second.submit(CLASS_MEMORY, memory=_ALONE, pid=200)

        # Model the stale-rank interleaving directly: the second claimant had
        # already observed rank zero when the first claimant entered the gate.
        with monkeypatch.context() as patch:
            patch.setattr(second, "_queue_position", lambda _token: None)
            with first._promotion_gate(first_token) as acquired:
                assert acquired is True
                assert second.refresh(second_token).state == QUEUED

        assert first.refresh(first_token).state == HOLDING
        assert second.refresh(second_token).state == QUEUED

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

    def test_only_the_memory_class_admits_with_a_claim(self, root, world):
        store = make_store(root, world)
        with pytest.raises(ValueError, match="memory class admits"):
            store.submit(CLASS_MEMORY, pid=100)
        with pytest.raises(ValueError, match="memory class admits"):
            store.submit("warp", pid=100, memory=_ALONE)

    def test_unknown_class_rejected(self, root, world):
        store = make_store(root, world)
        with pytest.raises(ValueError):
            store.submit("warp", pid=100)


def test_same_store_interleaved_submission_preserves_exact_entry_number(root, world, monkeypatch):
    spawn(world, 100)
    store = make_store(root, world)
    # Both threads may select the same sequence before either entry is published.
    monkeypatch.setattr(store, "_next_seq", lambda _directory: 1)
    construct = store._new_waiter_token
    second = None
    interleaved = False

    def finish_after_other_submission(path, payload, request):
        nonlocal second, interleaved
        if not interleaved:
            interleaved = True
            second = store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
        return construct(path, payload, request)

    monkeypatch.setattr(store, "_new_waiter_token", finish_after_other_submission)
    first = store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    assert second is not None
    # Equal sequence and PID: the lease suffix orders the entries, and a waiter promotes behind it.
    for token in sorted((first, second), key=lambda t: t.path.name):
        assert store.refresh(token).state == HOLDING
    assert store.renew(first), "second promotion must not overwrite the first lease"
    assert first.path != second.path
    holders, waiters = store.snapshot(CLASS_MEMORY)
    assert {token.lease_id for token in holders} == {first.lease_id, second.lease_id}
    assert not waiters
    store.release(first)
    store.release(second)
    assert store.snapshot(CLASS_MEMORY) == ([], [])


@pytest.mark.parametrize("promote_before_publication", [False, True], ids=["waiter", "holder"])
def test_same_pid_stores_never_reuse_entry_identity(
    root, world, monkeypatch, promote_before_publication
):
    spawn(world, 100)
    first_store = make_store(root, world)
    second_store = make_store(root, world)
    monkeypatch.setattr(first_store, "_next_seq", lambda _directory: 1)
    monkeypatch.setattr(second_store, "_next_seq", lambda _directory: 1)
    publish = job_slots.os.link
    second = None
    interleaved = False

    def publish_after_other_submission(source, target):
        nonlocal second, interleaved
        if not interleaved:
            interleaved = True
            second = second_store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
            if promote_before_publication:
                assert second_store.refresh(second).state == HOLDING
        publish(source, target)

    monkeypatch.setattr(job_slots.os, "link", publish_after_other_submission)
    first = first_store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    assert second is not None
    for token, owner in sorted(
        ((first, first_store), (second, second_store)), key=lambda pair: pair[0].path.name
    ):
        assert owner.refresh(token).state == HOLDING
    assert first_store.renew(first) and second_store.renew(second)
    assert first.path != second.path
    first_store.release(first)
    second_store.release(second)
    assert first_store.snapshot(CLASS_MEMORY) == ([], [])


def test_cancelled_publication_never_deletes_replacement(root, world, monkeypatch):
    spawn(world, 100)
    first_store = make_store(root, world)
    replacement_store = make_store(root, world)
    for store in (first_store, replacement_store):
        monkeypatch.setattr(store, "_next_seq", lambda _directory: 1)
    publish = job_slots.os.link
    replacement = None
    interleaved = False

    def cancel_and_replace_after_publication(source, target):
        nonlocal replacement, interleaved
        publish(source, target)
        if not interleaved:
            interleaved = True
            assert first_store.cancel_waiter(100)
            replacement = replacement_store.submit(CLASS_MEMORY, memory=_HALF, pid=100)

    monkeypatch.setattr(job_slots.os, "link", cancel_and_replace_after_publication)
    cancelled = first_store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    assert first_store.refresh(cancelled).state == LOST
    assert replacement is not None and replacement.path != cancelled.path
    assert replacement_store.refresh(replacement).state == HOLDING
    assert replacement_store.renew(replacement)
    replacement_store.release(replacement)
    assert first_store.snapshot(CLASS_MEMORY) == ([], [])


def test_legacy_counter_names_coexist_with_lease_names(root, world, monkeypatch):
    spawn(world, 100)
    store = make_store(root, world)
    legacy = store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    old_path = legacy.path.with_name(
        job_slots._entry_name("w", legacy.priority, legacy.seq, legacy.pid, 1)
    )
    legacy.path.rename(old_path)
    legacy.path, legacy.n = old_path, 1
    loaded = store._load_token(old_path)
    assert loaded is not None and loaded.n == 1 and loaded.lease_id == legacy.lease_id
    monkeypatch.setattr(store, "_next_seq", lambda _directory: legacy.seq)
    current = store.submit(CLASS_MEMORY, memory=_HALF, pid=100)
    assert current.seq == legacy.seq
    assert job_slots._parse_entry_name(current.path.name)[4] == current.n
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

    def test_both_race_for_last_slot_only_one_wins(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        b = make_store(root, world)
        # Both submit before either refreshes — the classic race window.
        tok_a = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        tok_b = b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        state_a = a.refresh(tok_a)
        state_b = b.refresh(tok_b)
        states = sorted([state_a.state, state_b.state])
        assert states == [HOLDING, QUEUED]

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
        enter_gate = a._promotion_gate

        @contextlib.contextmanager
        def gate_with_an_arrival(token):
            # Interactive work overtakes the queued background claim after its first look.
            arrivals.append(b.submit(CLASS_MEMORY, memory=_ALONE, pid=200))
            with enter_gate(token) as acquired:
                yield acquired

        monkeypatch.setattr(a, "_promotion_gate", gate_with_an_arrival)
        assert a.refresh(background).state == QUEUED
        (interactive,) = arrivals
        monkeypatch.undo()
        assert b.refresh(interactive).state == HOLDING
        assert a.refresh(background).state == QUEUED

    def test_loser_promotes_after_winner_releases(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        b = make_store(root, world)
        tok_a = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        tok_b = b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
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
        tok_a = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        tok_b = b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        # Force identical seq by construction check: if they differ this
        # test still holds — the point is exactly one holder either way.
        assert (a.refresh(tok_a).state == HOLDING) != (b.refresh(tok_b).state == HOLDING)


class TestGhostReaping:
    def test_dead_pid_holder_is_reaped_and_slot_recovered(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        dead_tok = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert a.refresh(dead_tok).state == HOLDING
        del world.alive[100]  # process 100 dies holding the slot
        b = make_store(root, world)
        tok = b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        assert b.refresh(tok).state == HOLDING

    def test_reap_leaves_entry_for_a_later_reap_when_sharing_violation_outlasts_retries(
        self, root, world, monkeypatch, caplog
    ):
        spawn(world, 100)
        store = make_store(root, world)
        dead_tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(dead_tok).state == HOLDING
        del world.alive[100]
        # Deny every attempt of the bounded entry removal, so the retries run out.
        attempts = hold_unlinks(monkeypatch, {dead_tok.path: job_slots._ENTRY_IO_ATTEMPTS})
        with caplog.at_level(logging.WARNING, logger="booley.runtime.job_slots"):
            assert store.reap(CLASS_MEMORY) == []

        assert dead_tok.path.exists()
        assert [r.getMessage() for r in caplog.records] == [
            f"Could not remove Memory Reservation after retries: {dead_tok.path.name}"
        ]
        assert attempts[dead_tok.path] == job_slots._ENTRY_IO_ATTEMPTS
        assert store.reap(CLASS_MEMORY) == [dead_tok.path.name]
        assert not dead_tok.path.exists()

    def test_dead_waiter_is_reaped_out_of_the_queue(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        assert a.refresh(holder).state == HOLDING
        ghost_waiter = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert a.refresh(ghost_waiter).state == QUEUED
        del world.alive[100]
        b = make_store(root, world)
        tok = b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        # The dead waiter no longer counts toward ordering.
        assert b.refresh(tok).position == 0

    def test_recycled_pid_argv_mismatch_is_reaped(self, root, world):
        spawn(world, 100, argv=["python", "-m", "booley.flows.sim"])
        a = make_store(root, world)
        tok = a.submit(
            CLASS_MEMORY, memory=_ALONE, pid=100, argv=["python", "-m", "booley.flows.sim"]
        )
        assert a.refresh(tok).state == HOLDING
        # PID 100 is recycled by an unrelated process.
        world.alive[100] = ["sshd"]
        spawn(world, 200)
        b = make_store(root, world)
        tok_b = b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        assert b.refresh(tok_b).state == HOLDING

    def test_unknown_cmdline_is_trusted_alive(self, root, world):
        spawn(world, 100, argv=None)  # /proc unavailable → cannot judge
        a = make_store(root, world)
        tok = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100, argv=["python", "-m", "x"])
        assert a.refresh(tok).state == HOLDING
        assert a.reap(CLASS_MEMORY) == []

    def test_linked_dead_owner_waits_for_complete_execution_tree(self, root, world):
        execution_id = "a" * 32
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
        del world.alive[100]

        assert store.reap(CLASS_MEMORY) == []
        assert token.path.exists()
        assert cancellations == [execution_id]

        terminal = True
        assert store.reap(CLASS_MEMORY) == [token.path.name]

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

    def test_execution_id_is_inherited_and_persisted(self, tmp_path, world, monkeypatch):
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
        monkeypatch.setattr(
            job_slots,
            "verify_inherited_execution_id",
            lambda raw, *, booley_dir, owner_pid: execution_records.verify_inherited_execution_id(
                raw,
                booley_dir=booley_dir,
                owner_pid=owner_pid,
                observe_identity=world.observe_identity,
                ancestor_check=lambda owner, ancestor: (owner, ancestor) == (100, 50),
            ),
        )
        runtime_booley_dir.reset_cache()
        terminal = False
        cancellations: list[str] = []
        try:
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
        finally:
            runtime_booley_dir.reset_cache()

    def assert_unverified_inherited_execution_owner_dies_and_waiter_proceeds(
        self, tmp_path, world, monkeypatch, unverified_reason
    ):
        execution_id = "7" * 32
        project = tmp_path / "current" / ".booley"
        project.mkdir(parents=True)
        root = project / "runtime" / "jobs" / "slots"
        record_project = (
            tmp_path / "foreign" / ".booley" if unverified_reason == "foreign_project" else project
        )
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
        monkeypatch.setenv("BOOLEY_DIR", str(project))
        monkeypatch.setenv(RUNTIME_EXECUTION_ENV, execution_id)
        monkeypatch.setattr(
            job_slots,
            "verify_inherited_execution_id",
            lambda raw, *, booley_dir, owner_pid: execution_records.verify_inherited_execution_id(
                raw,
                booley_dir=booley_dir,
                owner_pid=owner_pid,
                observe_identity=world.observe_identity,
                ancestor_check=lambda owner, ancestor: (
                    unverified_reason == "foreign_project" and (owner, ancestor) == (100, 50)
                ),
            ),
        )
        runtime_booley_dir.reset_cache()
        cancellations: list[str] = []
        store = SlotStore(
            root,
            is_pid_alive=world.is_pid_alive,
            read_cmdline=world.read_cmdline,
            now=world.now,
            sleep=lambda _seconds: None,
            capture_identity=world.capture_identity,
            observe_identity=world.observe_identity,
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

        assert owner.owner_kind == "process"
        del world.alive[100]
        assert store.refresh(waiter).state == HOLDING
        assert not owner.path.exists()
        assert cancellations == []
        runtime_booley_dir.reset_cache()

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

    def test_unattached_job_lease_keeps_process_owner_compatibility(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING

        holder = store.snapshot(CLASS_MEMORY)[0][0]
        assert holder.execution_id is None
        assert holder.owner_kind == "process"

    def test_project_resolution_failure_does_not_inherit_execution_owner(
        self, root, world, monkeypatch
    ):
        def fail_project_resolution():
            raise RuntimeError("project unavailable")

        monkeypatch.setenv(RUNTIME_EXECUTION_ENV, "8" * 32)
        monkeypatch.setattr(
            runtime_booley_dir,
            "resolve_booley_dir",
            fail_project_resolution,
        )
        spawn(world, 100)

        token = make_store(root, world).submit(CLASS_MEMORY, memory=_ALONE, pid=100)

        assert token.execution_id is None
        assert token.owner_kind == "process"

    def test_noncanonical_slot_root_does_not_inherit_execution_owner(
        self, tmp_path, world, monkeypatch
    ):
        execution_id = "9" * 32
        project = tmp_path / ".booley"
        project.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(project))
        monkeypatch.setenv(RUNTIME_EXECUTION_ENV, execution_id)
        runtime_booley_dir.reset_cache()
        spawn(world, 50)
        spawn(world, 100)
        supervisor = world.capture_identity(50)
        assert supervisor is not None
        atomic_write_json(
            execution_paths(execution_id, booley_dir=project).record,
            active_execution_record(supervisor),
        )
        monkeypatch.setattr(
            job_slots,
            "verify_inherited_execution_id",
            lambda raw, *, booley_dir, owner_pid: execution_records.verify_inherited_execution_id(
                raw,
                booley_dir=booley_dir,
                owner_pid=owner_pid,
                observe_identity=world.observe_identity,
                ancestor_check=lambda owner, ancestor: (owner, ancestor) == (100, 50),
            ),
        )
        try:
            store = SlotStore(
                tmp_path / "override-slots",
                is_pid_alive=world.is_pid_alive,
                read_cmdline=world.read_cmdline,
                now=world.now,
                sleep=lambda _seconds: None,
                capture_identity=world.capture_identity,
                observe_identity=world.observe_identity,
                recovery=job_slots.SlotRecovery(recover_process_owner=lambda _identity: True),
            )
            token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)

            assert token.execution_id is None
            assert token.owner_kind == "process"
        finally:
            runtime_booley_dir.reset_cache()

    def test_linked_missing_promotion_metadata_uses_recovery_deadline(self, root, world):
        execution_id = "d" * 32
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
        payload = json.loads(token.path.read_text(encoding="utf-8"))
        payload["promoted_at"] = None
        payload["lease_expires_at"] = None
        token.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        world.clock += job_slots.LEASE_DURATION_SECONDS + 1

        assert store.reap(CLASS_MEMORY) == []
        assert cancellations == [execution_id]
        assert store.snapshot(CLASS_MEMORY)[0][0].lease_state == job_slots.LEASE_CANCELLING

    @pytest.mark.skipif(sys.platform != "linux", reason="execution recovery scans Linux /proc")
    @pytest.mark.parametrize("record_state", ["missing", "unrecoverable"])
    def test_failed_execution_is_scoped_recovered_and_releases_holder(
        self, tmp_path, monkeypatch, record_state
    ):
        project = tmp_path / ".booley"
        project.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(project))
        runtime_booley_dir.reset_cache()
        # Unique: real recovery signals every host process carrying this ID (xdist workers too).
        execution_id = uuid.uuid4().hex
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            env={**os.environ, RUNTIME_EXECUTION_ENV: execution_id},
        )
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            env={key: value for key, value in os.environ.items() if key != RUNTIME_EXECUTION_ENV},
        )
        store = SlotStore(project / "runtime" / "jobs" / "slots")
        token = store.submit(
            CLASS_MEMORY, memory=_ALONE, pid=process.pid, execution_id=execution_id
        )
        assert store.refresh(token).state == HOLDING
        paths = execution_paths(execution_id, booley_dir=project)
        if record_state == "unrecoverable":
            atomic_write_json(
                paths.record,
                {
                    "schema_version": PROTOCOL_VERSION,
                    "state": "unrecoverable",
                    "tree_terminal": False,
                },
            )
        payload = json.loads(token.path.read_text(encoding="utf-8"))
        payload["lease_expires_at"] = "1970-01-01T00:00:00Z"
        token.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        try:
            assert store.reap(CLASS_MEMORY) == [token.path.name]
            assert process.wait(timeout=hang_guard_s()) != 0
            assert unrelated.poll() is None
            terminal = read_json(paths.record)
            assert terminal["state"] == "terminal"
            assert terminal["tree_terminal"] is True
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=hang_guard_s())
            unrelated.kill()
            unrelated.wait(timeout=hang_guard_s())
            runtime_booley_dir.reset_cache()

    @pytest.mark.skipif(sys.platform != "linux", reason="process recovery scans Linux /proc")
    def test_expired_process_lease_recovers_only_its_durable_owner(self, tmp_path):
        clean_env = {
            key: value for key, value in os.environ.items() if key != RUNTIME_EXECUTION_ENV
        }
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            env=clean_env,
        )
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            env=clean_env,
        )
        store = SlotStore(tmp_path / "slots")
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=process.pid, timeout_s=None)
        assert store.refresh(token).state == HOLDING
        payload = json.loads(token.path.read_text(encoding="utf-8"))
        payload["lease_expires_at"] = "1970-01-01T00:00:00Z"
        token.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        try:
            assert store.reap(CLASS_MEMORY) == [token.path.name]
            assert process.wait(timeout=hang_guard_s()) != 0
            assert unrelated.poll() is None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=hang_guard_s())
            unrelated.kill()
            unrelated.wait(timeout=hang_guard_s())

    @pytest.mark.parametrize("unknown_schema", [job_slots.SLOT_SCHEMA_VERSION + 1, "future"])
    def test_unknown_future_slot_schema_fails_closed(self, root, world, unknown_schema):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        payload = json.loads(token.path.read_text(encoding="utf-8"))
        payload["schema_version"] = unknown_schema
        token.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        del world.alive[100]

        assert store.reap(CLASS_MEMORY) == []
        assert token.path.exists()

    def test_holder_past_deadline_is_reaped(self, root, world):
        spawn(world, 100)
        a = make_store(root, world)
        tok = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100, timeout_s=600)
        assert a.refresh(tok).state == HOLDING
        world.clock += 600 + job_slots.DEADLINE_SLACK_SECONDS + 1
        spawn(world, 200)
        b = make_store(root, world)
        tok_b = b.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        assert b.refresh(tok_b).state == HOLDING

    def test_waiter_gets_no_deadline(self, root, world):
        spawn(world, 100)
        a = make_store(root, world)
        holder = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100, timeout_s=600)
        assert a.refresh(holder).state == HOLDING
        waiter = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100, timeout_s=600)
        world.clock += 100_000  # far past any budget — queueing is legitimate
        assert a.refresh(waiter).state in (QUEUED, HOLDING)

    def test_reaped_token_reads_lost(self, root, world):
        spawn(world, 100)
        a = make_store(root, world)
        tok = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        tok.path.unlink()  # someone judged us stale
        assert a.refresh(tok).state == LOST

    def test_fresh_junk_is_kept_old_junk_is_reaped(self, root, world):
        spawn(world, 100)
        a = make_store(root, world)
        cls_dir = root / CLASS_MEMORY
        cls_dir.mkdir(parents=True)
        junk = cls_dir / "not-an-entry.json"
        junk.write_text("{oops", encoding="utf-8")
        assert a.reap(CLASS_MEMORY) == []  # too young to condemn
        world.clock = junk.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
        assert a.reap(CLASS_MEMORY) == ["not-an-entry.json"]


class TestAcquire:
    def test_acquire_blocks_until_slot_frees(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert a.refresh(holder).state == HOLDING

        b = make_store(root, world)
        positions: list[int] = []
        released = {"done": False}

        def sleep_then_release(_seconds):
            if not released["done"]:
                a.release(holder)
                released["done"] = True

        b._sleep = sleep_then_release
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

        b = make_store(root, world)
        positions: list[int] = []
        polls = {"n": 0}

        def tick(_seconds):
            polls["n"] += 1
            world.clock += 10  # 10s per poll
            a.renew(holder)
            if polls["n"] >= 8:  # 80s of waiting, then let it through
                a.release(holder)

        b._sleep = tick
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
        orig_submit = a.submit

        def submit_then_cancel(*args, **kwargs):
            tok = orig_submit(*args, **kwargs)
            tok.path.unlink()  # what cancel_waiter does from the server
            return tok

        a.submit = submit_then_cancel
        with pytest.raises(job_slots.ClaimLostError):
            a.acquire(CLASS_MEMORY, memory=_ALONE, pid=100, poll_interval=0.01)

    def test_cancel_waiter_removes_queued_entry_only(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        a = make_store(root, world)
        holder = a.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert a.refresh(holder).state == HOLDING
        waiter = a.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        assert a.refresh(waiter).state == QUEUED
        # Holder pid is refused; waiter pid is cancelled.
        assert a.cancel_waiter(100) is False
        assert a.cancel_waiter(200) is True
        assert a.refresh(waiter).state == LOST
        assert a.refresh(holder).state == HOLDING


@pytest.mark.parametrize("unverified_reason", ["foreign_project", "non_ancestor"])
def test_unverified_inherited_execution_owner_dies_and_waiter_proceeds(
    tmp_path, world, monkeypatch, unverified_reason
):
    TestGhostReaping().assert_unverified_inherited_execution_owner_dies_and_waiter_proceeds(
        tmp_path, world, monkeypatch, unverified_reason
    )


def _scenario_dead_owner_recovery_fails(store, world, _root, _terminal):
    token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, execution_id="a" * 32)
    store.refresh(token)
    del world.alive[100]


def _scenario_terminal_execution(store, world, _root, terminal):
    token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, execution_id="b" * 32)
    store.refresh(token)
    terminal["value"] = True


def _scenario_execution_without_id(store, world, _root, _terminal):
    token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
    store.refresh(token)
    edit_entry(token, owner_kind="execution", execution_id=None)
    del world.alive[100]


def _scenario_future_schema(store, world, _root, _terminal):
    token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
    store.refresh(token)
    edit_entry(token, schema_version=job_slots.SLOT_SCHEMA_VERSION + 1)
    del world.alive[100]


def _corrupt_entry(root: Path, world: FakeWorld, age_s: float) -> None:
    directory = root / CLASS_MEMORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / job_slots._entry_name("h", 0, 1, 100, 1)
    path.write_bytes(b"{not json")
    old = world.clock - age_s  # the store reads age against the simulated clock
    os.utime(path, (old, old))


def _scenario_corrupt_young(_store, world, root, _terminal):
    _corrupt_entry(root, world, 0)


def _scenario_corrupt_old(_store, world, root, _terminal):
    _corrupt_entry(root, world, 3600)


def _scenario_holder_past_lease_recovery_fails(store, world, _root, _terminal):
    token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
    store.refresh(token)
    world.clock += job_slots.LEASE_DURATION_SECONDS + 1


def _scenario_live_holder(store, _world, _root, _terminal):
    store.refresh(store.submit(CLASS_MEMORY, memory=_ALONE, pid=100))


def _scenario_dead_process_owner(store, world, _root, _terminal):
    store.refresh(store.submit(CLASS_MEMORY, memory=_ALONE, pid=100))
    del world.alive[100]


# Every staleness branch of the read-only judgment.
STALENESS_SCENARIOS = {
    "dead-owner-recovery-fails": _scenario_dead_owner_recovery_fails,
    "terminal-execution": _scenario_terminal_execution,
    "execution-without-id": _scenario_execution_without_id,
    "future-schema": _scenario_future_schema,
    "corrupt-young": _scenario_corrupt_young,
    "corrupt-old": _scenario_corrupt_old,
    "holder-past-lease-recovery-fails": _scenario_holder_past_lease_recovery_fails,
    "live-holder": _scenario_live_holder,
    "dead-process-owner": _scenario_dead_process_owner,
}


# Scenarios whose entry a reap might remove, and those it does remove (the others fail their recovery).
MAY_BE_STALE = {
    "dead-owner-recovery-fails",
    "terminal-execution",
    "corrupt-old",
    "holder-past-lease-recovery-fails",
    "dead-process-owner",
}
REAPED = {"terminal-execution", "corrupt-old", "dead-process-owner"}


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

    def test_snapshot_of_empty_class(self, root, world):
        store = make_store(root, world)
        assert store.snapshot(CLASS_MEMORY) == ([], [])


def edit_entry(token: job_slots.SlotToken, **changes: object) -> None:
    """Rewrite fields of a slot entry's JSON in place, as a foreign or future writer might."""
    payload = json.loads(token.path.read_text(encoding="utf-8"))
    payload.update(changes)
    token.path.write_bytes(json.dumps(payload).encode("utf-8"))


class TestReadOnlyStalenessJudgment:
    """``_may_be_stale`` predicts, without touching the store, what a reap may remove."""

    @pytest.mark.parametrize("scenario", sorted(STALENESS_SCENARIOS))
    def test_judgment_and_reap_agree(self, root, world, scenario):
        """For every staleness branch, the reap removes an entry only when it may be stale."""
        spawn(world, 100)
        terminal = {"value": False}
        store = make_execution_store(
            root,
            world,
            execution_terminal=lambda _execution_id: terminal["value"],
            cancellations=[],
        )
        STALENESS_SCENARIOS[scenario](store, world, root, terminal)
        (path,) = (root / CLASS_MEMORY).glob("*.json")
        before = path.read_bytes()

        assert store._may_be_stale(path) is (scenario in MAY_BE_STALE)
        assert path.read_bytes() == before
        reaped = store.reap(CLASS_MEMORY)
        assert reaped == ([path.name] if scenario in REAPED else [])

    @pytest.mark.parametrize(
        ("age_s", "owner_alive", "may_be_stale"),
        [(0, False, False), (3600, False, True), (3600, True, False)],
        ids=["young-dead-owner", "old-dead-owner", "old-live-owner"],
    )
    def test_an_unreadable_entry_is_judged_as_reap_does(
        self, root, world, monkeypatch, age_s, owner_alive, may_be_stale
    ):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        old = world.clock - age_s
        os.utime(token.path, (old, old))
        if not owner_alive:
            del world.alive[100]

        def unreadable(_self, _path):
            raise OSError("sharing violation")

        monkeypatch.setattr(SlotStore, "_load_token", unreadable)
        assert store._may_be_stale(token.path) is may_be_stale
        assert len(store.reap(CLASS_MEMORY)) == int(may_be_stale)


class TestJobsConfigParsing:
    def test_shared_budget_loader_warns_for_expected_read_failure(
        self,
        monkeypatch,
        caplog,
    ):
        from booley.runtime.shared_infra import load_job_budget_config

        monkeypatch.setattr(
            "booley.runtime.shared_infra._load_rtl_config",
            lambda _root: (_ for _ in ()).throw(OSError("unreadable")),
        )

        assert load_job_budget_config() == {}
        assert "Failed to load the shared Project job budget" in caplog.text

    def test_shared_budget_loader_propagates_unexpected_failure(self, monkeypatch):
        from booley.runtime.shared_infra import load_job_budget_config

        monkeypatch.setattr(
            "booley.runtime.shared_infra._load_rtl_config",
            lambda _root: (_ for _ in ()).throw(RuntimeError("broken invariant")),
        )

        with pytest.raises(RuntimeError, match="broken invariant"):
            load_job_budget_config()


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

    def test_null_timeout_holder_without_promotion_stamp_uses_lease_deadline(self, root, world):
        spawn(world, 100)
        store = make_store(root, world)
        tok = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100, timeout_s=None)
        assert store.refresh(tok).state == HOLDING
        payload = json.loads(tok.path.read_text(encoding="utf-8"))
        del payload["promoted_at"]
        del payload["lease_expires_at"]
        tok.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        world.clock += job_slots.LEASE_DURATION_SECONDS + 1
        assert store.reap(CLASS_MEMORY) == [tok.path.name]


class TestAcquireWithdrawsOnFailure:
    def test_should_abort_raises_and_withdraws_entry(self, root, world):
        # Shutdown hook: a queued acquire must end (entry withdrawn) when the
        # caller's should_abort fires — the Runner's Ctrl+C path.
        spawn(world, 100)
        spawn(world, 200)
        store = make_store(root, world)
        holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(holder).state == HOLDING

        aborts = iter([False, True])
        with pytest.raises(job_slots.ClaimAbortedError):
            store.acquire(CLASS_MEMORY, memory=_ALONE, pid=200, should_abort=lambda: next(aborts))
        holders, waiters = store.snapshot(CLASS_MEMORY)
        assert [token.lease_id for token in holders] == [holder.lease_id]
        assert waiters == []

    def test_unexpected_error_mid_wait_withdraws_entry(self, root, world):
        # An exception escaping the poll loop (here: a narration hook blowing
        # up) must withdraw our own entry — a claim without a caller-held
        # token is unreleasable and, with the process alive, unreapable.
        spawn(world, 100)
        spawn(world, 200)
        store = make_store(root, world)
        holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(holder).state == HOLDING

        def explode(_position):
            raise RuntimeError("narration boom")

        with pytest.raises(RuntimeError, match="narration boom"):
            store.acquire(CLASS_MEMORY, memory=_ALONE, pid=200, on_queued=explode)
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

    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "elsewhere"))
        assert job_slots.slots_dir() == tmp_path / "elsewhere"

    def test_resolution_never_creates_the_dir(self, tmp_path, monkeypatch):
        # Readers (mcp_server._job_phase) call this on hot paths; it must not
        # have runtime_dir()'s mkdir side effect.
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "elsewhere"))
        job_slots.slots_dir()
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
    holder = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
    assert store.refresh(holder).state == HOLDING
    legacy = store.submit(CLASS_MEMORY, memory=_ALONE, pid=200, role="ticket")
    interactive = store.submit(CLASS_MEMORY, memory=_ALONE, pid=300, role=ROLE_INTERACTIVE)
    reloaded = make_store(root, world)
    holders, waiting = reloaded.snapshot(CLASS_MEMORY)
    assert [t.pid for t in holders] == [100]
    assert [(t.pid, t.role) for t in waiting] == [
        (interactive.pid, ROLE_INTERACTIVE),
        (legacy.pid, "ticket"),
    ]


def test_unreadable_execution_record_is_not_proven_terminal(monkeypatch) -> None:
    """A lasting sharing error keeps the slot: release() must never raise."""
    from types import SimpleNamespace

    from tests.sharing_error_support import sharing_error

    def held(_path: Path) -> None:
        raise sharing_error()

    monkeypatch.setattr(job_slots, "read_json", held)
    monkeypatch.setattr(
        job_slots, "execution_paths", lambda _id: SimpleNamespace(record=Path("r"))
    )
    assert job_slots._default_execution_is_terminal("a" * 32) is False


# A gate a dead process left behind: a bare PID, as gates were written before
# they named their owner's identity. The PID is above the kernel limit.
_DEAD_GATE_PID = 4_194_305


class _SecondActor:
    """Runs a second process's gate attempt while the first one is paused, and holds what it took."""

    def __init__(self, enter_gate) -> None:
        self._enter_gate = enter_gate
        self._settled = threading.Event()
        self._release = threading.Event()
        self._thread: threading.Thread | None = None
        self.acquired: bool | None = None

    def run_once(self) -> None:
        """Start the actor on first call and wait until it took the gate or gave up."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._attempt)
        self._thread.start()
        assert self._settled.wait(hang_guard_s()), "second actor never settled"

    def _attempt(self) -> None:
        with self._enter_gate() as acquired:
            self.acquired = acquired
            self._settled.set()
            self._release.wait(2 * hang_guard_s())

    def finish(self) -> None:
        self._release.set()
        if self._thread is not None:
            self._thread.join(hang_guard_s())
            assert not self._thread.is_alive()


def _pause(monkeypatch, gate: Path, actor: _SecondActor, method: str) -> None:
    """Run *actor* when this thread first calls ``Path.<method>`` on *gate*.

    ``read`` stands for both ``read_text`` and ``read_bytes``. A read pauses
    after it returns (the breaker has judged), a removal before it runs.
    """
    paused_thread = threading.current_thread()
    names = ("read_text", "read_bytes") if method == "read" else (method,)
    removes = method in ("unlink", "rmdir")

    for name in names:
        original = getattr(Path, name)

        def paused(path, *args, _original=original, **kwargs):
            here = path == gate and threading.current_thread() is paused_thread
            if here and removes:
                actor.run_once()
            result = _original(path, *args, **kwargs)
            if here and not removes:
                actor.run_once()
            return result

        monkeypatch.setattr(Path, name, paused)


class TestStaleGateBreaking:
    """Two processes that find the same stale gate: at most one of them holds it."""

    @pytest.mark.parametrize("pause", ["read", "unlink"], ids=["judging", "removing"])
    def test_one_breaker_holds_a_stale_promotion_gate(self, root, world, monkeypatch, pause):
        spawn(world, 100)
        spawn(world, 200)
        second = make_store(root, world)
        second_token = second.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        gate = root / CLASS_MEMORY / ".promotion.lock"
        gate.write_text(f"{_DEAD_GATE_PID}\n", encoding="utf-8")
        world.clock = gate.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
        actor = _SecondActor(lambda: second._promotion_gate(second_token))
        first = make_store(root, world)
        first_token = first.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        _pause(monkeypatch, gate, actor, pause)
        try:
            with first._promotion_gate(first_token) as first_acquired:
                assert actor.acquired is not None, "the second actor never ran"
                assert [first_acquired, actor.acquired].count(True) == 1
        finally:
            actor.finish()

    @pytest.mark.parametrize("pause", ["stat", "rmdir"], ids=["judging", "removing"])
    def test_one_breaker_holds_a_stale_lease_gate(self, root, world, monkeypatch, pause):
        spawn(world, 100)
        spawn(world, os.getpid())  # the process both gate breakers run in
        holder_store = make_store(root, world)
        holder = holder_store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert holder_store.refresh(holder).state == HOLDING
        gate = holder.path.parent / f".{holder.lease_id}.lease.lock"
        gate.mkdir()  # a lease gate as a dead process left it before gates named their owner
        world.clock = gate.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
        second = make_store(root, world)
        actor = _SecondActor(lambda: second._lease_gate(holder))
        first = make_store(root, world)
        _pause(monkeypatch, gate, actor, pause)
        try:
            with first._lease_gate(holder) as first_acquired:
                assert actor.acquired is not None, "the second actor never ran"
                assert [first_acquired, actor.acquired].count(True) == 1
        finally:
            actor.finish()

    def test_unreadable_live_promotion_gate_is_not_broken(self, root, world, monkeypatch):
        spawn(world, 100)
        spawn(world, 200)
        holder = make_store(root, world)
        holder_token = holder.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        breaker = make_store(root, world)
        breaker_token = breaker.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        gate = root / CLASS_MEMORY / ".promotion.lock"
        originals = {name: getattr(Path, name) for name in ("read_text", "read_bytes")}

        def scanner(name):
            def scanned(path, *args, **kwargs):
                if path == gate:
                    raise windows_sharing_error()  # a scanner holds the gate file
                return originals[name](path, *args, **kwargs)

            return scanned

        with holder._promotion_gate(holder_token) as held:
            assert held
            world.clock = gate.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
            for name in originals:
                monkeypatch.setattr(Path, name, scanner(name))
            with breaker._promotion_gate(breaker_token) as acquired:
                assert not acquired

    def test_unusable_break_lock_is_reported_once(self, root, world, caplog):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        gate = root / CLASS_MEMORY / ".promotion.lock"
        gate.write_text(f"{_DEAD_GATE_PID}\n", encoding="utf-8")
        world.clock = gate.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
        (root / CLASS_MEMORY / ".gate-break.lock").mkdir()  # cannot be opened as a file

        with caplog.at_level(logging.WARNING, logger="booley.runtime.job_slots"):
            for _attempt in range(2):
                with store._promotion_gate(token) as acquired:
                    assert not acquired
        assert gate.exists()
        assert caplog.text.count("Cannot take the gate break lock") == 1

    @pytest.mark.parametrize("content", [b"\xff\xfe junk\n", b"not json\n"], ids=["bytes", "text"])
    def test_junk_promotion_gate_is_broken_once_old(self, root, world, content):
        spawn(world, 100)
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        gate = root / CLASS_MEMORY / ".promotion.lock"
        gate.write_bytes(content)
        with store._promotion_gate(token) as acquired:
            assert not acquired  # not yet old: its writer may still be mid-write
        world.clock = gate.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
        with store._promotion_gate(token) as acquired:
            assert acquired

    def test_gate_owner_is_recaptured_after_a_fork(self, root, world):
        current = [os.getpid()]
        spawn(world, current[0])
        spawn(world, current[0] + 1)
        store = SlotStore(
            root,
            is_pid_alive=world.is_pid_alive,
            read_cmdline=world.read_cmdline,
            now=world.now,
            sleep=lambda _seconds: None,
            capture_identity=world.capture_identity,
            observe_identity=world.observe_identity,
            getpid=lambda: current[0],
        )
        first = store._gate_owner()
        assert first.identity is not None
        assert store._gate_owner() is first  # cached while pid and identity hold
        current[0] = first.pid + 1
        assert store._gate_owner().pid == first.pid + 1

    def test_gate_owner_retries_a_failed_identity_capture(self, root, world):
        store = make_store(root, world)
        assert store._gate_owner().identity is None  # this process is not in the fake world
        spawn(world, os.getpid())
        assert store._gate_owner().identity is not None

    def test_recycled_pid_promotion_gate_is_broken(self, root, world):
        spawn(world, 100)
        spawn(world, 200)
        dead = make_store(root, world)
        dead_token = dead.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        live = make_store(root, world)
        live_token = live.submit(CLASS_MEMORY, memory=_ALONE, pid=200)
        with dead._promotion_gate(dead_token) as held:
            assert held
            gate = root / CLASS_MEMORY / ".promotion.lock"
            world.alive[100] = ["another", "process"]  # PID 100 is live again, as a new process
            world.clock = gate.stat().st_mtime + job_slots._UNREADABLE_REAP_AGE_SECONDS + 1
            with live._promotion_gate(live_token) as acquired:
                assert acquired


class TestLeaseRenewal:
    @pytest.mark.parametrize(
        ("elapsed", "lost"), [(10, False), (job_slots.LEASE_DURATION_SECONDS + 1, True)]
    )
    def test_missed_lease_gate_is_lost_only_after_expiry(self, root, world, elapsed, lost):
        spawn(world, 100)
        spawn(world, os.getpid())  # the process the other gate holder runs in
        store = make_store(root, world)
        token = store.submit(CLASS_MEMORY, memory=_ALONE, pid=100)
        assert store.refresh(token).state == HOLDING
        world.clock += elapsed
        with make_store(root, world)._lease_gate(token) as held:
            assert held
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
        reaper = make_store(root, world)
        reaper._capture_identity = lambda _pid: world.capture_identity(300)
        with reaper._lease_gate(token) as held:
            assert held
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

    def test_renewal_thread_outlives_a_missed_lease_gate(self, root, monkeypatch):
        monkeypatch.setattr(job_slots, "LEASE_RENEW_INTERVAL_SECONDS", 0.02)
        store = SlotStore(root)
        missed = threading.Event()
        real_renew = store.renew

        def observed_renew(token):
            renewed = real_renew(token)
            if not renewed:
                missed.set()
            return renewed

        monkeypatch.setattr(store, "renew", observed_renew)
        token = store.acquire(CLASS_MEMORY, memory=_ALONE, pid=os.getpid(), poll_interval=0.01)
        try:
            with SlotStore(root)._lease_gate(token) as held:
                assert held
                assert missed.wait(hang_guard_s())
                generation = token.lease_generation
            deadline = time.monotonic() + hang_guard_s()
            while token.lease_generation == generation and time.monotonic() < deadline:
                time.sleep(0.01)
            assert token.lease_generation > generation
            assert not token.lease_health.lost.is_set()
        finally:
            store.release(token)
