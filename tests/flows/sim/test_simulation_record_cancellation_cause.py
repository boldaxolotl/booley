"""A Simulation Record cancellation names its cause, and unreadable is not absent (#1905)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows import endpoint_admission
from booley.flows.sim.record.capacity import LeaseLostError
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordCancellationError,
    SimulationRecordPolicy,
)
from booley.flows.sim.record.model import create_simulation_record_plan
from booley.flows.sim.record.scheduler import BoundedSimulationRecordScheduler
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.record.store import SimulationRecordStore
from booley.runtime.job_slots import SlotStore
from tests.flows.sim.simulation_record_test_support import (
    _admission,
    _holder_claim,
    _managed,
    _outer_claim,
    _pinned_lanes,
    _scheduled_attempt,
    _two_item_manifest,
)


class _SimulationRecord:
    """A scheduler over a real SlotStore."""

    def __init__(self, tmp_path: Path, *, lanes: int) -> None:
        self.manifest = _two_item_manifest()
        self.store = SimulationRecordStore(tmp_path / "record")
        self.store.publish_manifest(self.manifest)
        project = tmp_path / "project"
        (project / ".booley").mkdir(parents=True)
        self.slots = SlotStore(tmp_path / "slots")
        self.outer = _outer_claim(self.slots)
        # _admission_cancellation_cause: the endpoint builds its admission around it, so the
        # hand-built managed admission here needs the same callable to name slot-loss causes.
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

    def run(self, items: int, trigger: Callable[[], None]) -> None:
        """Run *items* work items; the outer lane fires *trigger*."""

        def execute(attempt, _child_id) -> None:
            # Only the outer lane's item, under the Flow's claim, can take its own at its build.
            if attempt.build_settled is not None:
                trigger()

        scheduler = BoundedSimulationRecordScheduler(
            self.capacity,
            allocate=lambda item: _scheduled_attempt(self.store, item),
            execute=execute,
        )
        try:
            scheduler.run(self.manifest.document["work_items"][:items])
        except Exception:  # noqa: BLE001 -- the cancelled record may fail; the cause is the subject
            pass
        finally:
            self.slots.release(self.outer)


def test_outer_worker_failure_names_its_cause(tmp_path: Path) -> None:
    simulation_record = _SimulationRecord(tmp_path, lanes=1)

    def boom() -> None:
        raise RuntimeError("outer boom")

    simulation_record.run(1, trigger=boom)

    cause = simulation_record.capacity.cancellation_cause()
    assert cause == "record stop requested after a work item failed (RuntimeError)"


def test_child_cancellation_cause_names_a_lost_lease(tmp_path: Path) -> None:
    simulation_record = _SimulationRecord(tmp_path, lanes=1)
    capacity = simulation_record.capacity
    try:
        with (
            capacity.outer_permit(),
            pytest.raises(LeaseLostError),  # the lane ends with its lease lost
            capacity.child_permit(
                "item",
                attempt_id="attempt",
                attempt_ordinal=1,
                attempt_directory=Path("attempt"),
                publish=lambda _child: None,
                memory=capacity.work_item_claim({}),
            ) as permit,
        ):
            assert permit.scope.cancellation_cause() is None
            permit.lease_health.lost.set()
            assert permit.scope.cancellation_cause() == (
                f"Memory Reservation lease lost: {permit.token.job_class}/{permit.token.path.name}"
            )
    finally:
        simulation_record.slots.release(simulation_record.outer)


def test_cancelled_simulation_record_stops_before_publication(tmp_path: Path) -> None:
    manifest = _two_item_manifest()
    admission = replace(
        _admission(),
        cancellation=lambda: True,
        cancellation_cause=lambda: "Memory Reservation missing: memory/entry.json",
    )
    project = tmp_path / "project"
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    executor = OrdinaryHdlSerialExecutor(invoke=lambda *_args, **_kwargs: None)  # type: ignore[arg-type]
    with pytest.raises(
        SimulationRecordCancellationError, match="cancelled: Memory Reservation missing"
    ):
        SimulationRecord(executor, work_item_request=lambda *_args: None).run(
            NewSimulationRecordRunRequest(
                create_simulation_record_plan(manifest),
                project,
                invocation.parent,
                SimulationRecordPolicy(),
                invocation,
                admission,
            )
        )
