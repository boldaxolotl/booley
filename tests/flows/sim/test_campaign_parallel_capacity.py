"""Bounded parallel execution, capacity, cancellation and lease loss of Simulation Campaigns."""

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

import pytest

from booley.config.jobs import SlotCaps
from booley.flows.base import FlowMechanics
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign.capacity import HeavyCapacity, HeavyCapacityError
from booley.flows.sim.campaign.child_protocol import ChildExecutionRegistry
from booley.flows.sim.campaign.codec import (
    canonical_json_bytes,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.run_directory import (
    RunDirectory,
    claimed_run_directory,
    expand_run_directory,
)
from booley.flows.sim.campaign.scheduler import (
    BoundedCampaignScheduler,
    CampaignSchedulingError,
    ScheduledAttempt,
)
from booley.flows.sim.campaign.serial_execution import (
    OrdinaryHdlSerialExecutor,
)
from booley.flows.sim.campaign.store import CampaignStore
from booley.runtime.execution_records import (
    ExecutionId,
    child_context_matches,
    execution_paths,
    read_json,
)
from booley.runtime.job_slots import (
    CLASS_HEAVY,
    HOLDING,
    INHERITED_SLOT_ENV,
    QUEUED,
    ROLE_INTERACTIVE,
    ClaimAbortedError,
    SlotStore,
)
from booley.runtime.supervised_execution import (
    SupervisedExecutionScope,
    SupervisedProcessSet,
    supervised_execution_scope,
)
from tests.flows.sim.campaign_test_support import (
    _PARALLEL_SYNC_TIMEOUT_S,
    _admission,
    _capacity,
    _lane_registry,
    _managed,
    _parallel_environment,
    _ParallelExecution,
    _run_identity,
    _scheduled_attempt,
    _three_item_manifest,
    _two_item_manifest,
    _wait_until,
)
from tests.oom_priority_support import FakeProc


def _unique_execution_id() -> ExecutionId:
    """A per-test runtime execution ID.

    Real recovery scans every process on the host for its execution ID, so a
    shared literal would let one xdist worker's recovery signal another's child.
    """
    return ExecutionId(uuid.uuid4().hex)


def _child_permit(capacity: HeavyCapacity, work_item_id: str = "item"):
    """Enter a child lane for a dummy attempt."""
    return capacity.child_permit(
        work_item_id,
        attempt_id="attempt",
        attempt_ordinal=1,
        attempt_directory=Path("attempt"),
        publish=lambda _child: None,
    )


def _prepared_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "record")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    project_data = project / ".booley"
    project_data.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(project_data))
    item = manifest.document["work_items"][0]
    attempt_id = "550e8400-e29b-41d4-a716-446655440010"
    ordinal, attempt = campaign_store.allocate_attempt_directory(
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
    with supervised_execution_scope(scope):
        first = flow._execute_local([sys.executable, "-c", "print('first')"], timeout=5)
        between = read_json(execution_paths(child_id, booley_dir=project_data).record)
        second = flow._execute_local([sys.executable, "-c", "print('second')"], timeout=5)
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
        first = flow._execute_local([sys.executable, "-c", "print('first')"], timeout=5)
        cancelled.set()
        second = flow._execute_local(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            timeout=5,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_data, _child_id, registry, _prepared, flow = _prepared_child(tmp_path, monkeypatch)
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = _capacity(_managed(store, outer), tmp_path)
    lane_ids: list[ExecutionId] = []
    child_registered = threading.Event()
    original_register = SupervisedProcessSet.register

    def register_and_signal(processes, process) -> None:
        original_register(processes, process)
        child_registered.set()

    monkeypatch.setattr(SupervisedProcessSet, "register", register_and_signal)
    started = time.monotonic()
    try:
        with (
            pytest.raises(HeavyCapacityError, match="lease lost"),
            capacity.outer_permit(),
            _child_permit(capacity) as permit,
        ):
            lane_ids.append(permit.child_execution_id)
            record_path = execution_paths(
                permit.child_execution_id, booley_dir=project_data
            ).record

            def lose_lease() -> None:
                assert child_registered.wait(timeout=2)
                assert (read_json(record_path) or {}).get("state") == "running"
                permit.lease_health.lost.set()

            killer = threading.Thread(
                target=lose_lease,
            )
            killer.start()
            with supervised_execution_scope(permit.scope):
                flow._execute_local(
                    [sys.executable, "-c", "import time; time.sleep(30)"], timeout=35
                )
            killer.join(timeout=2)
    finally:
        store.release(outer)
    assert time.monotonic() - started < 8
    assert registry.is_terminal(lane_ids[0])


def test_child_permit_checks_lease_loss_before_return(tmp_path: Path) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = _capacity(_managed(store, outer), tmp_path)
    try:
        with (
            capacity.outer_permit(),
            pytest.raises(HeavyCapacityError, match="lease lost"),
            _child_permit(capacity) as permit,
        ):
            permit.token.path.unlink()
    finally:
        store.release(outer)


def test_child_permit_never_yields_a_claim_admitted_after_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim admitted in the same moment the campaign is cancelled never starts its item."""
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = _capacity(_managed(store, outer), tmp_path)
    acquire_child = capacity._acquire_child

    def acquire_then_cancel(*args, **kwargs):
        token = acquire_child(*args, **kwargs)
        capacity.cancel_waiters("test cancellation")  # lands while the claim was granted
        return token

    monkeypatch.setattr(capacity, "_acquire_child", acquire_then_cancel)
    started: list[bool] = []
    try:
        with (
            capacity.outer_permit(),
            pytest.raises(ClaimAbortedError, match="cancellation"),
            _child_permit(capacity),
        ):
            started.append(True)
    finally:
        store.release(outer)
    assert started == []
    assert store.snapshot(CLASS_HEAVY) == ([], [])


def test_unmanaged_capacity_borrows_one_lane_and_never_claims_a_child(tmp_path: Path) -> None:
    capacity = _capacity(_admission(), tmp_path)
    with capacity.outer_permit() as permit:
        assert permit.execution_id is None
        assert isinstance(capacity.parent_execution_id, ExecutionId)
        assert capacity.max_lanes == 1
        assert capacity.managed is False
    with pytest.raises(HeavyCapacityError, match="exactly once"), capacity.outer_permit():
        pass
    with (
        pytest.raises(HeavyCapacityError, match="cannot acquire child"),
        _child_permit(capacity),
    ):
        pass


def test_managed_capacity_releases_and_reacquires_exact_child_tokens(
    tmp_path: Path,
) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = _capacity(_managed(store, outer), tmp_path)
    try:
        with capacity.outer_permit():
            for index in range(2):
                with _child_permit(capacity, f"item-{index}") as permit:
                    holders, waiters = store.snapshot(CLASS_HEAVY)
                    assert waiters == []
                    assert {token.execution_id for token in holders} == {
                        ExecutionId("a" * 32),
                        permit.child_execution_id,
                    }
                    assert permit.token.execution_id == permit.child_execution_id
                holders, waiters = store.snapshot(CLASS_HEAVY)
                assert [token.execution_id for token in holders] == [ExecutionId("a" * 32)]
                assert waiters == []
    finally:
        store.release(outer)
    assert store.snapshot(CLASS_HEAVY) == ([], [])


def test_real_slot_store_preserves_interactive_priority_and_peer_fifo(
    tmp_path: Path,
) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    holder = store.acquire(CLASS_HEAVY, pid=os.getpid())
    first_ticket = store.submit(CLASS_HEAVY, pid=os.getpid(), role="background")
    second_ticket = store.submit(CLASS_HEAVY, pid=os.getpid(), role="background")
    interactive = store.submit(CLASS_HEAVY, pid=os.getpid(), role=ROLE_INTERACTIVE)
    try:
        holders, waiters = store.snapshot(CLASS_HEAVY)
        assert [token.lease_id for token in holders] == [holder.lease_id]
        assert [token.lease_id for token in waiters] == [
            interactive.lease_id,
            first_ticket.lease_id,
            second_ticket.lease_id,
        ]
        store.release(holder)
        assert store.refresh(interactive).state == HOLDING
        assert store.refresh(first_ticket).state == QUEUED
        store.release(interactive)
        assert store.refresh(first_ticket).state == HOLDING
        assert store.refresh(second_ticket).state == QUEUED
    finally:
        for token in (holder, interactive, first_ticket, second_ticket):
            store.release(token)
    assert store.snapshot(CLASS_HEAVY) == ([], [])


def test_capacity_cancellation_withdraws_a_real_queued_child(tmp_path: Path) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = _capacity(_managed(store, outer, max_heavy=1), tmp_path)
    errors: list[BaseException] = []

    def wait_for_child() -> None:
        try:
            with _child_permit(capacity, "queued"):
                raise AssertionError("queued child unexpectedly acquired a slot")
        except BaseException as exc:  # noqa: BLE001 -- assertion inspects worker outcome
            errors.append(exc)

    worker = threading.Thread(target=wait_for_child)
    worker.start()
    try:
        _wait_until(lambda: len(store.snapshot(CLASS_HEAVY)[1]) == 1)
        capacity.cancel_waiters()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert store.snapshot(CLASS_HEAVY)[1] == []
    finally:
        store.release(outer)


def test_capacity_exception_recovers_then_releases_exact_child(tmp_path: Path) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = _capacity(_managed(store, outer), tmp_path)
    try:
        with (
            capacity.outer_permit(),
            pytest.raises(RuntimeError, match="injected worker failure"),
            _child_permit(capacity) as permit,
        ):
            raise RuntimeError("injected worker failure")
        holders, waiters = store.snapshot(CLASS_HEAVY)
        assert [token.execution_id for token in holders] == [ExecutionId("a" * 32)]
        assert waiters == []
        assert _lane_registry(tmp_path).is_terminal(permit.child_execution_id)
    finally:
        store.release(outer)


def test_child_reacquisition_rejoins_real_slot_fifo_behind_competitor(
    tmp_path: Path,
) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = _capacity(_managed(store, outer), tmp_path)
    competitor = None
    try:
        with capacity.outer_permit(), _child_permit(capacity, "first"):
            competitor = store.submit(
                CLASS_HEAVY,
                pid=os.getpid(),
                role=ROLE_INTERACTIVE,
                execution_id=ExecutionId("c" * 32),
            )
            assert store.refresh(competitor).state == QUEUED
        assert store.refresh(competitor).state == HOLDING
    finally:
        if competitor is not None:
            store.release(competitor)
        store.release(outer)
    assert store.snapshot(CLASS_HEAVY) == ([], [])


def test_scheduler_excludes_equal_collision_keys_before_execution(tmp_path: Path) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "record")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    registry = ChildExecutionRegistry(manifest, project)
    slot_store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = slot_store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    active = 0
    maximum_active = 0
    gate = threading.Lock()

    def allocate(item) -> ScheduledAttempt:
        attempt_id = os.urandom(16).hex()
        ordinal, directory = campaign_store.allocate_attempt_directory(
            str(item["work_item_id"]), attempt_id
        )
        return ScheduledAttempt(item, attempt_id, ordinal, directory, "same-run-dir")

    def execute(_attempt, _child_id) -> None:
        nonlocal active, maximum_active
        with gate:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with gate:
            active -= 1

    capacity = HeavyCapacity(_managed(slot_store, outer), manifest, registry.project_data)
    try:
        BoundedCampaignScheduler(capacity, allocate=allocate, execute=execute).run(
            manifest.document["work_items"]
        )
    finally:
        slot_store.release(outer)

    assert maximum_active == 1
    assert slot_store.snapshot(CLASS_HEAVY) == ([], [])


def test_normal_work_runtime_does_not_consume_shutdown_budget(tmp_path: Path) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "record")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    admission = AdmissionContext(
        "unmanaged", None, None, 1, "interactive", "a" * 32, 0.01, lambda: False
    )
    capacity = HeavyCapacity(admission, manifest, project / ".booley")

    def allocate(item) -> ScheduledAttempt:
        attempt_id = os.urandom(16).hex()
        ordinal, directory = campaign_store.allocate_attempt_directory(
            str(item["work_item_id"]), attempt_id
        )
        return ScheduledAttempt(item, attempt_id, ordinal, directory, attempt_id)

    started = time.monotonic()
    BoundedCampaignScheduler(
        capacity,
        allocate=allocate,
        execute=lambda *_args: time.sleep(0.04),
    ).run(manifest.document["work_items"])
    assert time.monotonic() - started >= 0.07


def test_shutdown_deadline_returns_bounded_but_retains_outer_until_worker_exits(
    tmp_path: Path,
) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "record")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    slots = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    outer = slots.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    cancelled = threading.Event()
    release_worker = threading.Event()
    entered = threading.Event()
    admission = AdmissionContext(
        "managed", slots, outer, 1, "interactive", "a" * 32, 0.05, cancelled.is_set
    )
    capacity = HeavyCapacity(admission, manifest, project / ".booley")

    def execute(*_args) -> None:
        entered.set()
        release_worker.wait()

    scheduler = BoundedCampaignScheduler(
        capacity,
        allocate=lambda item: _scheduled_attempt(campaign_store, item),
        execute=execute,
    )
    trigger = threading.Thread(target=lambda: (entered.wait(), cancelled.set()))
    trigger.start()
    started = time.monotonic()
    with pytest.raises(CampaignSchedulingError, match="bounded shutdown"):
        scheduler.run(manifest.document["work_items"][:1])
    assert time.monotonic() - started < 0.5
    slots.release(outer)  # endpoint AdmissionGate finally must be deferred
    assert [token.lease_id for token in slots.snapshot(CLASS_HEAVY)[0]] == [outer.lease_id]
    assert campaign_store.scan().interrupted == (
        str(manifest.document["work_items"][0]["work_item_id"]),
    )
    release_worker.set()
    _wait_until(lambda: slots.token_absent(outer))
    trigger.join(timeout=1)


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
    campaign_store = CampaignStore(tmp_path / "record")
    campaign_store.publish_manifest(manifest)
    # A zero budget means the shutdown deadline has always passed after the wait.
    capacity = SimpleNamespace(
        managed=False,
        shutdown_requested=True,
        shutdown_timeout_seconds=0.0,
        cancel_waiters=lambda: None,
        hold_outer_until=lambda _exited: pytest.fail("held the outer slot after a clean exit"),
    )
    scheduler = BoundedCampaignScheduler(
        capacity,  # type: ignore[arg-type]
        allocate=lambda item: _scheduled_attempt(campaign_store, item),
        execute=lambda *_args: None,
    )

    scheduler._await_lanes([], _ExitsDuringFinalWait())


@pytest.mark.parametrize("first_finisher", ["alpha", "beta"])
@pytest.mark.parametrize("schedule_seed", (0, 3))
def test_managed_campaign_bounds_overlap_keeps_order_and_cleans_child_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_finisher: str,
    schedule_seed: int,
) -> None:
    manifest = _three_item_manifest()
    plan = create_simulation_campaign_plan(manifest)
    project, state = _parallel_environment(tmp_path, first_finisher, schedule_seed)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: state.handle),
    )
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _ParallelExecution(state),  # type: ignore[arg-type,return-value]
    )
    outcome, slot_store, invocation = _run_parallel_campaign(
        tmp_path, manifest, plan, project, executor
    )
    _assert_parallel_outcome(outcome, plan, state, first_finisher)
    _assert_child_claim_integrity(invocation, slot_store, plan, project / ".booley")


def _run_parallel_campaign(tmp_path, manifest, plan, project, executor):
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    slot_store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = slot_store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    try:
        outcome = SimulationCampaign(executor).run(
            NewCampaignRunRequest(
                plan,
                project,
                invocation.parent,
                CampaignPolicy(),
                invocation,
                _managed(slot_store, outer),
            )
        )
        holders, waiters = slot_store.snapshot(CLASS_HEAVY)
        assert [token.execution_id for token in holders] == [ExecutionId("a" * 32)]
        assert waiters == []
    finally:
        slot_store.release(outer)
    return outcome, slot_store, invocation


def _assert_parallel_outcome(outcome, plan, state, first_finisher) -> None:
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert state.compile_count == 1
    by_name = {name: (start, end) for name, start, end in state.intervals}
    assert by_name["alpha"][0] <= by_name["beta"][1]
    assert by_name["beta"][0] <= by_name["alpha"][1]
    assert state.completion_order.index(first_finisher) < state.completion_order.index(
        "beta" if first_finisher == "alpha" else "alpha"
    )


def _assert_child_claim_integrity(invocation, slot_store, plan, project_data) -> None:
    store = CampaignStore(invocation / "targets/sim/record")
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
    assert slot_store.snapshot(CLASS_HEAVY) == ([], [])


@pytest.mark.parametrize("scope_stop", [False, "coverage"])
def test_scheduler_preserves_terminal_cause_after_actual_child_lease_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope_stop: bool | str
) -> None:
    monkeypatch.setattr("booley.runtime.job_slots.LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "record")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    registry = ChildExecutionRegistry(manifest, project)
    slots = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = slots.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    child_finished = threading.Event()
    child_ids = []

    execute = _lease_lost_execute(slots, child_finished, child_ids, scope_stop, tmp_path)

    # Recovery never completes, so the terminal cause comes from the lane itself.
    monkeypatch.setattr(ChildExecutionRegistry, "cancel", lambda _registry, _execution_id: False)
    capacity = HeavyCapacity(
        # Keep this cause test within the production shutdown budget.
        replace(_managed(slots, outer), timeout_seconds=30.0),
        manifest,
        registry.project_data,
    )
    scheduler = BoundedCampaignScheduler(
        capacity,
        allocate=lambda item: _scheduled_attempt(campaign_store, item),
        execute=execute,
    )
    try:
        with pytest.raises(HeavyCapacityError):
            scheduler.run(manifest.document["work_items"])
    finally:
        child_finished.set()
        slots.release(outer)
    _assert_scheduler_terminal_cause(registry, campaign_store, child_ids, scope_stop)
    assert slots.snapshot(CLASS_HEAVY) == ([], [])


def _lease_lost_execute(slots, child_finished, child_ids, scope_stop, tmp_path):
    from booley.flows.sim.execution.contract import PreSimScopeStoppedError
    from booley.runtime.supervised_execution import current_supervised_execution

    def execute(_attempt, child_id, *_entry_digest):
        if child_id is None:
            assert child_finished.wait(_PARALLEL_SYNC_TIMEOUT_S)
            return
        child_ids.append(child_id)
        scope = current_supervised_execution()
        assert scope is not None
        _wait_until(lambda: len(slots.snapshot(CLASS_HEAVY)[0]) == 2)
        for token in slots.snapshot(CLASS_HEAVY)[0]:
            if str(token.execution_id) == child_id:
                with slots._lease_gate(token) as acquired:
                    assert acquired
                    token.path.unlink()
        _wait_until(scope.cancelled)
        child_finished.set()
        if scope_stop == "coverage":
            try:
                _execute_pre_cancelled_coverage(tmp_path)
            except PreSimScopeStoppedError as exc:
                raise HeavyCapacityError("scope stopped") from exc
        raise HeavyCapacityError("unrelated publication failure")

    return execute


def _assert_scheduler_terminal_cause(registry, campaign_store, child_ids, scope_stop):
    assert len(child_ids) == 1
    terminal = read_json(
        execution_paths(ExecutionId(child_ids[0]), booley_dir=registry.project_data).record
    )
    assert terminal["terminal_cause"] == ("lease_lost" if scope_stop else "record_error")


def _execute_pre_cancelled_coverage(tmp_path):
    from booley.flows.sim.campaign.coverage_execution import CoverageAggregateExecutor
    from booley.flows.sim.execution.contract import PreSimScopeStoppedError
    from tests.flows.sim.campaign_test_support import _maximum_hook_selection_plan, _request

    plan, _ = _maximum_hook_selection_plan(("alpha",))
    store = CampaignStore(tmp_path / "coverage-cancel")
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
        campaign_id=str(uuid.uuid4()),
        target_key="sim",
        work_item_key="item",
        attempt_key="attempt",
    )
    alias = expand_run_directory(
        str(literal / ".." / "literal"),
        project_root=tmp_path,
        campaign_id=str(uuid.uuid4()),
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


def test_memory_admitted_campaign_runs_work_items_without_child_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the memory admission switch a heavy cap of 3 still gives one lane (#3701)."""
    from booley.flows import endpoint_admission
    from booley.flows.endpoint_admission import AdmissionGate
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.runtime.job_slots import CLASS_MEMORY

    manifest = _three_item_manifest()
    plan = create_simulation_campaign_plan(manifest)
    project, state = _parallel_environment(tmp_path, "alpha", 0)
    # One item at a time: nothing waits for a peer to overlap.
    state.barrier = threading.Barrier(1)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: state.handle),
    )
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _ParallelExecution(state),  # type: ignore[arg-type,return-value]
    )
    booley_dir = project / ".booley"
    (booley_dir / "booley.toml").write_text(
        '[jobs]\nmax_heavy = 3\nmemory_budget = "1000m"\n', encoding="utf-8"
    )
    monkeypatch.setenv("BOOLEY_DEV_MEMORY_ADMISSION", "1")
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
        assert admission.slot_store.caps.max_heavy == 3
        assert admission.max_heavy == 1
        outcome = SimulationCampaign(executor).run(
            NewCampaignRunRequest(
                plan, project, invocation.parent, CampaignPolicy(), invocation, admission
            )
        )
        (outer,) = admission.slot_store.snapshot(CLASS_MEMORY)[0]
        assert outer.job_class == CLASS_MEMORY
        assert admission.slot_store.snapshot(CLASS_HEAVY) == ([], [])

    assert outcome.complete is True
    # The items ran in series: each ended before the next began.
    runs = sorted(state.intervals, key=lambda interval: interval[1])
    assert [name for name, _start, _end in runs] == ["alpha", "beta", "gamma"]
    assert all(left[2] <= right[1] for left, right in pairwise(runs))
    store = CampaignStore(invocation / "targets/sim/record")
    attempts = [
        json.loads(path.read_bytes())
        for path in store.root.glob("work-items/*/attempts/*/attempt.json")
    ]
    assert len(attempts) == 3
    assert [attempt["child_execution_id"] for attempt in attempts] == [None] * 3
