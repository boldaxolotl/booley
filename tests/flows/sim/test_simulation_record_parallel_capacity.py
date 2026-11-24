"""Bounded parallel execution, capacity, cancellation and lease loss of Simulation Records."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.base import FlowMechanics
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.memory_admission import job_threads_at_start
from booley.flows.sim.record.capacity import (
    LeaseLostError,
    SimulationRecordCapacity,
    SimulationRecordCapacityError,
)
from booley.flows.sim.record.child_protocol import ChildExecutionRegistry
from booley.flows.sim.record.codec import (
    canonical_json_bytes,
)
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
)
from booley.flows.sim.record.model import create_simulation_record_plan
from booley.flows.sim.record.run_directory import (
    RunDirectory,
    claimed_run_directory,
    expand_run_directory,
)
from booley.flows.sim.record.scheduler import (
    BoundedSimulationRecordScheduler,
    ScheduledAttempt,
)
from booley.flows.sim.record.serial_execution import (
    OrdinaryHdlSerialExecutor,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.runtime.execution_records import (
    ExecutionId,
    child_context_matches,
    execution_paths,
    read_json,
)
from booley.runtime.job_slots import (
    CLASS_MEMORY,
    HOLDING,
    INHERITED_SLOT_ENV,
    QUEUED,
    ROLE_INTERACTIVE,
    ClaimAbortedError,
    SlotRecovery,
    SlotStore,
    SlotToken,
    TokenState,
)
from booley.runtime.supervised_execution import (
    SupervisedExecutionScope,
    SupervisedProcessSet,
    supervised_execution_scope,
)
from tests.flows.sim.simulation_record_test_support import (
    _PARALLEL_SYNC_TIMEOUT_S,
    _admission,
    _capacity,
    _holder_claim,
    _item_capacity,
    _lane_registry,
    _managed,
    _outer_claim,
    _parallel_environment,
    _ParallelExecution,
    _remove_child_lease,
    _remove_slot_entry,
    _run_identity,
    _scheduled_attempt,
    _three_item_manifest,
    _two_item_manifest,
    _wait_until,
)
from tests.memory_budget_support import pin_budget
from tests.oom_priority_support import FakeProc
from tests.sharing_error_support import sharing_error
from tests.time_budget import hang_guard_s, scaled


def _unique_execution_id() -> ExecutionId:
    """A per-test runtime execution ID.

    Real recovery scans every process on the host for its execution ID, so a
    shared literal would let one xdist worker's recovery signal another's child.
    """
    return ExecutionId(uuid.uuid4().hex)


def _child_permit(capacity: SimulationRecordCapacity, work_item_id: str = "item"):
    """Enter a child lane for a dummy attempt."""
    return capacity.child_permit(
        work_item_id,
        attempt_id="attempt",
        attempt_ordinal=1,
        attempt_directory=Path("attempt"),
        publish=lambda _child: None,
        memory=capacity.work_item_claim({}),
    )


def _record_project(tmp_path: Path):
    """A published two-item manifest, its store, and an empty project with a Booley Directory."""
    manifest = _two_item_manifest()
    simulation_record_store = SimulationRecordStore(tmp_path / "record")
    simulation_record_store.publish_manifest(manifest)
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True, exist_ok=True)
    return manifest, simulation_record_store, project


@pytest.fixture
def slots(tmp_path: Path):
    """A real slot store with the Flow's outer claim held; the claim is released on teardown."""
    store = SlotStore(tmp_path / "slots")
    outer = _outer_claim(store)
    try:
        yield store, outer
    finally:
        store.release(outer)


@pytest.fixture
def managed_capacity(tmp_path: Path, slots):
    """A managed capacity over the real slot store of ``slots``."""
    store, outer = slots
    return store, _capacity(_managed(store, outer), tmp_path)


@pytest.fixture
def blocked_capacity(tmp_path: Path, slots):
    """A one-lane capacity whose slot store holds another Job's claim, leaving no room."""
    store, outer = slots
    blocker = store.acquire(CLASS_MEMORY, pid=os.getpid(), memory=_holder_claim(1))
    try:
        yield store, _capacity(_managed(store, outer, holders=1), tmp_path, lanes=1), blocker
    finally:
        store.release(blocker)


def _assert_only_the_outer_claim_remains(store: SlotStore) -> None:
    holders, waiters = store.snapshot(CLASS_MEMORY)
    assert [token.execution_id for token in holders] == [ExecutionId("a" * 32)]
    assert waiters == []


def _prepared_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest, simulation_record_store, project = _record_project(tmp_path)
    project_data = project / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(project_data))
    item = manifest.document["work_items"][0]
    attempt_id = "550e8400-e29b-41d4-a716-446655440010"
    ordinal, attempt = simulation_record_store.allocate_attempt_directory(
        str(item["work_item_id"]), attempt_id
    )
    child_id = _unique_execution_id()
    registry = ChildExecutionRegistry(manifest, project)
    prepared = registry.prepare(
        child_id,
        work_item_id=str(item["work_item_id"]),
        attempt_id=attempt_id,
        attempt_ordinal=ordinal,
        attempt_directory=attempt,
        parent_execution_id="a" * 32,
    )
    flow = FlowMechanics()
    flow.args = SimpleNamespace(work_dir=project)
    return project_data, child_id, registry, prepared, flow


def test_child_scope_keeps_whole_work_item_nonterminal_between_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_data, child_id, registry, prepared, flow = _prepared_child(tmp_path, monkeypatch)
    scope = SupervisedExecutionScope(child_id, project_data, lambda: False)
    # _execute_local: FlowMechanics has no public command runner; these tests pin its
    # cancellation gate between processes.
    with supervised_execution_scope(scope):
        first = flow._execute_local(
            [sys.executable, "-c", "print('first')"], timeout=hang_guard_s()
        )
        between = read_json(execution_paths(child_id, booley_dir=project_data).record)
        second = flow._execute_local(
            [sys.executable, "-c", "print('second')"], timeout=hang_guard_s()
        )
    record = read_json(execution_paths(child_id, booley_dir=project_data).record)
    assert first.returncode == second.returncode == 0
    assert first.stdout.strip() == "first"
    assert second.stdout.strip() == "second"
    assert between is not None and between["state"] == "waiting"
    assert record is not None
    assert record["state"] == "waiting"
    assert record["tree_terminal"] is False
    registry.mark_terminal(prepared, "completed")
    terminal = read_json(execution_paths(child_id, booley_dir=project_data).record)
    assert terminal is not None and terminal["tree_terminal"] is True


def test_cancelled_scope_rejects_a_later_command_between_processes(
    tmp_path: Path,
) -> None:
    cancelled = threading.Event()
    scope = SupervisedExecutionScope(None, tmp_path, cancelled.is_set)
    flow = FlowMechanics()
    flow.args = SimpleNamespace(work_dir=tmp_path)
    marker = tmp_path / "must-not-run"
    with supervised_execution_scope(scope):
        first = flow._execute_local(
            [sys.executable, "-c", "print('first')"], timeout=hang_guard_s()
        )
        cancelled.set()
        second = flow._execute_local(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            timeout=hang_guard_s(),
        )
    assert first.returncode == 0
    assert second.returncode == -1
    assert not marker.exists()


def test_process_registration_closes_constructor_cancellation_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = threading.Event()
    cancelled.set()  # cancellation raced after the caller's pre-spawn check
    killed: list[object] = []
    monkeypatch.setattr("booley.runtime.supervised_execution.kill_process_tree", killed.append)
    process = object()
    processes = SupervisedProcessSet(None, tmp_path, cancelled.is_set)
    processes.register(process)  # type: ignore[arg-type]
    assert killed == [process]


def test_real_child_process_is_terminated_immediately_on_lease_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed_capacity
) -> None:
    project_data, _child_id, registry, _prepared, flow = _prepared_child(tmp_path, monkeypatch)
    _store, capacity = managed_capacity
    lane_ids: list[ExecutionId] = []
    child_registered = threading.Event()
    original_register = SupervisedProcessSet.register

    def register_and_signal(processes, process) -> None:
        original_register(processes, process)
        child_registered.set()

    monkeypatch.setattr(SupervisedProcessSet, "register", register_and_signal)
    started = time.monotonic()
    with (
        pytest.raises(SimulationRecordCapacityError, match="lease lost"),
        capacity.outer_permit(),
        _child_permit(capacity) as permit,
    ):
        lane_ids.append(permit.child_execution_id)
        record_path = execution_paths(permit.child_execution_id, booley_dir=project_data).record

        def lose_lease() -> None:
            assert child_registered.wait(timeout=hang_guard_s())
            assert (read_json(record_path) or {}).get("state") == "running"
            permit.lease_health.lost.set()

        killer = threading.Thread(target=lose_lease)
        killer.start()
        with supervised_execution_scope(permit.scope):
            flow._execute_local(
                [sys.executable, "-c", "import time; time.sleep(600)"], timeout=hang_guard_s()
            )
        killer.join(timeout=hang_guard_s())
    assert time.monotonic() - started < scaled(8)
    assert registry.is_terminal(lane_ids[0])


@pytest.mark.parametrize("cancel_after", [False, True])
def test_child_permit_checks_lease_loss_before_return(
    managed_capacity, cancel_after: bool
) -> None:
    store, capacity = managed_capacity
    with (
        capacity.outer_permit(),
        pytest.raises(LeaseLostError, match="lease lost"),
        _child_permit(capacity) as permit,
    ):
        _remove_slot_entry(store, permit.token)  # lost to a reaper, not to the cancel
        if cancel_after:
            capacity.cancel_waiters("test cancellation")


def test_cancel_while_a_lane_holds_its_slot_aborts_instead_of_losing_its_lease(
    tmp_path: Path, managed_capacity
) -> None:
    """A cancel retires a held lane as an abort and leaves its lease healthy."""
    store, capacity = managed_capacity
    with (
        capacity.outer_permit(),
        pytest.raises(ClaimAbortedError, match="cancellation"),
        _child_permit(capacity) as permit,
    ):
        capacity.cancel_waiters("test cancellation")
    assert not permit.lease_health.lost.is_set()
    record = read_json(
        execution_paths(
            permit.child_execution_id, booley_dir=_lane_registry(tmp_path).project_data
        ).record
    )
    assert record is not None
    assert record["terminal_cause"] == "orphan_recovered"
    _assert_only_the_outer_claim_remains(store)


def test_a_cancelled_lane_whose_claim_is_reaped_still_aborts(tmp_path: Path) -> None:
    """The cancelled execution's claim, reaped by any store scan, is the cancel's loss."""
    terminal: list[bool] = []
    store = SlotStore(
        tmp_path / "slots",
        recovery=SlotRecovery(execution_is_terminal=lambda _execution_id: bool(terminal)),
    )
    outer = _outer_claim(store)
    capacity = _capacity(_managed(store, outer), tmp_path)
    try:
        with (
            capacity.outer_permit(),
            pytest.raises(ClaimAbortedError, match="cancellation"),
            _child_permit(capacity) as permit,
        ):
            capacity.cancel_waiters("test cancellation")
            terminal.append(True)  # the cancel ended the execution; a scan reaps its claim
            store.snapshot(CLASS_MEMORY)
            assert store.token_absent(permit.token)
    finally:
        store.release(outer)


@pytest.mark.parametrize("lost_first", [False, True])
def test_a_waiter_promoted_after_the_cancel_snapshot_still_aborts(
    blocked_capacity, monkeypatch: pytest.MonkeyPatch, lost_first: bool
) -> None:
    """A claim promoted between the cancel's snapshot and its withdrawal aborts.

    One an independent recovery already took (``lost_first``) stays a lost lease.
    """
    store, capacity, blocker = blocked_capacity
    refresh, withdraw = store.refresh, store.cancel_waiter_token
    queued: list[SlotToken] = []
    states: list[str] = []

    def cancel_on_first_poll(token: SlotToken):
        if queued:
            return refresh(token)
        queued.append(token)
        capacity.cancel_waiters("test cancellation")  # snapshots the still-queued token
        return TokenState(HOLDING)  # the state the promotion left, as the lane sees it

    def promote_then_withdraw(token: SlotToken) -> bool:
        store.release(blocker)
        states.append(refresh(token).state)  # the owner promotes the token first
        if lost_first:
            store.begin_token_recovery(token)  # another cause, before the cancel's recovery
        return withdraw(token)

    monkeypatch.setattr(store, "refresh", cancel_on_first_poll)
    monkeypatch.setattr(store, "cancel_waiter_token", promote_then_withdraw)
    started: list[bool] = []
    with (
        capacity.outer_permit(),
        pytest.raises(LeaseLostError if lost_first else ClaimAbortedError),
        _child_permit(capacity),
    ):
        started.append(True)
    assert started == []
    assert states == [HOLDING]
    assert queued[0].lease_health.lost.is_set()
    _assert_only_the_outer_claim_remains(store)


@pytest.mark.parametrize("lost_first", [False, True])
def test_a_claim_granted_before_its_item_starts_aborts_unless_a_recovery_took_it_first(
    managed_capacity, monkeypatch: pytest.MonkeyPatch, lost_first: bool
) -> None:
    """A claim granted as the record is cancelled never starts its item (#3846).
    A recovery the cancel did not start (``lost_first``) is a lost lease, not an abort (#4492)."""
    store, capacity = managed_capacity
    acquire = store.acquire

    def acquire_then_recover(*args, **kwargs):
        token = acquire(*args, **kwargs)
        if lost_first:
            store.begin_token_recovery(token)  # another cause, before the cancel
        return token

    def threads_then_cancel(*args, **kwargs):
        capacity.cancel_waiters("test cancellation")  # the claim is already granted
        return job_threads_at_start(*args, **kwargs)

    monkeypatch.setattr(store, "acquire", acquire_then_recover)
    monkeypatch.setattr(
        "booley.flows.sim.record.capacity.job_threads_at_start", threads_then_cancel
    )
    started: list[bool] = []
    with (
        capacity.outer_permit(),
        pytest.raises(
            LeaseLostError if lost_first else ClaimAbortedError,
            match="lease lost" if lost_first else "cancellation",
        ),
        _child_permit(capacity),
    ):
        started.append(True)
    assert started == []
    _assert_only_the_outer_claim_remains(store)


def test_unmanaged_capacity_borrows_one_lane_and_never_claims_a_child(tmp_path: Path) -> None:
    capacity = _capacity(_admission(), tmp_path)
    with capacity.outer_permit() as permit:
        assert permit.execution_id is None
        assert isinstance(capacity.parent_execution_id, ExecutionId)
        assert capacity.max_lanes == 1
        assert capacity.managed is False
    with (
        pytest.raises(SimulationRecordCapacityError, match="exactly once"),
        capacity.outer_permit(),
    ):
        pass
    with (
        pytest.raises(SimulationRecordCapacityError, match="cannot acquire child"),
        _child_permit(capacity),
    ):
        pass


def test_managed_capacity_releases_and_reacquires_exact_child_tokens(managed_capacity) -> None:
    store, capacity = managed_capacity
    with capacity.outer_permit():
        for index in range(2):
            with _child_permit(capacity, f"item-{index}") as permit:
                holders, waiters = store.snapshot(CLASS_MEMORY)
                assert waiters == []
                assert {token.execution_id for token in holders} == {
                    ExecutionId("a" * 32),
                    permit.child_execution_id,
                }
                assert permit.token.execution_id == permit.child_execution_id
            holders, waiters = store.snapshot(CLASS_MEMORY)
            assert [token.execution_id for token in holders] == [ExecutionId("a" * 32)]
            assert waiters == []


def test_capacity_cancellation_withdraws_a_real_queued_child(blocked_capacity) -> None:
    store, capacity, _blocker = blocked_capacity
    errors: list[BaseException] = []

    def wait_for_child() -> None:
        try:
            with _child_permit(capacity, "queued"):
                raise AssertionError("queued child unexpectedly acquired a slot")
        except BaseException as exc:  # noqa: BLE001 -- assertion inspects worker outcome
            errors.append(exc)

    worker = threading.Thread(target=wait_for_child)
    worker.start()
    _wait_until(lambda: len(store.snapshot(CLASS_MEMORY)[1]) == 1)
    capacity.cancel_waiters()
    worker.join(timeout=hang_guard_s())
    assert not worker.is_alive()
    assert len(errors) == 1
    assert store.snapshot(CLASS_MEMORY)[1] == []


def test_child_reacquisition_rejoins_real_slot_fifo_behind_competitor(managed_capacity) -> None:
    store, capacity = managed_capacity
    competitor = None
    try:
        with capacity.outer_permit(), _child_permit(capacity, "first"):
            competitor = store.submit(
                CLASS_MEMORY,
                pid=os.getpid(),
                role=ROLE_INTERACTIVE,
                execution_id=ExecutionId("c" * 32),
                memory=_holder_claim(1),
            )
            assert store.refresh(competitor).state == QUEUED
        assert store.refresh(competitor).state == HOLDING
    finally:
        if competitor is not None:
            store.release(competitor)


def test_scheduler_excludes_equal_collision_keys_before_execution(tmp_path: Path, slots) -> None:
    manifest, simulation_record_store, project = _record_project(tmp_path)
    registry = ChildExecutionRegistry(manifest, project)
    slot_store, outer = slots
    active = 0
    maximum_active = 0
    gate = threading.Lock()

    def allocate(item) -> ScheduledAttempt:
        return replace(
            _scheduled_attempt(simulation_record_store, item), collision_key="same-run-dir"
        )

    def execute(_attempt, _child_id) -> None:
        nonlocal active, maximum_active
        with gate:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with gate:
            active -= 1

    capacity = _item_capacity(_managed(slot_store, outer), manifest, registry.project_data)
    BoundedSimulationRecordScheduler(capacity, allocate=allocate, execute=execute).run(
        manifest.document["work_items"]
    )

    assert maximum_active == 1
    _assert_only_the_outer_claim_remains(slot_store)


def test_normal_work_runtime_does_not_consume_shutdown_budget(tmp_path: Path) -> None:
    manifest, simulation_record_store, project = _record_project(tmp_path)
    admission = AdmissionContext(
        "unmanaged", None, None, "interactive", "a" * 32, 0.01, lambda: False
    )
    capacity = SimulationRecordCapacity(admission, manifest, project / ".booley")
    started = time.monotonic()
    BoundedSimulationRecordScheduler(
        capacity,
        allocate=lambda item: _scheduled_attempt(simulation_record_store, item),
        execute=lambda *_args: time.sleep(0.04),
    ).run(manifest.document["work_items"])
    assert time.monotonic() - started >= 0.07


class _ExitsDuringFinalWait(threading.Event):
    """Lane-exit event that is set just as the owner's timed wait gives up."""

    def wait(self, timeout: float | None = None) -> bool:
        self.set()
        return False


def test_shutdown_deadline_does_not_fail_when_every_worker_already_exited(
    tmp_path: Path,
) -> None:
    """Regression for #977: a lane that exits in the final wait is a clean shutdown."""
    manifest = _two_item_manifest()
    simulation_record_store = SimulationRecordStore(tmp_path / "record")
    simulation_record_store.publish_manifest(manifest)
    # A zero budget means the shutdown deadline has always passed after the wait.
    capacity = SimpleNamespace(
        managed=False,
        shutdown_requested=True,
        shutdown_timeout_seconds=0.0,
        cancel_waiters=lambda: None,
        hold_outer_until=lambda _exited: pytest.fail("held the outer slot after a clean exit"),
    )
    scheduler = BoundedSimulationRecordScheduler(
        capacity,  # type: ignore[arg-type]
        allocate=lambda item: _scheduled_attempt(simulation_record_store, item),
        execute=lambda *_args: None,
    )

    # _await_lanes: run() creates its own lane-exit event, so the final-wait race cannot be forced through it.
    scheduler._await_lanes([], _ExitsDuringFinalWait())


@pytest.mark.parametrize("first_finisher", ["beta", "gamma"])
@pytest.mark.parametrize("schedule_seed", (0, 3))
def test_managed_simulation_record_bounds_overlap_keeps_order_and_cleans_child_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slots,
    first_finisher: str,
    schedule_seed: int,
) -> None:
    manifest = _three_item_manifest()
    plan = create_simulation_record_plan(manifest)
    project, state = _parallel_environment(tmp_path, first_finisher, schedule_seed)
    executor = _parallel_executor(monkeypatch, state)
    outcome, slot_store, invocation = _run_parallel_simulation_record(
        tmp_path, manifest, plan, project, executor, slots
    )
    _assert_parallel_outcome(outcome, plan, state, first_finisher)
    _assert_child_claim_integrity(invocation, slot_store, plan, project / ".booley")


def _parallel_executor(monkeypatch: pytest.MonkeyPatch, state) -> OrdinaryHdlSerialExecutor:
    """An executor whose every work item runs the shared ``_ParallelExecution`` of *state*."""
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: state.handle),
    )
    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _ParallelExecution(state),  # type: ignore[arg-type,return-value]
    )


def _run_parallel_simulation_record(tmp_path, manifest, plan, project, executor, slots):
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    slot_store, outer = slots
    outcome = SimulationRecord(executor, work_item_request=lambda *_args: None).run(
        NewSimulationRecordRunRequest(
            plan,
            project,
            invocation.parent,
            SimulationRecordPolicy(),
            invocation,
            _managed(slot_store, outer),
        )
    )
    _assert_only_the_outer_claim_remains(slot_store)
    return outcome, slot_store, invocation


def _assert_parallel_outcome(outcome, plan, state, first_finisher) -> None:
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert state.compile_count == 1
    by_name = {name: (start, end) for name, start, end in state.intervals}
    assert by_name["beta"][0] <= by_name["gamma"][1]
    assert by_name["gamma"][0] <= by_name["beta"][1]
    assert state.completion_order.index(first_finisher) < state.completion_order.index(
        "gamma" if first_finisher == "beta" else "beta"
    )


def _assert_child_claim_integrity(invocation, slot_store, plan, project_data) -> None:
    store = SimulationRecordStore(invocation / "targets/sim/record")
    summary = json.loads(store.summary_path.read_text())
    assert summary["completed"] == list(plan.work_item_ids)
    attempts = [
        document
        for document in (
            json.loads(path.read_bytes())
            for path in store.root.glob("work-items/*/attempts/*/attempt.json")
        )
        if document["child_execution_id"] is not None
    ]
    assert attempts, "the parallel run scheduled no lane attempt"
    for attempt in attempts:
        assert attempt["child_entry_sha256"] is None
        lane = execution_paths(ExecutionId(attempt["child_execution_id"]), booley_dir=project_data)
        assert lane.context.is_file()
        record = read_json(lane.record)
        assert record["state"] == "terminal" and record["tree_terminal"] is True
    _assert_only_the_outer_claim_remains(slot_store)


@pytest.mark.parametrize(
    "case", ["record_error", "coverage_scope_stop", "windows_sharing_violation"]
)
def test_scheduler_preserves_terminal_cause_after_actual_child_lease_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slots, case: str
) -> None:
    denials_left = 2
    if case == "windows_sharing_violation":
        # A transient WinError 32 on the child's entry removal is retried, not raised (#3982).
        memory_dir = tmp_path / "slots" / CLASS_MEMORY
        real_unlink = Path.unlink
        denial_lock = threading.Lock()

        def sharing_violating_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            """Deny the first two holder-entry removals, like a concurrent reader's open handle."""
            nonlocal denials_left
            if path.parent == memory_dir and path.name.startswith("h-"):
                with denial_lock:
                    if denials_left > 0:
                        denials_left -= 1
                        raise sharing_error()
            real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", sharing_violating_unlink)
    scope_stop = "coverage" if case == "coverage_scope_stop" else False
    registry, child_ids = _run_lease_loss_scheduler(tmp_path, monkeypatch, slots, scope_stop)
    if case == "windows_sharing_violation":
        assert denials_left == 0
    assert len(child_ids) == 1
    terminal = read_json(
        execution_paths(ExecutionId(child_ids[0]), booley_dir=registry.project_data).record
    )
    assert terminal["terminal_cause"] == ("lease_lost" if scope_stop else "record_error")
    _assert_only_the_outer_claim_remains(slots[0])


def _run_lease_loss_scheduler(tmp_path, monkeypatch, slots, scope_stop):
    """Run two lanes whose first one loses its lease; return what the assertions read."""
    monkeypatch.setattr("booley.runtime.job_slots.LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    manifest, simulation_record_store, project = _record_project(tmp_path)
    registry = ChildExecutionRegistry(manifest, project)
    store, outer = slots
    child_finished = threading.Event()
    child_ids = []

    execute = _lease_lost_execute(store, child_finished, child_ids, scope_stop, tmp_path)

    # Recovery never completes, so the terminal cause comes from the lane itself.
    monkeypatch.setattr(ChildExecutionRegistry, "cancel", lambda _registry, _execution_id: False)
    capacity = _item_capacity(
        # Keep this cause test within the per-test limit.
        replace(_managed(store, outer), timeout_seconds=hang_guard_s()),
        manifest,
        registry.project_data,
    )
    scheduler = BoundedSimulationRecordScheduler(
        capacity,
        allocate=lambda item: _scheduled_attempt(simulation_record_store, item),
        execute=execute,
    )
    try:
        with pytest.raises(SimulationRecordCapacityError):
            scheduler.run(manifest.document["work_items"])
    finally:
        child_finished.set()
    return registry, child_ids


def _lease_lost_execute(slots, child_finished, child_ids, scope_stop, tmp_path):
    from booley.flows.sim.execution.contract import PreSimScopeStoppedError
    from booley.runtime.supervised_execution import current_supervised_execution

    first_lane = threading.Lock()

    def execute(_attempt, child_id, *_entry_digest):
        if not first_lane.acquire(blocking=False):
            # The other lane works until the first one's lease is lost.
            assert child_finished.wait(_PARALLEL_SYNC_TIMEOUT_S)
            return
        child_ids.append(child_id)
        scope = current_supervised_execution()
        assert scope is not None
        _wait_until(lambda: len(slots.snapshot(CLASS_MEMORY)[0]) == 3)
        _remove_child_lease(slots, child_id)
        _wait_until(scope.cancelled)
        child_finished.set()
        if scope_stop == "coverage":
            try:
                _execute_pre_cancelled_coverage(tmp_path)
            except PreSimScopeStoppedError as exc:
                raise SimulationRecordCapacityError("scope stopped") from exc
        raise SimulationRecordCapacityError("unrelated publication failure")

    return execute


def _execute_pre_cancelled_coverage(tmp_path):
    from booley.flows.sim.execution.contract import PreSimScopeStoppedError
    from booley.flows.sim.record.coverage_execution import CoverageAggregateExecutor
    from tests.flows.sim.simulation_record_test_support import (
        _maximum_hook_selection_plan,
        _request,
    )

    plan, _ = _maximum_hook_selection_plan(("alpha",))
    store = SimulationRecordStore(tmp_path / "coverage-cancel")
    store.publish_manifest(plan.manifest)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )

    def must_not_spawn(*_args):
        pytest.fail("pre-cancelled coverage must not construct or spawn execution")

    executor = CoverageAggregateExecutor(plans={}, execution_factory=must_not_spawn)
    executor.prepare_attempt(request)
    try:
        executor.execute(request)
    except PreSimScopeStoppedError:
        assert not list(store.root.glob("**/pre-sim/*.json"))
        raise
    pytest.fail("actual coverage scope gate must raise PreSimScopeStoppedError")


@pytest.mark.parametrize("error", ["truncated", "schema", "missing"])
def test_child_context_reader_rejects_corrupt_record(tmp_path, monkeypatch, error):
    project_data, child_id, _registry, _prepared, _flow = _prepared_child(tmp_path, monkeypatch)
    paths = execution_paths(child_id, booley_dir=project_data)
    assert child_context_matches(paths, child_id)
    document = json.loads(paths.context.read_bytes())
    if error == "schema":
        document["$schema"] = "unknown"
    elif error == "missing":
        del document["attempt_id"]
    paths.context.chmod(0o600)
    paths.context.write_bytes(b"{" if error == "truncated" else canonical_json_bytes(document))
    assert child_context_matches(paths, child_id) is False


def test_canonical_collision_keys_are_stable_across_threads(tmp_path: Path) -> None:
    literal = tmp_path / "literal"
    literal.mkdir()
    first = expand_run_directory(
        str(literal),
        project_root=tmp_path,
        simulation_record_id=str(uuid.uuid4()),
        target_key="sim",
        work_item_key="item",
        attempt_key="attempt",
    )
    alias = expand_run_directory(
        str(literal / ".." / "literal"),
        project_root=tmp_path,
        simulation_record_id=str(uuid.uuid4()),
        target_key="sim",
        work_item_key="item",
        attempt_key="attempt",
    )
    barrier = threading.Barrier(2)
    keys: list[str] = []

    def claim(run: RunDirectory) -> None:
        barrier.wait()
        with claimed_run_directory(run, identity=_run_identity()):
            keys.append(run.collision_key)

    threads = [threading.Thread(target=claim, args=(run,)) for run in (first, alias)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert first.collision_key == alias.collision_key
    assert keys == [first.collision_key, first.collision_key]


def test_unsized_memory_admitted_simulation_record_runs_work_items_without_child_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Flow admitted by memory whose Simulation Record sizes no items runs them in series (#3701)."""
    from booley.flows import endpoint_admission
    from booley.flows.endpoint_admission import AdmissionGate
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.runtime.job_slots import CLASS_MEMORY

    manifest = _three_item_manifest()
    plan = create_simulation_record_plan(manifest)
    project, state = _parallel_environment(tmp_path, "beta", 0)
    # One item at a time: nothing waits for a peer to overlap.
    state.barrier = threading.Barrier(1)
    executor = _parallel_executor(monkeypatch, state)
    booley_dir = project / ".booley"
    pin_budget(monkeypatch, 1000)
    # A fake /proc with no process: ranking never touches the worker's real OOM priority.
    monkeypatch.setattr("booley.runtime.oom_priority.PROC", FakeProc())
    monkeypatch.setenv("BOOLEY_RUNTIME_EXECUTION_ID", "a" * 32)
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    monkeypatch.delenv(INHERITED_SLOT_ENV, raising=False)
    monkeypatch.setattr(endpoint_admission, "resolve_common_booley_dir", lambda: booley_dir)
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    with AdmissionGate(AsicSynthesizeFlow()).enter() as admission:
        assert admission.slot_store is not None
        outcome = SimulationRecord(executor).run(
            NewSimulationRecordRunRequest(
                plan, project, invocation.parent, SimulationRecordPolicy(), invocation, admission
            )
        )
        (outer,) = admission.slot_store.snapshot(CLASS_MEMORY)[0]
        assert outer.job_class == CLASS_MEMORY

    assert outcome.complete is True
    # The items ran in series: each ended before the next began.
    runs = sorted(state.intervals, key=lambda interval: interval[1])
    assert [name for name, _start, _end in runs] == ["alpha", "beta", "gamma"]
    assert all(left[2] <= right[1] for left, right in pairwise(runs))
    store = SimulationRecordStore(invocation / "targets/sim/record")
    attempts = [
        json.loads(path.read_bytes())
        for path in store.root.glob("work-items/*/attempts/*/attempt.json")
    ]
    assert len(attempts) == 3
    assert [attempt["child_execution_id"] for attempt in attempts] == [None] * 3
