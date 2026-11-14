"""A Simulation Campaign cancellation names its cause, and unreadable is not absent (#1905)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows import endpoint_admission
from booley.flows.sim.record import coordinator
from booley.flows.sim.record.capacity import SimulationRecordCapacityError
from booley.flows.sim.record.coordinator import (
    SimulationRecord,
    SimulationRecordCancellationError,
)
from booley.flows.sim.record.scheduler import (
    BoundedSimulationRecordScheduler,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.runtime.job_slots import (
    SlotStore,
)
from tests.flows.sim.simulation_record_test_support import (
    _holder_claim,
    _managed,
    _outer_claim,
    _pinned_lanes,
    _scheduled_attempt,
    _two_item_manifest,
)


class _SimulationRecord:
    """A scheduler over a real SlotStore whose outer lane records its cancellation message."""

    def __init__(self, tmp_path: Path, *, lanes: int) -> None:
        self.manifest = _two_item_manifest()
        self.store = SimulationRecordStore(tmp_path / "record")
        self.store.publish_manifest(self.manifest)
        project = tmp_path / "project"
        (project / ".booley").mkdir(parents=True)
        self.slots = SlotStore(tmp_path / "slots")
        self.outer = _outer_claim(self.slots)
        cause = endpoint_admission._admission_cancellation_cause(self.slots, self.outer)
        admission = replace(
            _managed(self.slots, self.outer, holders=lanes),
            cancellation=lambda: cause() is not None,
            cancellation_cause=cause,
        )
        claim = _holder_claim(lanes)
        # The outer lane's item runs under the Flow's claim; every other lane's takes its own.
        self.capacity = _pinned_lanes(
            admission,
            self.manifest,
            project / ".booley",
            lambda _item: None if threading.current_thread().name.endswith("outer") else claim,
            lanes=lanes,
        )
        self.outer_message: str | None = None
        self.scheduler: BoundedSimulationRecordScheduler | None = None

    def run(self, items: int, trigger: Callable[[], None]) -> None:
        """Run *items* work items; the outer lane fires *trigger* and records its message."""

        def execute(_attempt, child_id) -> None:
            if child_id is not None:
                deadline = time.monotonic() + 5.0  # idle child lane until cancelled
                while not self.capacity._shutdown.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return
            trigger()
            # The scheduler's await loop has now run its argument-less cancel_waiters,
            # which must not mask the cause that triggered the cancellation.
            deadline = time.monotonic() + 5.0
            while not self.capacity._shutdown.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            try:
                coordinator.raise_if_cancelled_before_publication()
            except SimulationRecordCapacityError as exc:
                self.outer_message = str(exc)

        self.scheduler = BoundedSimulationRecordScheduler(
            self.capacity,
            allocate=lambda item: _scheduled_attempt(self.store, item),
            execute=execute,
        )
        try:
            self.scheduler.run(self.manifest.document["work_items"][:items])
        except Exception:  # noqa: BLE001 -- the cancelled campaign may fail; the message is the subject
            pass
        finally:
            self.slots.release(self.outer)


def test_outer_lane_stop_requested_names_cause(tmp_path: Path) -> None:
    simulation_record = _SimulationRecord(tmp_path, lanes=1)

    def fail_an_item() -> None:
        assert simulation_record.scheduler is not None
        simulation_record.scheduler._fail_simulation_record(RuntimeError("an item failed"))

    simulation_record.run(1, trigger=fail_an_item)

    assert simulation_record.outer_message is not None
    assert "execution cancelled before result publication" in simulation_record.outer_message
    assert "outer lane" in simulation_record.outer_message
    assert "stop requested" in simulation_record.outer_message
    assert "RuntimeError" in simulation_record.outer_message


def test_outer_lane_lease_lost_names_slot(tmp_path: Path) -> None:
    simulation_record = _SimulationRecord(tmp_path, lanes=1)
    simulation_record.run(1, trigger=simulation_record.outer.lease_health.lost.set)

    assert simulation_record.outer_message is not None
    assert "outer lane" in simulation_record.outer_message
    assert "lease lost" in simulation_record.outer_message
    assert simulation_record.outer.path.name in simulation_record.outer_message


def test_outer_lane_missing_token_names_slot(tmp_path: Path, monkeypatch) -> None:
    simulation_record = _SimulationRecord(tmp_path, lanes=1)
    path = simulation_record.outer.path
    real_absent = simulation_record.slots.token_absent
    withdrawn = threading.Event()
    # Report the entry gone without unlinking it: Windows may still hold it open.
    monkeypatch.setattr(
        simulation_record.slots,
        "token_absent",
        lambda token: withdrawn.is_set() or real_absent(token),
    )
    simulation_record.run(1, trigger=withdrawn.set)

    assert simulation_record.outer_message is not None
    assert "outer lane" in simulation_record.outer_message
    assert "token missing" in simulation_record.outer_message
    assert path.name in simulation_record.outer_message
    # A really missing file (released by the run) is still absent.
    assert real_absent(simulation_record.outer) is True


def test_outer_worker_failure_names_its_cause(tmp_path: Path) -> None:
    simulation_record = _SimulationRecord(tmp_path, lanes=1)

    def boom() -> None:
        raise RuntimeError("outer boom")

    simulation_record.run(1, trigger=boom)

    cause = simulation_record.capacity.cancellation_cause()
    assert cause == "campaign stop requested after a work item failed (RuntimeError)"


def test_child_cancellation_cause_names_a_lost_lease(tmp_path: Path) -> None:
    simulation_record = _SimulationRecord(tmp_path, lanes=1)
    lost = threading.Event()
    permit = SimpleNamespace(
        lease_health=SimpleNamespace(lost=lost),
        token=SimpleNamespace(job_class="memory", path=Path("child.json")),
    )
    try:
        assert simulation_record.capacity._child_cancellation_cause(permit) is None
        lost.set()
        cause = simulation_record.capacity._child_cancellation_cause(permit)
        assert cause == "Job Slot lease lost: memory/child.json"
    finally:
        simulation_record.slots.release(simulation_record.outer)


def test_cancelled_simulation_record_stops_before_publication() -> None:
    request = SimpleNamespace(
        admission=SimpleNamespace(
            cancellation=lambda: True,
            cancellation_cause=lambda: "Job Slot token missing: memory/entry.json",
        )
    )
    with pytest.raises(
        SimulationRecordCancellationError, match="cancelled: Job Slot token missing"
    ):
        SimulationRecord._raise_if_cancelled(request)  # type: ignore[arg-type]
