"""Runtime reads a Simulation Record depends on that real runs only hit by timing.

A real run measures /proc and reads Slot entries while other processes change them, so
these failure arcs were covered by chance. Each test forces one of them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.runtime import job_slots, memory_budget
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
