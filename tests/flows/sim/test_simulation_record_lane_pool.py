"""The parallel Simulation Record path is one bounded pool of lane threads."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.record.capacity import SimulationRecordCapacityError
from booley.flows.sim.record.child_protocol import ChildExecutionRegistry
from booley.flows.sim.record.scheduler import BoundedSimulationRecordScheduler
from booley.flows.sim.record.store import SimulationRecordStore
from booley.runtime.execution_records import ExecutionId, execution_paths, read_json
from booley.runtime.job_slots import ClaimAbortedError, SlotRecovery, SlotStore
from tests.flows.sim.simulation_record_test_support import (
    _capacity,
    _item_capacity,
    _lane_registry,
    _managed,
    _named_manifest,
    _outer_claim,
    _scheduled_attempt,
)
from tests.time_budget import hang_guard_s


class _SimulationRecord:
    """One real manifest, registry, slot store and held outer token."""

    def __init__(self, tmp_path: Path, names: tuple[str, ...], lanes: int) -> None:
        self.manifest = _named_manifest(names)
        self.store = SimulationRecordStore(tmp_path / "record")
        self.store.publish_manifest(self.manifest)
        project = tmp_path / "project"
        project.mkdir()
        (project / ".booley").mkdir()
        self.registry = ChildExecutionRegistry(self.manifest, project)

        # Like production, the store reads the child records the registry writes, so
        # any reap reclaims a child token once that child is terminal.
        def is_terminal(execution_id: str) -> bool:
            return self.registry.is_terminal(ExecutionId(execution_id))

        terminal = SlotRecovery(execution_is_terminal=is_terminal)
        self.slots = SlotStore(tmp_path / "slots", recovery=terminal)
        self.outer = _outer_claim(self.slots)
        self.lanes = lanes

    def scheduler(self, execute, *, timeout_seconds: float, prepare_child=None):
        admission = replace(
            _managed(self.slots, self.outer, holders=self.lanes), timeout_seconds=timeout_seconds
        )
        self.capacity = _item_capacity(
            admission, self.manifest, self.registry.project_data, lanes=self.lanes
        )
        return BoundedSimulationRecordScheduler(
            self.capacity,
            allocate=lambda item: _scheduled_attempt(self.store, item),
            execute=execute,
            prepare_child=prepare_child,
        )

    @property
    def items(self):
        return self.manifest.document["work_items"]


@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        pytest.param("completed", "completed", id="completed"),
        # The lane held its slot, so its release-time tree recovery records the cause.
        pytest.param("work_failed", "orphan_recovered", id="work_failed"),
        # The lane never reached a slot: nothing was recovered, so the error is recorded.
        pytest.param("publish_failed", "record_error", id="publish_failed"),
    ],
)
def test_child_lane_terminal_cause(tmp_path: Path, failure: str, cause: str) -> None:
    """A child lane has its identity before its work starts and a terminal record after."""
    simulation_record = _SimulationRecord(tmp_path, ("alpha",), lanes=1)
    lanes: list[ExecutionId] = []

    def lane_paths(child_id):
        return execution_paths(
            ExecutionId(child_id), booley_dir=simulation_record.registry.project_data
        )

    def publish(_attempt, child_id) -> None:
        lanes.append(child_id)
        assert lane_paths(child_id).context.is_file()
        if failure == "publish_failed":
            raise RuntimeError("publish boom")

    def execute(_attempt, child_id) -> None:
        assert read_json(lane_paths(child_id).record)["state"] == "waiting"
        if failure == "work_failed":
            raise RuntimeError("work boom")

    scheduler = simulation_record.scheduler(
        execute, timeout_seconds=hang_guard_s(), prepare_child=publish
    )
    try:
        if failure == "completed":
            scheduler.run(simulation_record.items)
        else:
            with pytest.raises(RuntimeError, match="boom"):
                scheduler.run(simulation_record.items)
    finally:
        simulation_record.slots.release(simulation_record.outer)
    (lane,) = lanes
    record = read_json(lane_paths(lane).record)
    assert (record["state"], record["tree_terminal"]) == ("terminal", True)
    assert record["terminal_cause"] == cause


@pytest.mark.parametrize(
    ("when", "error", "match", "cause"),
    [
        ("before_slot", ClaimAbortedError, "shutdown requested", "record_error"),
        ("in_work", SimulationRecordCapacityError, "plain capacity failure", "orphan_recovered"),
        ("after_work", ClaimAbortedError, "cancellation requested", "completed"),
    ],
)
def test_child_permit_retires_the_lane_on_every_exit(
    tmp_path: Path, when: str, error: type[Exception], match: str, cause: str
) -> None:
    store = SlotStore(tmp_path / "slots")  # 2-holder budget
    outer = _outer_claim(store)
    cancelled = threading.Event()
    admission = replace(_managed(store, outer), cancellation=cancelled.is_set)
    capacity = _capacity(admission, tmp_path)
    lanes: list[ExecutionId] = []

    def publish(child_id: ExecutionId) -> None:
        lanes.append(child_id)
        if when == "before_slot":
            cancelled.set()

    try:
        with (
            capacity.outer_permit(),
            pytest.raises(error, match=match),
            capacity.child_permit(
                "item",
                attempt_id="attempt",
                attempt_ordinal=1,
                attempt_directory=Path("attempt"),
                publish=publish,
                memory=capacity.work_item_claim({}),
            ),
        ):
            if when == "in_work":
                raise SimulationRecordCapacityError("plain capacity failure")
            cancelled.set()
    finally:
        store.release(outer)
    record = read_json(
        execution_paths(lanes[0], booley_dir=_lane_registry(tmp_path).project_data).record
    )
    assert (record["state"], record["terminal_cause"]) == ("terminal", cause)
