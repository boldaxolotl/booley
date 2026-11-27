"""The parallel Simulation Record path is one bounded pool of lane threads."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.record.capacity import (
    SimulationRecordCapacity,
    SimulationRecordCapacityError,
)
from booley.flows.sim.record.child_protocol import ChildExecutionRegistry
from booley.flows.sim.record.scheduler import (
    BoundedSimulationRecordScheduler,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.runtime.execution_records import (
    ExecutionId,
    execution_paths,
    read_json,
)
from booley.runtime.job_slots import (
    CLASS_MEMORY,
    ClaimAbortedError,
    SlotRecovery,
    SlotStore,
)
from tests.flows.sim.simulation_record_test_support import (
    _PARALLEL_SYNC_TIMEOUT_S,
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

    def __init__(
        self,
        tmp_path: Path,
        names: tuple[str, ...],
        lanes: int,
        *,
        cancel_execution: Callable[[str], None] | None = None,
    ) -> None:
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

        terminal = (
            SlotRecovery(execution_is_terminal=is_terminal)
            if cancel_execution is None
            else SlotRecovery(execution_is_terminal=is_terminal, cancel_execution=cancel_execution)
        )
        self.slots = SlotStore(tmp_path / "slots", recovery=terminal)
        self.outer = _outer_claim(self.slots)
        self.lanes = lanes

    def scheduler(self, execute, *, timeout_seconds: float, prepare_child=None):
        admission = replace(
            _managed(self.slots, self.outer, holders=self.lanes),
            timeout_seconds=timeout_seconds,
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


def test_child_token_left_after_terminal_proof_fails_the_simulation_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    simulation_record = _SimulationRecord(tmp_path, ("alpha", "beta"), lanes=2)
    child_done = threading.Event()
    # Report every token still present, as if the child's release never landed.
    monkeypatch.setattr(simulation_record.slots, "token_absent", lambda _token: False)

    def execute(_attempt, child_id) -> None:
        if child_id is None:
            assert child_done.wait(_PARALLEL_SYNC_TIMEOUT_S)
            return
        child_done.set()

    try:
        with pytest.raises(
            SimulationRecordCapacityError, match="child token remained after terminal proof"
        ):
            simulation_record.scheduler(execute, timeout_seconds=hang_guard_s()).run(
                simulation_record.items
            )
    finally:
        simulation_record.slots.release(simulation_record.outer)


# Who reclaims a terminal child's token first: the lane's own hook, or another lane.
_REAP_ORDERINGS = ("hook_first", "other_lane_first")


def _reap_in_order(
    slots: SlotStore,
    unwrapped_reap: Callable[[str], list[str]],
    child_id: ExecutionId,
    ordering: str,
) -> None:
    """Reap the terminal child's token in the requested order and prove it is gone."""
    if ordering == "other_lane_first":
        # Another lane's reap, on its own thread outside this lane's scope.
        other_lane_reaped: list[str] = []
        other_lane = threading.Thread(
            target=lambda: other_lane_reaped.extend(unwrapped_reap(CLASS_MEMORY)),
            daemon=True,  # a hung reap must not keep the worker alive past the assert
        )
        other_lane.start()
        other_lane.join(_PARALLEL_SYNC_TIMEOUT_S)
        assert not other_lane.is_alive()
        assert len(other_lane_reaped) == 1  # exactly this child's token
        assert slots.reap(CLASS_MEMORY) == []  # nothing left for the hook
    else:
        assert len(slots.reap(CLASS_MEMORY)) == 1  # this child's token
    holders, waiters = slots.snapshot(CLASS_MEMORY)
    assert all(token.execution_id != child_id for token in (*holders, *waiters))


def _record_recovery_side_effects(
    slots: SlotStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[ExecutionId], list[str]]:
    """Record child cancels and token-recovery starts; both must stay empty."""
    cancelled: list[ExecutionId] = []
    registry_cancel = ChildExecutionRegistry.cancel

    def recording_cancel(registry, execution_id: ExecutionId) -> bool:
        cancelled.append(execution_id)
        return registry_cancel(registry, execution_id)

    monkeypatch.setattr(ChildExecutionRegistry, "cancel", recording_cancel)
    recoveries: list[str] = []
    begin_recovery = slots.begin_token_recovery

    def recording_begin_recovery(token) -> bool:
        recoveries.append(str(token.execution_id))
        return begin_recovery(token)

    monkeypatch.setattr(slots, "begin_token_recovery", recording_begin_recovery)
    return cancelled, recoveries


def _run_terminal_reap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordering: str) -> None:
    """Run a two-item Simulation Record whose terminal child tokens are reaped in a forced order.

    The competing reapers are the other lane's slot-store calls (``refresh``,
    ``snapshot`` and ``submit`` all reap the memory class). The ``reap_gate`` makes the
    race deterministic: the hook publishes the terminal record and reaps under the
    gate, so no other lane observes the terminal record before the ordered reap.
    """
    recovery_cancels: list[str] = []
    simulation_record = _SimulationRecord(
        tmp_path, ("alpha", "beta"), lanes=2, cancel_execution=recovery_cancels.append
    )
    reap_gate = threading.RLock()
    unwrapped_reap = simulation_record.slots.reap

    def gated_reap(job_class: str) -> list[str]:
        with reap_gate:
            return unwrapped_reap(job_class)

    monkeypatch.setattr(simulation_record.slots, "reap", gated_reap)
    finished: list[ExecutionId] = []
    publish_terminal = ChildExecutionRegistry.mark_terminal

    def mark_terminal_then_reap(registry, prepared, terminal_cause: str) -> None:
        with reap_gate:
            publish_terminal(registry, prepared, terminal_cause)
            finished.append(prepared.execution_id)
            _reap_in_order(
                simulation_record.slots, unwrapped_reap, prepared.execution_id, ordering
            )

    monkeypatch.setattr(ChildExecutionRegistry, "mark_terminal", mark_terminal_then_reap)
    cancelled, recoveries = _record_recovery_side_effects(simulation_record.slots, monkeypatch)

    try:
        # A reaped terminal token handled as a lost lease raises LeaseLostError here.
        simulation_record.scheduler(
            lambda _attempt, _child_id: None, timeout_seconds=hang_guard_s()
        ).run(simulation_record.items)
    finally:
        simulation_record.slots.release(simulation_record.outer)
    assert len(finished) == 2  # each item's own child
    for child_id in finished:
        paths = execution_paths(child_id, booley_dir=simulation_record.registry.project_data)
        assert read_json(paths.record)["terminal_cause"] == "completed"
        assert not paths.cancel.exists()
    assert (cancelled, recoveries, recovery_cancels) == ([], [], [])
    assert simulation_record.capacity.cancellation_cause() is None
    assert simulation_record.slots.snapshot(CLASS_MEMORY) == ([], [])


def test_reaping_a_terminal_child_token_is_not_a_lost_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The terminal child's token may be reclaimed by the lane's own hook or by another
    # lane first; either way it is not a lost lease. Both orders are forced, not raced.
    for ordering in _REAP_ORDERINGS:
        with monkeypatch.context() as ordering_patch:
            try:
                _run_terminal_reap(tmp_path / ordering, ordering_patch, ordering)
            except AssertionError as error:
                error.add_note(f"failing ordering: {ordering}")
                raise


def test_lease_tick_after_terminal_reap_starts_no_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After the reap, a renewal tick marks the reaped lease lost. The await loop must
    # not then cancel the already-terminal child (no stray cancel file).

    simulation_record = _SimulationRecord(tmp_path, ("alpha",), lanes=1)
    confirmed: list = []
    cancelled: list[ExecutionId] = []
    publish_terminal = ChildExecutionRegistry.mark_terminal

    def mark_terminal_then_reap_and_tick(registry, prepared, terminal_cause: str) -> None:
        publish_terminal(registry, prepared, terminal_cause)
        assert len(simulation_record.slots.reap(CLASS_MEMORY)) == 1
        (permit,) = confirmed
        assert not simulation_record.slots.renew(permit.token)  # the tick sees the lease gone
        simulation_record.capacity.recover_lost_children()

    monkeypatch.setattr(ChildExecutionRegistry, "mark_terminal", mark_terminal_then_reap_and_tick)
    monkeypatch.setattr(
        ChildExecutionRegistry,
        "cancel",
        lambda _registry, execution_id: cancelled.append(execution_id),
    )
    scheduler = simulation_record.scheduler(
        lambda _attempt, _child_id: None, timeout_seconds=hang_guard_s()
    )
    # _confirm_lease: the lane's final lease check has no public observation point; the test
    # records its calls to prove no recovery starts after a terminal reap.
    confirm = SimulationRecordCapacity._confirm_lease

    def recording_confirm(capacity, permit) -> bool:
        confirmed.append(permit)
        return confirm(capacity, permit)

    monkeypatch.setattr(SimulationRecordCapacity, "_confirm_lease", recording_confirm)
    try:
        scheduler.run(simulation_record.items)
    finally:
        simulation_record.slots.release(simulation_record.outer)
    (permit,) = confirmed
    assert cancelled == []
    child_paths = execution_paths(
        permit.child_execution_id, booley_dir=simulation_record.registry.project_data
    )
    assert not child_paths.cancel.exists()
    assert simulation_record.slots.snapshot(CLASS_MEMORY) == ([], [])


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
