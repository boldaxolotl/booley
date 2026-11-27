"""Each Simulation Record work item holds its own Memory Reservation (#3772, ADR 0087).

The Flow keeps one reservation for its
shared builds, then drops to zero and each later item takes a claim of its own
(#3773: one lane per core, the claims decide how many run at once).
The Simulation Record, slot store and Flow claim are real and the simulators are fake.
The slot clock is fake too; the lane step machine's scenarios also control process
liveness, waits and thread order, with its actors as the foreign Jobs.
"""

from __future__ import annotations

import contextvars
import functools
import json
import os
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.core.file_lock import acquire_file_lock
from booley.flows import endpoint_admission
from booley.flows.endpoint_admission import AdmissionContext, AdmissionGate
from booley.flows.endpoint_session import _attach_memory_warning
from booley.flows.job_estimates import JobRequest
from booley.flows.memory_admission import (
    MemorySizing,
    job_thread_count,
    measure_memory_claim,
)
from booley.flows.sim import build_session
from booley.flows.sim.build_session import (
    SimulationBuildLeaseStoppedError,
    SimulationBuildLeaseTimeoutError,
    SimulationBuildSession,
    simulation_build_slot,
)
from booley.flows.sim.record import capacity, coordinator
from booley.flows.sim.record.capacity import LeaseLostError, SimulationRecordCapacity
from booley.flows.sim.record.codec import canonical_json_bytes
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    ResumeSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
)
from booley.flows.sim.record.model import (
    SimulationRecordManifest,
    create_simulation_record_plan,
)
from booley.flows.sim.record.planning import finalize_manifest
from booley.flows.sim.record.scheduler import ScheduledAttempt
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.runtime import job_slots, oom_priority
from booley.runtime.endpoint_execution import (
    EndpointOutcome,
    job_attempt,
    job_stopped_after_oom,
    note_job_oom_kill,
)
from booley.runtime.execution_records import execution_paths
from booley.runtime.job_slots import (
    CLASS_MEMORY,
    ROLE_INTERACTIVE,
    ClaimAbortedError,
    ClaimLostError,
    MemoryClaim,
)
from booley.runtime.resource_history import SCHEMA
from booley.runtime.supervised_execution import current_supervised_execution
from tests.flows.sim.simulation_record_test_support import (
    _PARALLEL_SYNC_TIMEOUT_S,
    _failed_shared_outcome,
    _manifest_for,
    _parallel_environment,
    _ParallelExecution,
    _ParallelGroup,
    _scheduled_attempt,
    _validated_simulation_record,
    _wait_until,
)
from tests.flows.test_endpoint_admission import _selection_prepared
from tests.job_slot_steps import (
    DRAIN_STEP_CAP,
    Actor,
    FakeProcessWorld,
    StepEvent,
    Worker,
    stepped_file_lock_wait,
    stepped_turn,
    thread_name,
)
from tests.memory_budget_support import pin_budget
from tests.oom_priority_support import FakeProc
from tests.test_job_slot_lane_step_machine import (
    _PARENT_EXECUTION,
    LaneStepMachine,
    ScheduledRun,
    _no_inherited_execution,  # noqa: F401 -- an autouse fixture of the lane step machine
    _stepped_record_lock,  # noqa: F401 -- an autouse fixture of the lane step machine
    _waits,
    build_machine,  # noqa: F401 -- a fixture
)
from tests.time_budget import hang_guard_s

NAMES = ("alpha", "beta", "gamma")
BUDGET_MIB = 1000.0
# History peaks (MiB); the estimator adds 25 %. The build is the largest phase.
PREPARE_PEAK = 50.0  # FuseSoC setup (#3787): every item runs it, so every item reserves it
BUILD_PEAK = 400.0
RUN_PEAKS = {"alpha": 100.0, "beta": 200.0, "gamma": 160.0}
FLOW_REQUESTS = (JobRequest("sim", "sim", test_runs=NAMES),)
_machine = pytest.mark.skipif(
    sys.platform != "linux", reason="the lane step machine's recovery scans Linux /proc"
)


def _record(target: str, phase: str, peak: float, *, test_run: str = "", flags=()) -> dict:
    return {
        "key": {"target": target, "phase": phase, "flags": list(flags), "test_run": test_run},
        "peak_memory_mb": peak,
        "wall_s": 1.0,
    }


def _history(*targets: str) -> list[dict]:
    """The history of a Simulation Record whose Targets are *targets*."""
    return [
        record
        for target in targets
        for record in (
            _record(target, "sim_prepare", PREPARE_PEAK),
            _record(target, "sim_build", BUILD_PEAK),
            *(_record(target, "sim_run", peak, test_run=name) for name, peak in RUN_PEAKS.items()),
        )
    ]


def _sizer(selector: str, runs: Sequence[object], flags: tuple[str, ...]) -> JobRequest:
    """The Flow's ``work_item_job_request``, without a Target plan to read."""
    return JobRequest("sim", selector, test_runs=tuple(run.name for run in runs), flags=flags)  # type: ignore[attr-defined]


class _ObservedGroup(_ParallelGroup):
    """A fake simulator group that reports to its scenario while an item runs."""

    def planning_disclosure(self):
        return getattr(self.state, "disclosures", {}).get(self.names[0], {})

    def compile(self):
        if getattr(self.state, "kill_build", False):
            # The OOM killer took the compiler of the item that builds: the build fails.
            self.state.kill_build = False
            note_job_oom_kill()
            return SimpleNamespace(passed=False)
        return super().compile()

    def finish_build_failure(self):
        return _failed_shared_outcome()

    def launch_bundle(self, bundle_root: Path, evidence_root: Path, run_cwd: Path):
        with self.state.running():
            killed = self.state.observe(self.names[0])
            outcome = super().launch_bundle(bundle_root, evidence_root, run_cwd)
            if not killed:
                return outcome
            # The OOM killer took the simulator: the adapter reports a crash.
            (test,) = outcome.tests
            crashed = replace(test, verdict="fail", passed=False, crashed=True)
            return replace(outcome, passed=False, verdict="fail", tests=(crashed,))


class _ObservedExecution(_ParallelExecution):
    @contextmanager
    def ordinary_group(self, _handle, names, _test_runs=None):
        yield _ObservedGroup(self.state, names)


@dataclass
class _Scenario:
    """One Flow admission with its slot store, and what each item saw while it ran."""

    tmp_path: Path
    project: Path
    state: SimpleNamespace
    admission: AdmissionContext
    store: job_slots.SlotStore
    seen: dict[str, dict[str, list[object]]] = field(default_factory=dict)
    hooks: dict[str, Callable[[], None]] = field(default_factory=dict)
    # Per item: does its own child execution hold a Memory Reservation while it runs?
    holds_own: dict[str, bool] = field(default_factory=dict)
    # Item name -> how many of its next launches the OOM killer takes; each launch's own
    # claim (``(run_alone, queue_reason)``) and whether it was killed, in launch order.
    kills: dict[str, int] = field(default_factory=dict)
    launches: dict[str, list[tuple[bool, str | None, bool]]] = field(default_factory=dict)
    # Per item: the Job thread count it saw while it ran (``None`` when it has none).
    threads: dict[str, int | None] = field(default_factory=dict)
    # Memory claims held before the Flow was admitted, so the Flow's own count is below the cores.
    early_holders: list[job_slots.SlotToken] = field(default_factory=list)
    # Items running now, the most at once, and the Simulation Record lane threads that ran items.
    active: int = 0
    peak: int = 0
    lanes: set[str] = field(default_factory=set)
    _gate: threading.Lock = field(default_factory=threading.Lock)
    # Step-machine scenarios: each idle item's thread and scope, the items that closed with a
    # cancelled scope, the record's outcomes and each lane execution's own token.
    idle: dict[str, tuple[Worker, Any]] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    outcomes: list[Any] = field(default_factory=list)
    item_tokens: dict[str, job_slots.SlotToken] = field(default_factory=dict)

    @property
    def flow_token(self) -> job_slots.SlotToken:
        assert isinstance(self.admission.outer_token, job_slots.SlotToken)
        return self.admission.outer_token

    def observe(self, name: str) -> bool:
        """Record who holds memory while item *name* runs (the Flow apart from the items).

        Returns whether the OOM killer takes this launch's simulator.
        """
        holders = self.store.observe(CLASS_MEMORY)[0]
        flow = [_held(token) for token in holders if token.lease_id == self.flow_token.lease_id]
        items = [_held(token) for token in holders if token.lease_id != self.flow_token.lease_id]
        self.seen[name] = {"flow": flow, "items": items}
        scope = current_supervised_execution()
        own_id = None if scope is None else scope.execution_id
        self.holds_own[name] = own_id is not None and any(
            token.execution_id == own_id for token in holders
        )
        self.threads[name] = job_thread_count()
        if name in self.hooks:
            self.hooks[name]()
        own = [token for token in holders if token.execution_id == own_id and own_id is not None]
        killed = self.kills.get(name, 0) > 0
        with self._gate:
            self.launches.setdefault(name, []).append(
                (
                    bool(own and own[0].memory is not None and own[0].memory.run_alone),
                    own[0].queue_reason if own else None,
                    killed,
                )
            )
            if killed:
                self.kills[name] -= 1
        if killed:
            note_job_oom_kill()
        return killed

    @contextmanager
    def running(self) -> Iterator[None]:
        """Count one item as running on the calling lane thread."""
        with self._gate:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.lanes.add(threading.current_thread().name)
        try:
            yield
        finally:
            with self._gate:
                self.active -= 1

    def record(self) -> SimulationRecordStore:
        return SimulationRecordStore(self.tmp_path / "reports" / "000001" / "targets/sim/record")

    def request(
        self, manifest: SimulationRecordManifest, number: int = 1
    ) -> NewSimulationRecordRunRequest:
        invocation = self.tmp_path / "reports" / f"{number:06d}"
        invocation.mkdir(parents=True, exist_ok=True)
        return NewSimulationRecordRunRequest(
            create_simulation_record_plan(manifest),
            self.project,
            invocation.parent,
            SimulationRecordPolicy(),
            invocation,
            self.admission,
        )

    def simulation_record(self) -> SimulationRecord:
        executor = OrdinaryHdlSerialExecutor(
            invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
            execution_factory=lambda _options: _ObservedExecution(self.state),  # type: ignore[arg-type,return-value]
        )
        return SimulationRecord(executor, work_item_request=_sizer)


def _own_lanes(scenario: _Scenario) -> list[bool]:
    """Per item in manifest order: did it run as a lane of its own (child execution)?"""
    paths = sorted(scenario.record().root.glob("work-items/*/attempts/*/attempt.json"))
    return [json.loads(path.read_bytes())["child_execution_id"] is not None for path in paths]


def _held(token: job_slots.SlotToken) -> object:
    """What a holder reserves: MiB, or ``"alone"``."""
    assert token.memory is not None
    return "alone" if token.memory.run_alone else token.memory.reservation_mb


@contextmanager
def _scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    flow_requests: Sequence[JobRequest] | None = FLOW_REQUESTS,
    history: Sequence[dict] = (),
    cores: int = 1,
    held_at_admission: int = 0,
    machine: LaneStepMachine | None = None,
) -> Iterator[_Scenario]:
    """The Flow of a Simulation Record, admitted under a 1,000 MiB Memory Budget on *cores* cores.

    *held_at_admission* 50 MiB claims are held (``early_holders``) when the Flow is admitted.
    With a *machine*, its Simulation Record process R is the Flow, over the stepped slots.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    project, state = _parallel_environment(tmp_path)
    state.barrier = threading.Barrier(1)  # one item at a time: nothing waits for a peer
    booley_dir = project / ".booley"
    pin_budget(monkeypatch, 1000)
    # The container's cores, as the CPU budget reads them (affinity bounded by the cgroup quota).
    # Private seam: AdmissionGate.enter calls available_cpu_count with defaults, so no public
    # argument sets the core count.
    monkeypatch.setattr("booley.runtime.cpu_budget._affinity_cpu_count", lambda: cores)
    monkeypatch.setattr("booley.runtime.cpu_budget._cgroup_cpu_count", lambda _read_text: None)
    path = booley_dir / "runtime" / "resources" / "history.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"$schema": SCHEMA, "records": list(history)}), encoding="utf-8")
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(
            select=lambda *_args, **_kwargs: state.handle,
            # A synthesis Job plans its tools from the Target's flow options.
            inspect=lambda _handle: SimpleNamespace(flow_options={}),
        ),
    )
    # Private seam: the fake simulators stand in for the Pre-Sim hook runner, which has no public
    # injection point on the executor.
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution._run_hook", lambda *_args, **_kwargs: None
    )
    # A fake /proc with no process: ranking never touches the worker's real OOM priority.
    monkeypatch.setattr("booley.runtime.oom_priority.PROC", FakeProc())
    monkeypatch.setenv("BOOLEY_RUNTIME_EXECUTION_ID", "a" * 32)
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    monkeypatch.delenv(job_slots.INHERITED_SLOT_ENV, raising=False)
    monkeypatch.setattr(endpoint_admission, "resolve_common_booley_dir", lambda: booley_dir)
    prepared = (
        None
        if flow_requests is None
        else SimpleNamespace(invocation=SimpleNamespace(job_requests=lambda: tuple(flow_requests)))
    )
    if machine is not None:
        assert not held_at_admission
        admission = _machine_admission(machine, monkeypatch, booley_dir, flow_requests)
        scenario = _Scenario(tmp_path, project, state, admission, machine.record.store)
        state.observe, state.running = scenario.observe, scenario.running
        _remember_item_tokens(monkeypatch, scenario)
        yield scenario
        return
    # A lease clock that never moves: no lease expires and no renewal thread starts.
    clocked = functools.partial(job_slots.SlotStore, now=FakeProcessWorld().now)
    monkeypatch.setattr(job_slots, "SlotStore", clocked)  # boundary: the slot stores' clock
    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir))
    early = [
        store.acquire(
            CLASS_MEMORY,
            pid=os.getpid(),
            memory=MemoryClaim(budget_mb=BUDGET_MIB, reservation_mb=50.0),
            poll_interval=0.01,
        )
        for _ in range(held_at_admission)
    ]
    with AdmissionGate(AsicSynthesizeFlow(), prepared).enter() as admission:  # type: ignore[arg-type]
        scenario = _Scenario(tmp_path, project, state, admission, store, early_holders=early)
        state.observe = scenario.observe
        state.running = scenario.running
        yield scenario


def _machine_admission(
    machine: LaneStepMachine,
    monkeypatch: pytest.MonkeyPatch,
    booley_dir: Path,
    flow_requests: Sequence[JobRequest] | None,
) -> AdmissionContext:
    """R admitted as the Flow: ``AdmissionGate`` would name this process's real PID."""
    store = machine.record.store
    assert machine.record.flow_token is not None
    store.release(machine.record.flow_token)
    token = store.acquire(
        CLASS_MEMORY,
        pid=machine.record.pid,
        execution_id=_PARENT_EXECUTION,
        memory=measure_memory_claim(booley_dir, flow_requests).claim,
    )
    # Private: the builder AdmissionGate names a lost Flow slot with.
    cause = endpoint_admission._admission_cancellation_cause(store, token)
    stepped = functools.partial(
        coordinator.BoundedSimulationRecordScheduler,
        clock=machine.world.now,
        event=lambda: StepEvent(machine.scheduler),
        take_turn=stepped_turn(machine.scheduler),
    )
    monkeypatch.setattr(  # boundary: the lane scheduler's clock and waits
        coordinator, "BoundedSimulationRecordScheduler", stepped
    )
    monkeypatch.setattr(  # boundary: the Simulation slot's file-lock wait
        build_session, "wait_for_file_lock", stepped_file_lock_wait
    )
    # I7 knows R's machine lanes only; this record's lanes keep theirs in the project.
    machine.scheduler.checks.remove(machine._check_execution_entries)
    return AdmissionContext(
        "managed",
        store,
        token,
        ROLE_INTERACTIVE,
        _PARENT_EXECUTION,
        None,
        # The unstepped scope watchers must not scan the stepped slots between steps.
        lambda: machine.scheduler.owns_caller() and cause() is not None,
        cause,
        memory_sizing=MemorySizing(booley_dir),
    )


def _remember_item_tokens(monkeypatch: pytest.MonkeyPatch, scenario: _Scenario) -> None:
    """Keep each lane's own token: R runs no renewal thread, so a scenario renews it itself."""
    acquire = scenario.store.acquire

    def remember(*args: Any, **kwargs: Any) -> job_slots.SlotToken:
        token = acquire(*args, **kwargs)
        scenario.item_tokens[str(kwargs.get("execution_id"))] = token
        return token

    monkeypatch.setattr(scenario.store, "acquire", remember)


def _step(machine: LaneStepMachine, until: Callable[[], bool]) -> None:
    """Grant runnable threads before waiting ones, which take turns, until *until*."""
    turns: Counter[Worker] = Counter()
    for _ in range(DRAIN_STEP_CAP):
        if until():
            return
        parked = machine.scheduler.parked()
        if not parked:
            break
        worker = min(parked, key=lambda w: (_waits(w), turns[w], thread_name(w)))
        turns[worker] += _waits(worker)
        machine.scheduler.grant(worker)
    raise AssertionError("the schedule did not get there")


def _step_past_waits(machine: LaneStepMachine) -> None:
    """Step until every waiting thread has tried again at least twice."""
    seen: set[tuple[Worker, int]] = set()

    def tried() -> bool:
        parked = machine.scheduler.parked()
        seen.update((w, w.steps) for w in parked if _waits(w))
        return all(sum(worker is w for worker, _ in seen) > 2 for w in parked)

    _step(machine, tried)


def _start_record(
    machine: LaneStepMachine, scenario: _Scenario, manifests: Sequence[SimulationRecordManifest]
) -> ScheduledRun:
    """R publishes and runs a Simulation Record of each manifest."""
    record = scenario.simulation_record()
    requests = [scenario.request(manifest, number) for number, manifest in enumerate(manifests, 1)]
    run = ScheduledRun(scenario.record())

    def work() -> None:
        try:
            for request in requests:
                record.publish_new(request)
            scenario.outcomes.extend(record.run(request) for request in requests)
        except Exception as exc:  # noqa: BLE001 -- the scenario judges it
            run.error = exc

    run.worker = machine.scheduler.start(machine.record, "record", work)
    return run


def _finish(machine: LaneStepMachine, run: ScheduledRun) -> None:
    assert run.worker is not None
    worker = run.worker
    _step(machine, lambda: worker.state == "done")


def _idle_until_closed(
    machine: LaneStepMachine, scenario: _Scenario, name: str
) -> Callable[[], None]:
    """A hook that holds item *name* until the scenario closes it."""

    def hook() -> None:
        scope = current_supervised_execution()
        assert scope is not None
        scenario.idle[name] = (machine.scheduler.current_worker(), scope)
        machine.scheduler.await_command()
        if scope.cancelled():
            scenario.cancelled.append(name)

    return hook


def _close(machine: LaneStepMachine, scenario: _Scenario, *names: str) -> None:
    for name in names:
        machine.scheduler.command(scenario.idle[name][0], "close")


def _foreign_job(machine: LaneStepMachine, actor: Actor, claim: MemoryClaim) -> None:
    """A Job of another process queues for *claim* and holds it until released."""

    def acquire() -> None:
        actor.store.acquire(
            CLASS_MEMORY,
            pid=actor.pid,
            memory=claim,
            poll_interval=0.01,
            on_submitted=actor.tokens.append,
        )

    machine.scheduler.start(actor, "acquire", acquire)


def _release(machine: LaneStepMachine, actor: Actor) -> None:
    machine.start_operation(actor, "release", ROLE_INTERACTIVE, 0)


def _holds(actor: Actor) -> bool:
    return bool(actor.tokens) and actor.tokens[0].is_holder


def _waiting(scenario: _Scenario) -> int:
    return len(scenario.store.observe(CLASS_MEMORY)[1])


def _alone() -> MemoryClaim:
    return MemoryClaim(budget_mb=BUDGET_MIB, run_alone=True)


@pytest.mark.parametrize(
    ("history", "alpha", "beta", "gamma", "warned"),
    [
        pytest.param(_history("sim"), [125.0], [250.0], [200.0], False, id="shared-build"),
        # Without a measured FuseSoC setup the unknown-phase rule applies to every item (#3787).
        pytest.param(
            [record for record in _history("sim") if record["key"]["phase"] != "sim_prepare"],
            ["alone"],
            ["alone"],
            ["alone"],
            False,
            id="no-sim-prepare-history",
        ),
        pytest.param(
            [*_history("sim"), _record("sim", "sim_run", 900.0, test_run="beta")],
            [125.0],
            ["alone"],
            [200.0],
            True,
            id="over-budget-item",
        ),
    ],
)
def test_items_after_the_shared_build_hold_their_own_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: list[dict],
    alpha: list[object],
    beta: list[object],
    gamma: list[object],
    warned: bool,
) -> None:
    with _scenario(tmp_path, monkeypatch, history=history) as scenario:
        outcome = scenario.simulation_record().run(scenario.request(_manifest_for(NAMES)))
        (still_held,) = scenario.store.observe(CLASS_MEMORY)[0]
        assert _held(still_held) == 0
        assert scenario.store.observe(CLASS_MEMORY)[1] == []
        result = EndpointOutcome(report_text="RESULT: PASS")
        # Private seam: the over-budget warning is composed inside the endpoint session, which
        # this test does not run.
        _attach_memory_warning(result, scenario.admission)

    assert outcome.complete is True
    # The first item builds under the Flow. Once its build is ready it takes over the Flow's
    # reservation as its own run estimate (no build); the others hold theirs, or run alone
    # when the history cannot size them.
    assert scenario.seen["alpha"] == {"flow": [0], "items": alpha}
    assert scenario.seen["beta"] == {"flow": [0], "items": beta}
    assert scenario.seen["gamma"] == {"flow": [0], "items": gamma}
    # Every item ran as a lane of its own: the first one prepared its lane before its build.
    assert _own_lanes(scenario) == [True, True, True]
    warning = "Memory estimate 1125 MiB exceeds the Memory Budget of 1000 MiB; this Job ran alone."
    if warned:
        assert result.detail["memory_admission_warning"] == warning
        assert result.report_text.splitlines() == ["RESULT: PASS", warning]
    else:
        assert "memory_admission_warning" not in result.detail


def test_a_job_queued_behind_an_item_names_its_run_flow_target_and_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holders list names the item's run, Flow, own Target and test, not a pid (#4487).

    The Job names two Targets, the Simulation Record one: the item shows its own. The Flow
    holds nothing by then, so it is not listed.
    """
    monkeypatch.setenv("BOOLEY_RUN_ID", "run-4487")
    requests = (*FLOW_REQUESTS, JobRequest("sim", "other", test_runs=NAMES))
    said: list[str] = []
    with _scenario(
        tmp_path, monkeypatch, history=_history("sim", "other"), flow_requests=requests
    ) as scenario:

        def queue_behind_beta() -> None:
            waiter = scenario.store.submit(
                CLASS_MEMORY,
                pid=os.getpid(),
                memory=MemoryClaim(budget_mb=BUDGET_MIB, reservation_mb=900.0),
            )
            try:
                wait = scenario.store.memory_wait(os.getpid())
                assert wait is not None
                said.append(wait.describe())
            finally:
                scenario.store.release(waiter)

        scenario.hooks["beta"] = queue_behind_beta
        outcome = scenario.simulation_record().run(scenario.request(_manifest_for(NAMES)))

    assert outcome.complete is True
    assert said == [
        "needs 900 MiB; 750 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-4487 (synth, sim, beta) 250 MiB"
    ]


def test_resumed_simulation_record_items_take_their_own_claims_after_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume has no Flow estimate (run-alone); its frozen manifest says what it traces."""
    history = [
        *_history("sim"),
        *(
            _record("sim", "sim_run", peak, test_run=name, flags=("trace",))
            for name, peak in {"alpha": 300.0, "beta": 400.0, "gamma": 360.0}.items()
        ),
    ]
    document = json.loads(canonical_json_bytes(_manifest_for(NAMES).document))
    document.pop("fingerprints")
    document["workload"]["trace"] = True
    manifest = finalize_manifest(document)
    with _scenario(tmp_path, monkeypatch, flow_requests=None, history=history) as scenario:
        store = scenario.record()
        store.publish_manifest(manifest)
        validated = _validated_simulation_record(
            store, manifest, scenario.project, scenario.state.handle
        )
        invocation = scenario.tmp_path / "reports" / "000001"
        outcome = scenario.simulation_record().run(
            ResumeSimulationRecordRunRequest(
                validated,
                create_simulation_record_plan(manifest),
                scenario.project,
                invocation.parent,
                SimulationRecordPolicy(),
                invocation,
                scenario.admission,
            )
        )

    assert outcome.complete is True
    # The Flow was run-alone until the build settled; the traced run keys are larger.
    assert scenario.seen == {
        "alpha": {"flow": [0], "items": [375.0]},
        "beta": {"flow": [0], "items": [500.0]},
        "gamma": {"flow": [0], "items": [450.0]},
    }


def _disclosures() -> dict[str, dict]:
    return {
        name: {
            "planner": f"fake-{name}",
            "scratch_inputs": [],
            "generated_files": [],
            "tool_provenance": {"kind": "fake", "version": "1", "contract_version": "1"},
            "cleanup": {"removed": True},
        }
        for name in NAMES
    }


def _private_manifest() -> SimulationRecordManifest:
    """Three items that each build privately (the legacy Pre-Sim build access)."""
    document = json.loads(
        canonical_json_bytes(_manifest_for(NAMES, access="legacy-per-test").document)
    )
    document.pop("fingerprints")
    document["planning_disclosures"] = list(_disclosures().values())
    return finalize_manifest(document)


@_machine
@pytest.mark.parametrize("case", ["alone", "sized", "private", "several"])
def test_stepped_record_completes_beside_a_queued_foreign_job(
    build_machine: Callable[[], LaneStepMachine],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """A foreign run-alone Job queued ahead of the items runs mid-run; the items wait for it."""
    options: dict = {"alone": {"flow_requests": None, "history": ()}}.get(
        case, {"history": _history("sim")}
    )
    manifests = [_private_manifest() if case == "private" else _manifest_for(NAMES)]
    if case == "several":  # a second Simulation Record, in its own store, with a build of its own
        manifests.append(manifests[0])
    machine = build_machine()
    actor = machine.actors[0]
    with _scenario(tmp_path, monkeypatch, machine=machine, **options) as scenario:
        scenario.state.disclosures = _disclosures()
        _foreign_job(machine, actor, _alone())
        _step(machine, lambda: _waiting(scenario) == 1)
        run = _start_record(machine, scenario, manifests)
        _step(machine, lambda: _holds(actor) and _waiting(scenario) == 1)
        started_after = list(scenario.state.completion_order)
        _release(machine, actor)
        _finish(machine, run)
        assert scenario.store.observe(CLASS_MEMORY)[1] == []

    assert run.error is None
    assert [outcome.complete for outcome in scenario.outcomes] == [True] * len(manifests)
    ran = scenario.state.completion_order
    assert len(ran) == 3 * len(manifests)
    assert len(started_after) < len(ran), case
    if case == "private":
        # Each item builds inside itself, so its claim covers the build too.
        assert [seen["items"] for seen in scenario.seen.values()] == [[500.0]] * 3


def _coverage_manifest() -> SimulationRecordManifest:
    """One coverage aggregate item: it builds inside itself and owes no shared build."""
    from tests.flows.sim.simulation_record_test_support import _maximum_hook_selection_manifest

    return _maximum_hook_selection_manifest(("alpha",))[0]


def _flow_claim(scenario: _Scenario):
    """A ``FlowClaim`` over the Flow's admission."""
    from booley.flows.sim.record.flow_claim import FlowClaim

    assert scenario.admission.memory_sizing is not None
    return FlowClaim(scenario.admission, scenario.admission.memory_sizing, _sizer)


@_machine
@pytest.mark.parametrize("case", ["coverage-record", "private-build", "shared-build"])
def test_stepped_front_run_item_starts_before_a_later_job(
    build_machine: Callable[[], LaneStepMachine],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """A Job submitted after the Record does not start before the Record's first building item (#4482).

    With a shared build the first item takes its own claim when the build settles: it keeps
    the Flow's queue place, so the Job does not start before the item runs (#4846).
    """
    manifest = {
        "coverage-record": _coverage_manifest,
        "private-build": _private_manifest,
        "shared-build": lambda: _manifest_for(NAMES),
    }[case]()
    record_store = SimulationRecordStore(tmp_path / "record-store")
    record_store.publish_manifest(manifest)
    machine = build_machine()
    later = machine.actors[0]
    started_before_item: list[bool] = []
    holders: list[object] = []
    with _scenario(
        tmp_path / "scenario", monkeypatch, flow_requests=None, machine=machine
    ) as scenario:
        flow_claim = _flow_claim(scenario)
        flow_claim.expect(record_store.root, manifest, manifest.document["work_items"])
        lanes = SimulationRecordCapacity(
            scenario.admission,
            manifest,
            scenario.project / ".booley",
            lambda item: flow_claim.item_claim(record_store.root, manifest, item),
        )
        # No wall-clock estimate: the later Job never jumps ahead of a waiting item.
        _foreign_job(machine, later, MemoryClaim(budget_mb=BUDGET_MIB, reservation_mb=100.0))
        _step(machine, lambda: _waiting(scenario) == 1)

        def execute(attempt: ScheduledAttempt, _child_id: str | None) -> None:
            if attempt.build_settled is not None:
                # What the coordinator does when the shared build is ready.
                flow_claim.settle(record_store.root, attempt.item)
                attempt.build_settled()
            if not started_before_item:
                started_before_item.append(_holds(later))
                holders.extend(_held(token) for token in scenario.store.observe(CLASS_MEMORY)[0])

        pool = coordinator.BoundedSimulationRecordScheduler(
            lanes,
            allocate=lambda item: _scheduled_attempt(record_store, item),
            execute=execute,
        )
        run = ScheduledRun(record_store)
        run.worker = machine.scheduler.start(
            machine.record, "schedule", lambda: pool.run(manifest.document["work_items"][:1])
        )
        _step(machine, lambda: _holds(later))
        _release(machine, later)
        _finish(machine, run)

    assert started_before_item == [False], "the later Job started before the Record's first item"
    # The item ran alone: the Flow reserves nothing and no other claim holds memory.
    assert sorted(map(str, holders)) == ["0", "alone"]


def test_flow_claim_waits_only_for_the_running_simulation_records_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest_for(NAMES)
    items = manifest.document["work_items"]
    other = tmp_path / "other-store"
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:
        claim = _flow_claim(scenario)
        # Two Simulation Records published, each owing its shared build.
        claim.expect(tmp_path, manifest, items)
        claim.expect(other, manifest, items)
        assert not claim.release(tmp_path)
        claim.settle(tmp_path, items[0])
        # The later Simulation Record's build does not keep the Flow's reservation.
        assert claim.release(tmp_path)
        assert _held(scenario.store.observe(CLASS_MEMORY)[0][0]) == 0
        assert claim.release(other)  # idempotent, once dropped
        # Its building item reserves the build nobody settled (the Flow is zero now).
        building = claim.item_claim(other, manifest, items[0])
        assert building is not None and building.reservation_mb == 500.0
        # A drop the store refuses (the Flow's lease is gone) leaves items under the Flow.
        scenario.store.release(scenario.flow_token)
        assert not _flow_claim(scenario).release(tmp_path)


def _with_pre_sim_commands(manifest: SimulationRecordManifest, item: dict) -> tuple[object, dict]:
    document = json.loads(canonical_json_bytes(manifest.document))
    document["workload"]["source_recipe"]["pre_sim_commands"] = ["make prepare"]
    return SimpleNamespace(document=document), item


def _as_coverage_aggregate(manifest: SimulationRecordManifest, item: dict) -> tuple[object, dict]:
    return manifest, {**item, "kind": "coverage_aggregate"}


def _under_coverage_and_trace(
    manifest: SimulationRecordManifest, item: dict
) -> tuple[object, dict]:
    traced = json.loads(canonical_json_bytes(manifest.document))
    traced.pop("fingerprints")
    traced["workload"].update(coverage=True, trace=True)
    return finalize_manifest(traced), item


@pytest.mark.parametrize(
    ("variant", "reservation", "run_alone"),
    [
        pytest.param(_as_coverage_aggregate, 500.0, False, id="coverage-aggregate-builds"),
        # No history under the coverage and trace flags: the item runs alone.
        pytest.param(_under_coverage_and_trace, None, True, id="no-history-runs-alone"),
    ],
)
def test_item_reserves_its_build_when_the_shared_build_does_not_cover_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: Callable[[SimulationRecordManifest, dict], tuple[object, dict]],
    reservation: float | None,
    run_alone: bool,
) -> None:
    manifest = _manifest_for(NAMES)
    (item, *_) = manifest.document["work_items"]
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:
        claim = _flow_claim(scenario)
        claim.settle(tmp_path, item)  # the shared build is settled, but this variant builds again
        plain = claim.item_claim(tmp_path, manifest, item)
        varied_manifest, varied_item = variant(manifest, item)
        varied = claim.item_claim(tmp_path, varied_manifest, varied_item)  # type: ignore[arg-type]

    assert plain is not None and plain.reservation_mb == 125.0
    assert varied is not None and varied.run_alone is run_alone
    if reservation is not None:
        assert varied.reservation_mb == reservation


@pytest.mark.parametrize(
    ("pre_sim_peak", "reservation"),
    [
        # Unmeasured Pre-Sim Commands reserve the Target's build estimate.
        pytest.param(None, 500.0, id="unmeasured"),
        pytest.param(240.0, 300.0, id="pre-sim-larger"),
        pytest.param(80.0, 125.0, id="run-larger"),
    ],
)
def test_item_with_pre_sim_commands_reserves_their_estimate_not_the_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pre_sim_peak: float | None,
    reservation: float,
) -> None:
    sized: list[tuple[JobRequest, bool]] = []
    original = MemorySizing.plan_work_item

    def spy(self: MemorySizing, request: JobRequest | None, *, build: bool) -> MemoryClaim:
        assert request is not None
        sized.append((request, build))
        return original(self, request, build=build)

    monkeypatch.setattr(MemorySizing, "plan_work_item", spy)
    manifest = _manifest_for(NAMES)
    (item, *_) = manifest.document["work_items"]
    history = _history("sim")
    if pre_sim_peak is not None:
        history.append(_record("sim", "sim_pre_run", pre_sim_peak))
    with _scenario(tmp_path, monkeypatch, history=history) as scenario:
        claim = _flow_claim(scenario)
        claim.settle(tmp_path, item)  # the shared build is settled
        with_hook_manifest, with_hook_item = _with_pre_sim_commands(manifest, item)
        claimed = claim.item_claim(tmp_path, with_hook_manifest, with_hook_item)  # type: ignore[arg-type]

    assert claimed is not None and not claimed.run_alone
    assert claimed.reservation_mb == reservation
    # The settled build is not the item's; its Pre-Sim Commands are.
    ((request, build),) = sized
    assert request.pre_sim_commands and not build


# --- One lane per core, items admitted by their own claims (#3773) ---

CORES = 4


def _names(count: int) -> tuple[str, ...]:
    return tuple(f"t{number:02d}" for number in range(count))


def _sized_history(names: Sequence[str], run_peak: float) -> list[dict]:
    """A history where every run of *names* peaks at *run_peak* MiB (the estimate adds 25 %)."""
    return [
        _record("sim", "sim_prepare", PREPARE_PEAK),
        _record("sim", "sim_build", BUILD_PEAK),
        *(_record("sim", "sim_run", run_peak, test_run=name) for name in names),
    ]


def _flow_requests(names: Sequence[str]) -> tuple[JobRequest, ...]:
    return (JobRequest("sim", "sim", test_runs=tuple(names)),)


def _sized(names: Sequence[str], run_peak: float, cores: int) -> dict[str, Any]:
    """The ``_scenario`` options of a Simulation Record of *names*, each run peaking at *run_peak*."""
    return {
        "flow_requests": _flow_requests(names),
        "history": _sized_history(names, run_peak),
        "cores": cores,
    }


def _meet(barrier: threading.Barrier) -> Callable[[], None]:
    """A hook that waits until the other parties of *barrier* run beside it."""

    def hook() -> None:
        barrier.wait()

    return hook


def _barrier(parties: int) -> threading.Barrier:
    return threading.Barrier(parties, timeout=_PARALLEL_SYNC_TIMEOUT_S)


def _simulation_record_lanes(scenario: _Scenario) -> set[str]:
    return {name for name in scenario.lanes if name.startswith("booley-record-")}


@_machine
@pytest.mark.parametrize(
    ("count", "run_peak", "parties"),
    [
        pytest.param(12, 100.0, CORES, id="twelve-items-one-lane-per-core"),
        pytest.param(6, 320.0, 2, id="budget-admits-two"),  # 400 MiB per item: two fit
        pytest.param(6, 100.0, CORES, id="four-items-each-hold-their-own-claim"),
        pytest.param(3, 100.0, 3, id="three-items-start-together"),
    ],
)
def test_stepped_items_run_in_parallel_each_on_their_own_reservation(
    build_machine: Callable[[], LaneStepMachine],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    run_peak: float,
    parties: int,
) -> None:
    names = _names(count)
    machine = build_machine()
    sized = _sized(names, run_peak, CORES)
    with _scenario(tmp_path, monkeypatch, machine=machine, **sized) as scenario:
        # The first *parties* items each hold a claim of their own and stay, so exactly that
        # many fit at once: the first takes its own when its build settles, then the rest follow.
        for name in names[:parties]:
            scenario.hooks[name] = _idle_until_closed(machine, scenario, name)
        run = _start_record(machine, scenario, [_manifest_for(names)])
        _step(machine, lambda: len(scenario.idle) == parties)
        _step_past_waits(machine)  # a further item gets its chance to start
        assert scenario.peak == parties
        _close(machine, scenario, *names[:parties])
        _finish(machine, run)
        assert scenario.peak == parties  # no later item exceeded it either
        (still_held,) = scenario.store.observe(CLASS_MEMORY)[0]
        assert _held(still_held) == 0
        assert scenario.store.observe(CLASS_MEMORY)[1] == []

    assert run.error is None and scenario.outcomes[0].complete is True
    assert sorted(scenario.state.completion_order) == list(names)
    assert len(_simulation_record_lanes(scenario)) <= CORES
    for name in names:
        assert scenario.seen[name]["flow"] == [0], name
        assert scenario.holds_own[name], name
    assert _own_lanes(scenario) == [True] * count
    # Items ran side by side, each on its own claim.
    assert max(len(seen["items"]) for seen in scenario.seen.values()) == parties


def _inject_item_fault(
    machine: LaneStepMachine, scenario: _Scenario, lost: str, fault: str
) -> list[tuple[Worker, Any]]:
    """Set up item *lost*'s *fault*; returns its thread and scope once it waits for the slot."""
    at_slot: list[tuple[Worker, Any]] = []

    def wait_for_the_slot() -> None:
        at_slot.append((machine.scheduler.current_worker(), current_supervised_execution()))
        _wait_for_busy_slot(scenario.state.handle)

    def time_out() -> None:
        _idle_until_closed(machine, scenario, lost)()
        # This item never reaches the peers' rendezvous; release the peers' wait.
        scenario.state.first_finished.set()
        raise SimulationBuildLeaseTimeoutError("Simulation slot busy for 1s (#4843)")

    if fault == "lease-lost-while-waiting":
        scenario.hooks[lost] = wait_for_the_slot
    elif fault == "lease-timeout":
        scenario.hooks[lost] = time_out
    return at_slot


def _lose_item_lease(
    machine: LaneStepMachine, scenario: _Scenario, run: ScheduledRun, scope: Any
) -> None:
    """Reap the item's entry, then let its renewal and the supervisor find the lease lost."""
    token = scenario.item_tokens[str(scope.execution_id)]
    token.path.unlink()
    # One tick of R's renewal thread, which the machine does not run.
    tick = machine.scheduler.start(machine.record, "tick", lambda: scenario.store.renew(token))
    machine.scheduler.finish(tick)
    machine.supervise(run)
    _step(machine, scope.cancelled)


@_machine
@pytest.mark.parametrize("siblings", [False, True], ids=["one-lane", "other-lanes-run"])
@pytest.mark.parametrize("fault", ["lost-lease", "lease-timeout", "lease-lost-while-waiting"])
def test_stepped_lost_item_lease_stops_only_that_item(
    build_machine: Callable[[], LaneStepMachine],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    siblings: bool,
    fault: str,
) -> None:
    names = _names(6) if siblings else NAMES
    options = _sized(names, 100.0, CORES) if siblings else {"history": _history("sim")}
    lost = "t02" if siblings else "beta"
    expected = SimulationBuildLeaseTimeoutError if fault == "lease-timeout" else LeaseLostError
    held = ["t01", lost] if siblings else [lost]  # a sibling lane runs while this item fails
    machine = build_machine()
    with _scenario(tmp_path, monkeypatch, machine=machine, **options) as scenario:
        for name in held:
            scenario.hooks[name] = _idle_until_closed(machine, scenario, name)
        at_slot = _inject_item_fault(machine, scenario, lost, fault)
        run = _start_record(machine, scenario, [_manifest_for(names)])
        assert run.worker is not None
        worker = run.worker

        def lost_item_is_there() -> bool:
            there = lost in scenario.idle or any(
                w.pending == ("lock_wait", "") for w, _ in at_slot
            )
            siblings_idle = not siblings or "t01" in scenario.idle
            return there and siblings_idle and worker.pending == ("lock_wait", "event")

        _step(machine, lost_item_is_there)
        if fault != "lease-timeout":
            _worker, scope = at_slot[0] if at_slot else scenario.idle[lost]
            _lose_item_lease(machine, scenario, run, scope)
        if lost in scenario.idle:
            _close(machine, scenario, lost)
        if siblings:
            _step_past_waits(machine)  # the failure takes effect while the sibling still runs
            _close(machine, scenario, "t01")
        _finish(machine, run)
        assert scenario.admission.cancellation_cause() is None

    assert isinstance(run.error, expected), run.error
    assert scenario.cancelled == ([lost] if fault == "lost-lease" else [])
    # Every other item ran and finished; the failed one published no result. A lease wait
    # that timed out raised before its simulator ran, so it never completes.
    finished = [name for name in names if name != lost] if fault == "lease-timeout" else names
    assert sorted(scenario.state.completion_order) == sorted(finished)
    assert len(list(scenario.record().root.glob("work-items/*/result.json"))) == len(names) - 1
    if fault == "lease-timeout":
        # The item is left interrupted, so a later resume runs it.
        manifest = scenario.record().load_manifest()
        (interrupted,) = scenario.record().scan(manifest).interrupted
        (item,) = (i for i in manifest.document["work_items"] if i["work_item_id"] == interrupted)
        assert list(item["selection"]["names"]) == [lost]


def _wait_for_busy_slot(handle: Any) -> None:
    """Wait for the Target slot lease while another handle holds it, until cancelled."""
    slot = simulation_build_slot(handle)
    slot.mkdir(parents=True, exist_ok=True)
    with (slot / "lease.lock").open("a+", encoding="utf-8") as holder:
        acquire_file_lock(holder)
        # The wait ends at the cancellation, well before the guard.
        with pytest.raises(SimulationBuildLeaseStoppedError):
            SimulationBuildSession(handle, wait_s=hang_guard_s()).__enter__()


@_machine
@pytest.mark.parametrize("parallel", [False, True], ids=["one-lane", "running-lanes"])
def test_stepped_lost_flow_lease_cancels_lanes_and_withdraws_the_queued_claim(
    build_machine: Callable[[], LaneStepMachine],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parallel: bool,
) -> None:
    names = _names(8) if parallel else NAMES
    # 400 MiB per item: two run, the third waits (one lane: a foreign Job holds the machine).
    options = _sized(names, 320.0, CORES) if parallel else {"history": _history("sim")}
    running = names[1:3] if parallel else ()  # items that run until the Flow's lease goes
    machine = build_machine()
    actor = machine.actors[0]
    with _scenario(tmp_path, monkeypatch, machine=machine, **options) as scenario:
        for name in running:
            scenario.hooks[name] = _idle_until_closed(machine, scenario, name)
        if not parallel:
            _foreign_job(machine, actor, _alone())
            _step(machine, lambda: _waiting(scenario) == 1)
        run = _start_record(machine, scenario, [_manifest_for(names)])
        assert run.worker is not None
        # The running items run (or the foreign Job starts once the Flow dropped) and the
        # next item's claim waits: now the Flow's lease goes.
        _step(
            machine,
            lambda: (
                set(running) <= set(scenario.idle)
                and (parallel or _holds(actor))
                and _waiting(scenario) == 1
            ),
        )
        _step_past_waits(machine)  # the claim's own thread now polls for admission
        _step(machine, lambda: run.worker.pending == ("lock_wait", "event"))
        scenario.store.release(scenario.flow_token)
        machine.supervise(run)  # its cancel_waiters pass, before any lane runs again
        assert _waiting(scenario) == 0
        for name in running:
            execution_id = scenario.idle[name][1].execution_id
            booley_dir = scenario.project / ".booley"
            assert execution_paths(execution_id, booley_dir=booley_dir).cancel.exists(), name
        _close(machine, scenario, *running)
        if not parallel:
            _release(machine, actor)
        _finish(machine, run)
        cause = scenario.admission.cancellation_cause()
        assert cause is not None and scenario.flow_token.path.name in cause
        assert scenario.store.observe(CLASS_MEMORY)[1] == []

    # Any lane may report the loss: a waiting claim, or a cancelled running item.
    errors = (ClaimAbortedError, ClaimLostError, capacity.SimulationRecordCapacityError)
    assert isinstance(run.error, errors), run.error
    assert sorted(scenario.cancelled) == list(running)
    started = names[:3] if parallel else names[:1]
    assert set(scenario.state.completion_order) <= set(started)
    assert names[0] in scenario.state.completion_order


@_machine
def test_stepped_record_queues_one_item_claim_at_a_time(
    build_machine: Callable[[], LaneStepMachine],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = _names(8)
    machine = build_machine()
    alone, other = machine.actors[:2]
    sized = _sized(names, 100.0, CORES)
    with _scenario(tmp_path, monkeypatch, machine=machine, **sized) as scenario:
        _foreign_job(machine, alone, _alone())
        _step(machine, lambda: _waiting(scenario) == 1)
        run = _start_record(machine, scenario, [_manifest_for(names)])
        # The foreign Job runs once the first item finished (its claim holds the Flow's queue
        # place ahead of the run-alone Job). Exactly one item claim queues behind it: the
        # turn passes on only when that claim is admitted, however many lanes wait.
        _step(machine, lambda: _holds(alone) and _waiting(scenario) >= 1)
        _step_past_waits(machine)  # a second queued item claim would show up here
        (waiter,) = scenario.store.observe(CLASS_MEMORY)[1]
        assert waiter.pid == machine.record.pid
        # The other Job's claim queues beside the item claim.
        _foreign_job(machine, other, MemoryClaim(budget_mb=BUDGET_MIB, reservation_mb=100.0))
        _step(machine, lambda: _waiting(scenario) == 2)
        assert not _holds(other)
        _release(machine, alone)
        _finish(machine, run)

    assert run.error is None and scenario.outcomes[0].complete is True
    assert sorted(scenario.state.completion_order) == list(names)
    # Admitted lanes start items concurrently, so the order they record is a thread race: the
    # turn's order is pinned by the one queued item claim above, not by this list.
    assert sorted(scenario.seen) == list(names)


def _item_attempts(scenario: _Scenario) -> dict[str, tuple[int, dict]]:
    """Per item name: how many attempt directories it has, and its one published result."""
    found: dict[str, tuple[int, dict]] = {}
    for directory in sorted(scenario.record().root.glob("work-items/*")):
        results = list(directory.glob("result.json"))
        assert len(results) == 1, f"{directory.name} has {len(results)} results"
        result = json.loads(results[0].read_bytes())
        found[result["observations"][0]["test"]] = (
            len(list(directory.glob("attempts/*/attempt.json"))),
            result,
        )
    return found


def _kill_item_run_beside_another(scenario: _Scenario, name: str, others: str, times: int) -> dict:
    """Make the OOM killer take item *name*'s next *times* launches while *others* runs beside it.

    Returns what *others* saw: whether its commands were told to stop after the kill.
    """
    scenario.kills[name] = times
    together = _barrier(2)
    killed_noted = threading.Event()
    seen: dict[str, bool] = {}

    def killed_item() -> None:
        if scenario.kills[name] == times:  # first launch only: the peer is there once
            together.wait()

    def peer() -> None:
        together.wait()
        assert killed_noted.wait(_PARALLEL_SYNC_TIMEOUT_S)
        seen["peer_stopped"] = job_stopped_after_oom()

    scenario.hooks[name] = killed_item
    scenario.hooks[others] = peer
    original = scenario.observe

    def observe(launched: str) -> bool:
        killed = original(launched)
        if killed:
            killed_noted.set()
        return killed

    scenario.state.observe = observe
    return seen


@pytest.mark.parametrize("times", [1, 2], ids=["requeued-once", "killed-again-while-alone"])
@pytest.mark.parametrize(
    ("killed", "third"), [("gamma", "alpha"), ("alpha", "gamma")], ids=["later-item", "first-item"]
)
def test_oom_killed_item_is_requeued_once_alone_and_never_a_third_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, times: int, killed: str, third: str
) -> None:
    """The first item is killed after its build settled, so it is requeued like the others."""
    with _scenario(tmp_path, monkeypatch, history=_history("sim"), cores=2) as scenario:
        seen = _kill_item_run_beside_another(scenario, killed, "beta", times)
        with job_attempt(stop_on_oom=True) as flow_attempt:
            outcome = scenario.simulation_record().run(scenario.request(_manifest_for(NAMES)))
        assert scenario.store.observe(CLASS_MEMORY)[1] == []

    assert outcome.complete is True
    attempts = _item_attempts(scenario)
    # Two attempts, one result: the second one, never a third.
    count, result = attempts[killed]
    assert count == 2, "the killed item was not run again"
    assert result["attempt_ordinal"] == 2
    first, second = scenario.launches[killed]
    assert first[2] and first[:2] == (False, None)
    assert second[:2] == (True, "requeued after out-of-memory")  # alone, with its reason
    if times == 1:
        assert not second[2] and result["grade"] == "pass"
        # The item beside it was not stopped, and published its own single result.
        assert seen == {"peer_stopped": False}
        assert attempts["beta"][0] == 1 and len(scenario.launches["beta"]) == 1
        assert attempts[third][0] == 1
    else:
        assert second[2] and result["state"] == "crash" and result["grade"] != "pass"
    # Only an item killed again while alone is named, by its tests, on the Flow's attempt.
    assert flow_attempt.final_item_kills == ([killed] if times == 2 else [])
    # The Flow's own attempt saw no kill: it is not requeued.
    assert not flow_attempt.oom_killed.is_set() and not flow_attempt.stopped.is_set()


def _item_killed_then_execution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, times: int
) -> list[str]:
    """Kill item gamma's first *times* launches and fail its executor after the last; return the names."""
    with _scenario(tmp_path, monkeypatch, history=_history("sim"), cores=2) as scenario:
        _kill_item_run_beside_another(scenario, "gamma", "beta", times)
        record = scenario.simulation_record()
        assert record._executor is not None
        execute = record._executor.execute

        # The executor's evidence I/O is not reachable from a public entry point on demand.
        def execute_then_fail(request):
            before = len(scenario.launches.get("gamma", []))
            result = execute(request)
            launches = len(scenario.launches.get("gamma", []))
            if launches > before and launches == times and scenario.kills["gamma"] == 0:
                raise OSError("evidence write failed")
            return result

        monkeypatch.setattr(record._executor, "execute", execute_then_fail)
        with (
            job_attempt(stop_on_oom=True) as flow_attempt,
            pytest.raises(OSError, match="evidence write failed"),
        ):
            record.run(scenario.request(_manifest_for(NAMES)))
    return flow_attempt.final_item_kills


def test_item_killed_again_whose_execution_raises_is_still_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second kill is named even when the executor then fails (for example on evidence I/O)."""
    assert _item_killed_then_execution_fails(tmp_path, monkeypatch, 2) == ["gamma"]


def test_item_killed_once_whose_execution_raises_is_named_as_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first attempt that dies and then fails is not requeued, so its kill is named too."""
    assert _item_killed_then_execution_fails(tmp_path, monkeypatch, 1) == ["gamma"]


def test_oom_kill_under_the_flow_claim_keeps_the_flow_requeue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first item's build runs under the Flow's claim: a kill of it is the Flow's."""
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:
        scenario.state.kill_build = True  # the OOM killer takes alpha's build
        with job_attempt(stop_on_oom=True) as flow_attempt:
            scenario.simulation_record().run(scenario.request(_manifest_for(NAMES)))

    attempts = _item_attempts(scenario)
    # Every item published its blocked result once: no item was requeued on its own.
    assert {name: count for name, (count, _result) in attempts.items()} == dict.fromkeys(NAMES, 1)
    assert flow_attempt.oom_killed.is_set()
    assert scenario.launches == {}  # nothing launched after the failed build


@pytest.mark.parametrize("pre_sim", [False, True], ids=["no-pre-sim", "pre-sim"])
def test_only_items_holding_their_own_claim_are_registered_for_oom_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pre_sim: bool
) -> None:
    """An own-claim item's commands rank as its own Job; the Flow-claim item's do not."""
    registered: dict[str, int] = {}

    def record(name: str) -> Callable[[], None]:
        def hook() -> None:
            holders, _waiters = scenario.store.observe(CLASS_MEMORY)
            # Private seam: the scheduler's OOM registration has no public view.
            ranked = oom_priority._item_roots({token.lease_id for token in holders})
            registered[name] = len(ranked)

        return hook

    manifest = _manifest_for(NAMES)
    if pre_sim:
        # The build-settled signal does not depend on Pre-Sim Commands, which run after it.
        document = json.loads(canonical_json_bytes(manifest.document))
        document.pop("fingerprints")
        document["workload"]["source_recipe"]["pre_sim_commands"] = ["make prepare"]
        manifest = finalize_manifest(document)
    with _scenario(tmp_path, monkeypatch, history=_history("sim")) as scenario:
        for name in NAMES:
            scenario.hooks[name] = record(name)
        scenario.simulation_record().run(scenario.request(manifest))

    # The first item took its own claim when its build settled: each item holds one.
    assert registered == {"alpha": 1, "beta": 1, "gamma": 1}


# --- Each item's own thread count (#3788) ---

EIGHT_CORES = 8


def test_two_items_building_in_parallel_each_get_half_the_cores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = _names(3)
    # 400 MiB per item: two fit the 1,000 MiB budget.
    with _scenario(tmp_path, monkeypatch, **_sized(names, 320.0, EIGHT_CORES)) as scenario:

        def first_waits_for_a_sibling() -> None:
            # t00 takes its own claim alone when its build settles; t01 is then admitted beside
            # it, and t02 (which would not fit beside both) only after t00 returns its claim.
            # t00 holds on until t01 has fixed its thread count: that count is read a moment
            # after t01's claim is admitted, and a t00 that returned sooner would leave t01
            # the whole machine.
            _wait_until(lambda: "t01" in scenario.threads, _PARALLEL_SYNC_TIMEOUT_S)

        scenario.hooks["t00"] = first_waits_for_a_sibling
        together = _barrier(2)
        for name in ("t01", "t02"):
            scenario.hooks[name] = _meet(together)
        outcome = scenario.simulation_record().run(scenario.request(_manifest_for(names)))

    assert outcome.complete is True
    # Admitted alone, t00 has every core; t01 and t02 each share the cores with one sibling.
    assert scenario.threads["t00"] == EIGHT_CORES
    assert scenario.threads["t01"] == EIGHT_CORES // 2
    assert scenario.threads["t02"] == EIGHT_CORES // 2


def test_another_job_counts_the_flow_token_and_the_item_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = _names(2)
    history = [*_sized_history(names, 100.0), _record("a", "synth", 40.0)]
    with _scenario(
        tmp_path,
        monkeypatch,
        flow_requests=_flow_requests(names),
        history=history,
        cores=EIGHT_CORES,
    ) as scenario:
        seen: dict[str, int | None] = {}

        def admit_another_job() -> None:
            def run() -> None:
                flow = AsicSynthesizeFlow()
                with AdmissionGate(flow, _selection_prepared(flow, tmp_path / "other")).enter():
                    seen["count"] = job_thread_count()

            # A plain thread with a fresh context, as another process's Job would have.
            thread = threading.Thread(target=contextvars.Context().run, args=(run,))
            thread.start()
            thread.join(_PARALLEL_SYNC_TIMEOUT_S)
            assert not thread.is_alive()

        scenario.hooks["t01"] = admit_another_job
        outcome = scenario.simulation_record().run(scenario.request(_manifest_for(names)))

    assert outcome.complete is True
    assert scenario.holds_own["t01"]
    # The Flow's zero token, the running item's claim and the Job itself: three holders.
    assert seen["count"] == EIGHT_CORES // 3


def test_item_thread_count_stays_fixed_while_holders_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = _names(2)
    with _scenario(
        tmp_path, monkeypatch, **_sized(names, 100.0, EIGHT_CORES), held_at_admission=1
    ) as scenario:
        counts: list[int | None] = []
        counted, released = threading.Event(), threading.Event()

        def release_the_early_holder_once_t01_counted() -> None:
            assert counted.wait(_PARALLEL_SYNC_TIMEOUT_S)
            for token in scenario.early_holders:
                scenario.store.release(token)
            released.set()

        def watch_the_count() -> None:
            counted.set()
            assert released.wait(_PARALLEL_SYNC_TIMEOUT_S)
            counts.append(job_thread_count())
            foreign = scenario.store.acquire(
                CLASS_MEMORY,
                pid=os.getpid(),
                memory=MemoryClaim(budget_mb=BUDGET_MIB, reservation_mb=50.0),
                poll_interval=0.01,
            )
            counts.append(job_thread_count())
            scenario.store.release(foreign)
            counts.append(job_thread_count())

        scenario.hooks["t00"] = release_the_early_holder_once_t01_counted
        scenario.hooks["t01"] = watch_the_count
        outcome = scenario.simulation_record().run(scenario.request(_manifest_for(names)))

    assert outcome.complete is True
    # The Flow's zero claim is never counted. t00 counts the early holder and its own claim; t01,
    # admitted before t00 lets the holder go, counts both and its own, and keeps that count.
    assert scenario.threads["t00"] == EIGHT_CORES // 2
    assert scenario.holds_own["t01"]
    assert [scenario.threads["t01"], *counts] == [EIGHT_CORES // 3] * 4


def test_failed_item_thread_count_releases_the_item_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = _names(2)

    def unreadable(*_args: object, **_kwargs: object) -> int:
        raise OSError("slot directory unreadable")

    with _scenario(tmp_path, monkeypatch, **_sized(names, 100.0, CORES)) as scenario:
        monkeypatch.setattr(capacity, "job_threads_at_start", unreadable)
        with pytest.raises(OSError, match="unreadable"):
            scenario.simulation_record().run(scenario.request(_manifest_for(names)))
        holders, waiters = scenario.store.observe(CLASS_MEMORY)

    # Only the Flow's own claim remains: the item's claim was released with its lane.
    assert [token.lease_id for token in holders] == [scenario.flow_token.lease_id]
    assert waiters == []
