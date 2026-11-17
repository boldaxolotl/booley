"""Runtime reads a Simulation Record depends on that real runs only hit by timing.

A real run measures /proc and reads Slot entries while other processes change them, so
these failure arcs were covered by chance. Each test forces one of them.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.record.scheduler import (
    BoundedSimulationRecordScheduler,
    SimulationRecordSchedulingError,
)
from booley.runtime import execution_recovery, job_slots, memory_budget
from booley.runtime.job_slots import (
    CLASS_MEMORY,
    HOLDING,
    QUEUED,
    ClaimAbortedError,
    ClaimLostError,
    SlotStore,
)
from tests.conftest import symlink_or_skip


def test_reserve_measurement_counts_a_process_without_a_readable_namespace_as_unreadable(
    tmp_path: Path,
) -> None:
    """The reserve a run measures from /proc must not drop a process it cannot place."""
    process = tmp_path / "proc" / "4200001"
    process.mkdir(parents=True)
    (process / "comm").write_text("claude\n", encoding="utf-8")
    (process / "status").write_text("VmRSS:\t4 kB\n", encoding="utf-8")
    stat = ["S", "1", *["0"] * 17, "7", "0"]
    (process / "stat").write_text("4200001 (x) " + " ".join(stat), encoding="utf-8")

    (agent, *_rest) = memory_budget.measure_reserve(proc_root=process.parent)

    assert agent.source is memory_budget.ReserveSource.UNREADABLE


def test_reserve_measurement_counts_a_process_that_vanished_mid_read_as_unreadable(
    tmp_path: Path,
) -> None:
    """A process that exits between the scan and its reads keeps its full reserve."""
    process = tmp_path / "proc" / "4200002"
    process.mkdir(parents=True)
    (process / "comm").write_text("claude\n", encoding="utf-8")

    (agent, *_rest) = memory_budget.measure_reserve(proc_root=process.parent)

    assert agent.source is memory_budget.ReserveSource.UNREADABLE


def test_memory_claim_stays_queued_behind_a_holder_whose_entry_cannot_be_parsed(
    tmp_path: Path,
) -> None:
    """An unknown reservation never fits: the claim waits instead of starting beside it."""
    store = SlotStore(tmp_path / "slots")
    claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=100.0)
    holder = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)
    assert store.refresh(holder).state == HOLDING
    waiter = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)

    holder.path.write_text("{", encoding="utf-8")

    assert store.refresh(waiter).state == QUEUED


def test_queued_memory_claim_whose_entry_vanishes_fails_instead_of_waiting_forever(
    tmp_path: Path,
) -> None:
    """A cancelled waiter's entry disappears while its owner polls: the wait ends as lost."""
    store = SlotStore(tmp_path / "slots")
    claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=600.0)
    holder = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)
    assert store.refresh(holder).state == HOLDING

    def cancel_the_waiter(_position: int | None) -> None:
        for waiter in store.observe(CLASS_MEMORY)[1]:
            waiter.path.unlink()

    with pytest.raises(ClaimLostError, match="vanished while waiting"):
        store.acquire(
            CLASS_MEMORY,
            pid=os.getpid(),
            memory=claim,
            poll_interval=0.01,
            on_queued=cancel_the_waiter,
        )


def test_memory_claim_gives_up_relisting_a_listed_entry_that_stays_gone(
    tmp_path: Path,
) -> None:
    """The relist after a vanished entry is bounded: an entry that never reads counts as blocking."""
    store = SlotStore(tmp_path / "slots")
    claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=100.0)
    holder = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)
    assert store.refresh(holder).state == HOLDING
    waiter = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)

    holder.path.unlink()
    symlink_or_skip(holder.path, tmp_path / "nowhere")  # listed, yet it never reads or exists

    assert store.refresh(waiter).state == QUEUED


def test_queued_memory_claim_stops_waiting_when_the_record_is_cancelled(tmp_path: Path) -> None:
    """A cancel that reaches a polling waiter ends its wait and withdraws its entry."""
    store = SlotStore(tmp_path / "slots")
    claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=600.0)
    holder = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)
    assert store.refresh(holder).state == HOLDING

    with pytest.raises(ClaimAbortedError, match="aborted while waiting"):
        store.acquire(
            CLASS_MEMORY,
            pid=os.getpid(),
            memory=claim,
            poll_interval=0.01,
            should_abort=lambda: True,
        )

    assert [token.lease_id for token in store.observe(CLASS_MEMORY)[0]] == [holder.lease_id]
    assert store.observe(CLASS_MEMORY)[1] == []


def test_cancelling_a_waiter_whose_entry_is_unreadable_refuses_the_withdrawal(
    tmp_path: Path,
) -> None:
    """An entry that cannot be read is not withdrawn, and the waiter stays queued."""
    store = SlotStore(tmp_path / "slots", sleep=lambda _seconds: None)
    claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=600.0)
    holder = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)
    assert store.refresh(holder).state == HOLDING
    waiter = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)
    entry = waiter.path
    entry.unlink()
    entry.mkdir()  # same name, but reading it fails with an OSError other than "missing"

    assert store.cancel_waiter_token(waiter) is False
    assert entry.is_dir()


def test_unreadable_process_environment_with_an_unreadable_stat_never_matches(
    tmp_path: Path,
) -> None:
    """A process whose environment and stat cannot be read is skipped, not matched."""
    proc = tmp_path / "654"
    proc.mkdir()
    (proc / "environ").mkdir()  # unreadable: an OSError other than "missing"
    marker = b"BOOLEY_RUNTIME_EXECUTION_ID="

    # Private seam: the scan reads the host /proc, so only the per-process matcher takes a path.
    assert execution_recovery._matches_execution(proc, marker) is False  # pyright: ignore[reportPrivateUsage]


def test_a_record_stopped_while_its_attempt_waits_for_a_run_directory_refuses_to_start_it() -> (
    None
):
    """The permit wait ends in a refusal, not a start, once the record shuts down."""

    def unused(*_args: object) -> object:
        raise AssertionError("the attempt must not be allocated or executed")

    scheduler = BoundedSimulationRecordScheduler(
        SimpleNamespace(shutdown_requested=False),  # pyright: ignore[reportArgumentType]
        allocate=unused,  # pyright: ignore[reportArgumentType]
        execute=unused,  # pyright: ignore[reportArgumentType]
    )
    # Private seam: the permit is reached only by a lane that races a shutdown.
    scheduler._stop.set()  # pyright: ignore[reportPrivateUsage]

    with (
        pytest.raises(SimulationRecordSchedulingError, match="record shutdown requested"),
        scheduler._collision_permit("run"),  # pyright: ignore[reportPrivateUsage]
    ):
        pass


class _StoreWhoseQueuedEntryVanishesDuringTheRefresh(SlotStore):
    """A store whose waiting entry is cancelled between the poll's existence check and its read."""

    armed = False

    def reap(self, job_class: str) -> list[str]:
        if self.armed:
            for waiter in self.observe(job_class)[1]:
                waiter.path.unlink()
        return super().reap(job_class)


def test_queued_memory_claim_whose_entry_vanishes_during_the_refresh_fails_instead_of_waiting(
    tmp_path: Path,
) -> None:
    """The poll's own read can lose the entry: the wait ends as lost, never as a timeout."""
    store = _StoreWhoseQueuedEntryVanishesDuringTheRefresh(tmp_path / "slots")
    claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=600.0)
    holder = store.submit(CLASS_MEMORY, pid=os.getpid(), memory=claim)
    assert store.refresh(holder).state == HOLDING
    store.armed = True

    with pytest.raises(ClaimLostError, match="vanished while waiting"):
        store.acquire(CLASS_MEMORY, pid=os.getpid(), memory=claim, poll_interval=0.01)
