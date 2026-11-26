"""One Flow-neutral admission path for every endpoint.

The same admission outcomes (withdrawn queued run, invalid runtime
identity, no-slot cases, release at run end) must hold for Simulation and for a
non-Simulation Flow, and must not depend on the endpoint's name.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows import endpoint_admission
from booley.flows.endpoint_admission import (
    AdmissionContext,
    AdmissionGate,
    AdmissionIdentityError,
)
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.endpoint_state import EndpointState
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EndpointOutcome,
    EndpointRejectedError,
    ExecutionResult,
)
from booley.runtime.execution_records import RUNTIME_EXECUTION_ENV, ExecutionId
from tests.memory_budget_support import fail_budget
from tests.oom_priority_support import (
    PROC_PATH,
    FakeProc,
    inert_oom_proc,  # noqa: F401  (fixture, used by memory_runtime)
    oom_priority,
)

FLOW_TYPES = {"sim": SimulateFlow, "synth": AsicSynthesizeFlow, "fpga": FpgaImplFlow}


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A session runtime with no ambient identity, slot or state variables."""
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.chdir(tmp_path)
    for name in (
        "BOOLEY_TICKET_FILE",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_RUNTIME_DIR",
        RUNTIME_EXECUTION_ENV,
        job_slots.INHERITED_SLOT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@dataclass
class _Harness:
    """One real Flow wired so its invocation body and admission are observable."""

    flow: SimulateFlow | AsicSynthesizeFlow | FpgaImplFlow
    request: Any
    invocations: list[object] = field(default_factory=list[object])
    admissions: list[AdmissionContext | None] = field(
        default_factory=list[AdmissionContext | None]
    )
    behavior: Callable[[], EndpointOutcome] = EndpointOutcome
    # The admission each invocation body received from its prepared invocation.
    run_admissions: list[object | None] = field(default_factory=list[object | None])
    # The prepared simulation each Simulation Record body received.
    run_prepared: list[object] = field(default_factory=list[object])
    # Prepares the Flow's real invocation kind; runs once, when the test first executes.
    prepare: Callable[[], None] = lambda: None

    def execute(self) -> ExecutionResult:
        self.prepare()
        self.prepare = lambda: None
        return self.flow.execute(self.request)


@pytest.fixture(params=("sim", "synth"))
def harness(
    request: pytest.FixtureRequest, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> _Harness:
    """Simulation and a non-Simulation Flow, reduced to their invocation body.

    Both run their real prepared-invocation kind: the Simulation Record (which
    borrows admission) and Target selection (which does not).
    """
    return _build_harness(request.param, runtime, monkeypatch)


def _build_harness(kind: str, runtime: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """The harness of Flow *kind* (``sim`` or ``synth``), patched through *monkeypatch*."""
    flow = FLOW_TYPES[kind]()
    typed_request = flow.request_type(
        target="demo", work_dir=runtime, report_dir=runtime / "reports"
    )
    observed = _Harness(flow=flow, request=typed_request)

    def run() -> EndpointOutcome:
        observed.invocations.append(object())
        return observed.behavior()

    if kind == "sim":

        def run_prepared_simulation(
            _prepared: object, admission: object | None
        ) -> EndpointOutcome:
            observed.run_admissions.append(admission)
            observed.run_prepared.append(_prepared)
            return run()

        monkeypatch.setattr(flow, "run_prepared_simulation", run_prepared_simulation)

        def prepare() -> None:
            from booley.flows.sim.flow import PreparedSimulationEndpoint
            from booley.flows.sim.record_invocation import SimulationRecordInvocation
            from tests.flows.invocation_doubles import prepare_as

            invocation = SimulationRecordInvocation(
                flow, PreparedSimulationEndpoint((), None, (), {})
            )
            prepare_as(monkeypatch, flow, invocation)
    else:

        def prepare() -> None:
            from booley.flows.prepared_invocation import TargetSelectionInvocation
            from tests.flows.invocation_doubles import prepare_as

            prepare_as(monkeypatch, flow, TargetSelectionInvocation(flow, None, run))

    observed.prepare = prepare

    original_admission = EndpointState.admission

    def recording_admission(
        self: EndpointState, prepared: PreparedExecution, *, requeued: bool = False
    ) -> AbstractContextManager[AdmissionContext | None]:
        @contextmanager
        def record() -> Iterator[AdmissionContext | None]:
            # Only the requeued admission names the keyword, as only it is new.
            options = {"requeued": True} if requeued else {}
            with original_admission(self, prepared, **options) as borrowed:
                observed.admissions.append(borrowed)
                yield borrowed

        return record()

    monkeypatch.setattr(EndpointState, "admission", recording_admission)
    return observed


def _refuse_slot(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    """Make every claim on the slot store fail with *failure* at the store's public ``acquire``."""

    def refuse(_store: job_slots.SlotStore, _job_class: str, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(job_slots.SlotStore, "acquire", refuse)


def _forbid_slot_store(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    """Fail the test if admission opens the slot store."""
    monkeypatch.setattr(job_slots, "SlotStore", lambda *_args, **_kwargs: pytest.fail(reason))


@pytest.mark.usefixtures("memory_runtime")
def test_withdrawn_queued_run_reports_cancelled(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_slot(monkeypatch, job_slots.ClaimLostError("vanished"))

    result = harness.execute()

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.exit_code == EXIT_ERROR
    assert result.outcome.report_text == (
        f"CANCELLED: this queued '{harness.flow.name}' run was "
        "withdrawn (booley_cancel) before it started."
    )
    assert harness.invocations == []


@pytest.mark.usefixtures("memory_runtime")
def test_invalid_runtime_identity_names_endpoint(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUNTIME_EXECUTION_ENV, "not-an-execution-id")
    _forbid_slot_store(monkeypatch, "slot acquisition must not run")

    result = harness.execute()

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith(
        f"{harness.flow.name}: invalid {RUNTIME_EXECUTION_ENV}: "
    )
    assert harness.invocations == []


def test_disagreeing_token_identity_names_endpoint(
    harness: _Harness, memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed token linked to another execution is rejected and still released."""

    class LinkingStore(job_slots.SlotStore):
        def __init__(self, root: Path, **kwargs: Any) -> None:
            # The store links the new entry to the execution this verifier vouches for.
            super().__init__(
                root, inherited_execution_verifier=lambda *_args: ExecutionId("a" * 32), **kwargs
            )

    monkeypatch.setattr(job_slots, "SlotStore", LinkingStore)
    monkeypatch.setenv(RUNTIME_EXECUTION_ENV, "b" * 32)

    result = harness.execute()

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith(
        f"{harness.flow.name}: invalid {RUNTIME_EXECUTION_ENV}: "
    )
    assert harness.invocations == []
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


def _fail() -> EndpointOutcome:
    raise RuntimeError("endpoint failed")


def _cancel() -> EndpointOutcome:
    raise KeyboardInterrupt("cancelled in process")


@pytest.mark.parametrize(
    ("ending", "behavior"),
    (
        pytest.param("success", EndpointOutcome, id="success"),
        pytest.param("failure", _fail, id="failure"),
        pytest.param("cancelled", _cancel, id="cancelled"),
    ),
)
def test_slot_released_when_run_ends(
    harness: _Harness,
    memory_runtime: Path,
    ending: str,
    behavior: Callable[[], EndpointOutcome],
) -> None:
    harness.behavior = behavior
    stdout_before = sys.stdout

    if ending == "cancelled":
        with pytest.raises(KeyboardInterrupt):
            harness.execute()
    else:
        result = harness.execute()
        assert result.exit_code == (0 if ending == "success" else EXIT_ERROR)

    assert len(harness.invocations) == 1
    (borrowed,) = harness.admissions
    assert isinstance(borrowed, AdmissionContext)
    assert borrowed.mode == "managed"
    assert isinstance(borrowed.outer_token, job_slots.SlotToken)
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []
    assert sys.stdout is stdout_before


def _prepared() -> PreparedExecution:
    """A prepared execution of an ordinary run."""
    from booley.flows.prepared_invocation import OrdinaryRunInvocation

    return _prepared_for(OrdinaryRunInvocation(EndpointOutcome))


@pytest.mark.usefixtures("memory_runtime")
def test_dry_run_invocation_runs_without_admission(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-persisting dry run takes no slot and writes no numbered report."""
    _forbid_slot_store(monkeypatch, "a dry run must not take a slot")
    harness.request = replace(harness.request, dry_run=True)

    result = harness.execute()

    assert result.exit_code == 0
    assert len(harness.invocations) == 1
    assert harness.admissions == [None]
    # Only the Simulation Record borrows admission, and it saw none.
    if isinstance(harness.flow, SimulateFlow):
        assert harness.run_admissions == [None]
    assert not (harness.request.report_dir / harness.flow.name).exists()


class _StubEndpoint:
    """The least an admitted endpoint needs; admission runs the real memory claim."""

    endpoint_kind = "flow"
    _pre_run_head: str | None = None

    def __init__(self, name: str, job_class: str | None) -> None:
        self.name = name
        self._job_class = job_class

    def _resolve_job_class(self) -> str | None:
        return self._job_class

    def _memory_admitted(self) -> bool:
        # The production policy, so the Specialist guard exercises it rather than a copy.
        return EndpointState._memory_admitted(self)  # type: ignore[arg-type]

    def _memory_admission_plan(self, project: Path | None, requests: Any) -> Any:
        return EndpointState._memory_admission_plan(self, project, requests)  # type: ignore[arg-type]

    def _get_head_sha(self) -> str | None:
        return None


@pytest.mark.usefixtures("memory_runtime")
@pytest.mark.parametrize("case", ("unclassed", "inherited", "no_runtime"))
def test_no_slot_cases_admit_unmanaged(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = _StubEndpoint("custom", None if case == "unclassed" else job_slots.CLASS_MEMORY)
    if case == "inherited":
        monkeypatch.setenv(job_slots.INHERITED_SLOT_ENV, "memory/slot")
    if case == "no_runtime":
        monkeypatch.setattr(job_slots, "slots_dir", lambda _project=None: None)
    _forbid_slot_store(monkeypatch, "no slot store may be opened")

    with endpoint_admission.admission(endpoint, _prepared()) as borrowed:  # type: ignore[arg-type]
        assert isinstance(borrowed, AdmissionContext)
        assert borrowed.mode == "unmanaged"
        assert borrowed.slot_store is None
        assert borrowed.outer_token is None


@pytest.mark.parametrize(
    "failure",
    (
        job_slots.ClaimLostError("vanished"),
        AdmissionIdentityError("mismatch"),
    ),
    ids=("claim_lost", "identity"),
)
def test_body_failures_are_not_remapped_to_admission_rejections(
    failure: Exception, memory_runtime: Path, runtime: Path
) -> None:
    """Only entering admission is mapped; the invocation body's errors pass through."""
    flow = AsicSynthesizeFlow()

    with (
        pytest.raises(type(failure)),
        endpoint_admission.admission(flow, _selection_prepared(flow, runtime)),
    ):
        raise failure
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


@pytest.mark.usefixtures("memory_runtime")
def test_invocation_exception_is_normalized(harness: _Harness) -> None:
    """An unexpected invocation failure becomes the stable exit-2 report, and cleans up."""
    from booley.runtime.exception_diagnostics import exception_report_text

    harness.behavior = _fail
    stdout_before = sys.stdout

    result = harness.execute()

    assert result.exit_code == EXIT_ERROR
    expected = exception_report_text(harness.flow.name, RuntimeError("endpoint failed"))
    # The persisted diagnostic path trails the stable "<name> failed: <Type>: <message>" text.
    assert result.outcome.report_text.startswith(expected)
    assert sys.stdout is stdout_before


def test_publication_resources_outlive_final_report(
    harness: _Harness, memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invocation resources and the slot are held until the final report is written."""
    flow = harness.flow
    events: list[str] = []
    at_final_report: list[tuple[list[str], int]] = []

    def behavior() -> EndpointOutcome:
        flow.publication_resources.callback(events.append, "resources_released")
        if isinstance(flow, SimulateFlow):
            # The numbered report directory the final report writes into.
            assert flow.reserve_invocation_dir() is not None
        return EndpointOutcome()

    original_write_report = type(flow).write_report

    def spy_write_report(self: object, result: EndpointOutcome) -> object:
        held = len(_held(memory_runtime, job_slots.CLASS_MEMORY))
        at_final_report.append((list(events), held))
        return original_write_report(self, result)  # type: ignore[arg-type]

    monkeypatch.setattr(type(flow), "write_report", spy_write_report)
    harness.behavior = behavior

    assert harness.execute().exit_code == 0

    assert at_final_report
    assert at_final_report[-1] == ([], 1)
    assert events == ["resources_released"]
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


# --- memory admission (#3701) ---------------------


def _seed_records(booley_dir: Path, *peaks: tuple[str, str, float]) -> None:
    """Write a Resource History whose records say each (Target, phase) peak (MiB)."""
    from booley.runtime.resource_history import SCHEMA

    records = [
        {
            "key": {"target": target, "phase": phase, "flags": [], "test_run": ""},
            "peak_memory_mb": peak,
            "wall_s": 1.0,
        }
        for target, phase, peak in peaks
    ]
    path = booley_dir / "runtime" / "resources" / "history.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"$schema": SCHEMA, "records": records}), encoding="utf-8")


def _seed_history(booley_dir: Path, *peaks: tuple[str, float]) -> None:
    """Write a Resource History whose records say each Target's synth peak (MiB)."""
    _seed_records(booley_dir, *((target, "synth", peak) for target, peak in peaks))


@pytest.fixture
def memory_runtime(
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    inert_oom_proc: FakeProc,  # noqa: F811  (keeps the worker's own OOM priority out of reach)
    pin_memory_budget: Callable[[int], None],
) -> Path:
    """The runtime with a real slots root and a pinned 1,000 MiB Memory Budget."""
    booley_dir = runtime / ".booley"
    booley_dir.mkdir()
    pin_memory_budget(1000)
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    monkeypatch.setattr(endpoint_admission, "resolve_common_booley_dir", lambda: booley_dir)
    return booley_dir


def _target_selection(
    flow: Any, work_dir: Path, *names: str, drivable_by: str = "synth"
) -> PreparedExecution:
    """The preparation of *flow* as a Target-selection invocation over the Targets *names*."""
    from booley.flows.prepared_invocation import TargetSelectionInvocation
    from tests.target_test_support import make_target_handle

    handles = tuple(make_target_handle(work_dir, n, drivable_by=(drivable_by,)) for n in names)
    # Private seam: Target selection reads the Flow's parsed request, and preparing it through
    # execute needs a resolved FuseSoC core.
    flow._args = SimpleNamespace(
        work_dir=work_dir, target=",".join(names), baseline=None, frontend=None
    )
    selection = SimpleNamespace(handles=handles, matches=lambda *_args: True)
    return _prepared_for(TargetSelectionInvocation(flow, selection, flow._run))  # type: ignore[arg-type]


def _selection_prepared(flow: AsicSynthesizeFlow, work_dir: Path) -> PreparedExecution:
    """The preparation of *flow* as a Target-selection invocation over its one Target ``a``."""
    return _target_selection(flow, work_dir, "a")


def _prepared_for(invocation: Any) -> PreparedExecution:
    return PreparedExecution(
        non_persisting_dry_run=False,
        invocation=invocation,
    )


@contextmanager
def _holding(
    booley_dir: Path, *claims: job_slots.MemoryClaim, job: job_slots.JobLabel | None = None
) -> Iterator[list[job_slots.SlotToken]]:
    """Hold each claim in the memory Job Class for this live process, as another Job would."""
    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir))
    tokens = [
        store.acquire(job_slots.CLASS_MEMORY, pid=os.getpid(), memory=claim, job=job)
        for claim in claims
    ]
    try:
        yield tokens
    finally:
        for token in tokens:
            store.release(token)


class _QueuedError(Exception):
    """Raised from the store's wait to stop a claim once it has narrated that it is queued."""


@pytest.fixture
def queued_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop an admission that must wait: it narrates once, then its first sleep raises."""

    def stop(_seconds: float) -> None:
        raise _QueuedError

    class StoppingStore(job_slots.SlotStore):
        def __init__(self, root: Path, **kwargs: Any) -> None:
            super().__init__(root, sleep=stop, **kwargs)

    monkeypatch.setattr(job_slots, "SlotStore", StoppingStore)


def _held(booley_dir: Path, job_class: str) -> list[job_slots.SlotToken]:
    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir))
    return store.observe(job_class)[0]


def test_memory_admission_claims_the_estimated_reservation(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_history(memory_runtime, ("a", 400))
    monkeypatch.setenv("BOOLEY_RUN_ID", "run-7")
    flow = AsicSynthesizeFlow()

    with AdmissionGate(flow, _selection_prepared(flow, runtime)).enter() as context:
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.job == job_slots.JobLabel("run-7", "synth", "a")
        # One phase with its own history: 1.0 s x the estimator's 1.25 headroom.
        assert holder.memory == job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=500.0, wall_s=1.25
        )
        assert context.mode == "managed"
        assert context.memory_warning is None

    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


def test_memory_admitted_job_ranks_its_processes(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_history(memory_runtime, ("a", 400))
    proc = FakeProc()
    coordinator = proc.add(os.getpid(), value=0)
    tool = proc.add(coordinator + 1_000_000, parent=coordinator)
    monkeypatch.setattr(PROC_PATH, proc)
    flow = AsicSynthesizeFlow()

    with AdmissionGate(flow, _selection_prepared(flow, runtime)).enter():
        # The only running Job is the newest: its tool is first to go, the
        # coordinator that reports the kill stays just below it.
        assert proc.values[tool] == 1000
        assert proc.values[coordinator] == 1000 - oom_priority.COORDINATOR_GAP

    assert proc.values[coordinator] == 0


@pytest.mark.parametrize("case", ("unknown_invocation", "inherited"))
def test_memory_admission_without_a_known_job_or_under_a_parent(
    case: str, memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_history(memory_runtime, ("a", 400))
    if case == "inherited":
        monkeypatch.setenv(job_slots.INHERITED_SLOT_ENV, "memory/entry")

    with AdmissionGate(AsicSynthesizeFlow()).enter() as context:
        holders = _held(memory_runtime, job_slots.CLASS_MEMORY)
        if case == "inherited":
            assert (context.mode, holders) == ("unmanaged", [])
        else:
            # No invocation says what the Job runs: it takes the whole budget.
            assert [token.memory for token in holders] == [
                job_slots.MemoryClaim(budget_mb=1000.0, run_alone=True)
            ]


def test_memory_admission_survives_a_budget_it_cannot_compute(
    memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable MemTotal serializes the Job; it never refuses."""
    fail_budget(monkeypatch)

    with AdmissionGate(AsicSynthesizeFlow()).enter() as context:
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory == job_slots.MemoryClaim(budget_mb=0.0, run_alone=True)
        assert context.memory_warning is not None
        assert context.memory_warning.startswith("Memory Budget unavailable (")


def test_over_budget_job_result_carries_the_memory_warning(
    harness: _Harness, memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.job_estimates import JobRequest
    from booley.flows.prepared_invocation import TargetSelectionInvocation
    from booley.flows.sim.record_invocation import SimulationRecordInvocation

    # A 2,000 MiB peak is a 2,500 MiB estimate against the 1,000 MiB budget.
    _seed_history(memory_runtime, ("a", 2000))
    request = (JobRequest("synth", "a"),)
    warning = "Memory estimate 2500 MiB exceeds the Memory Budget of 1000 MiB; this Job ran alone."
    monkeypatch.setattr(TargetSelectionInvocation, "job_requests", lambda _self: request)
    monkeypatch.setattr(SimulationRecordInvocation, "job_requests", lambda _self: request)
    harness.behavior = lambda: EndpointOutcome(report_text="RESULT: PASS")

    result = harness.execute()

    assert result.exit_code == 0
    assert result.outcome.detail["memory_admission_warning"] == warning
    assert result.outcome.report_text.splitlines()[:2] == ["RESULT: PASS", warning]
    (borrowed,) = harness.admissions
    assert borrowed is not None and borrowed.memory_warning == warning


def test_unwarned_job_result_is_unchanged(harness: _Harness, memory_runtime: Path) -> None:
    held: list[int] = []

    def behavior() -> EndpointOutcome:
        held.append(len(_held(memory_runtime, job_slots.CLASS_MEMORY)))
        return EndpointOutcome(report_text="RESULT: PASS")

    harness.behavior = behavior

    result = harness.execute()

    assert held == [1]
    assert "memory_admission_warning" not in result.outcome.detail
    assert result.outcome.report_text.splitlines()[0] == "RESULT: PASS"
    assert "ran alone" not in result.outcome.report_text


def test_a_memory_admitted_job_lends_work_item_sizing(
    memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sizing a Simulation Record needs for its work items' own claims comes with the Job's claim (#3772)."""
    with AdmissionGate(AsicSynthesizeFlow()).enter() as context:
        assert context.memory_sizing is not None
        assert context.memory_sizing.booley_dir == memory_runtime
        assert context.memory_sizing.warnings == []
    # A Job under its parent's claim takes none of its own, so it has nothing to lend.
    monkeypatch.setenv(job_slots.INHERITED_SLOT_ENV, "memory/entry")
    with AdmissionGate(AsicSynthesizeFlow()).enter() as context:
        assert context.memory_sizing is None


def test_mutation_coordinator_holds_a_zero_companion_claim(memory_runtime: Path) -> None:
    """The coordinator is memory-class with a zero companion claim; its items still take claims (#3772)."""
    from booley.flows.mutation.flow import MutationFlow

    with AdmissionGate(MutationFlow()).enter() as context:
        assert context.mode == "managed"
        assert isinstance(context.outer_token, job_slots.SlotToken)
        assert context.outer_token.job_class == job_slots.CLASS_MEMORY
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory == job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=0.0, companion=True
        )
        assert context.memory_sizing is not None
        assert context.memory_sizing.booley_dir == memory_runtime
        assert context.memory_sizing.warnings == []


def test_a_companion_coordinator_is_never_requeued_after_oom(memory_runtime: Path) -> None:
    """A coordinator that came back run-alone would never run beside Specialists."""
    from booley.flows.mutation.flow import MutationFlow

    flow = MutationFlow()
    with AdmissionGate(flow).enter() as context:
        assert not flow.may_requeue_after_oom(_prepared(), context)


def test_specialist_holds_a_companion_memory_reservation(memory_runtime: Path) -> None:
    from booley.runtime.resource_history import SCHEMA
    from booley.specialists.reviewer import ReviewerSpecialist

    def record(peak: float, specialist: str) -> dict[str, Any]:
        return {
            "key": {
                "target": "",
                "phase": f"specialist:{specialist}",
                "flags": [],
                "test_run": "",
                "specialist": specialist,
            },
            "peak_memory_mb": peak,
            "wall_s": 1.0,
        }

    path = memory_runtime / "runtime" / "resources" / "history.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [record(300, "reviewer"), record(346, "reviewer"), record(900, "other")]
    path.write_text(json.dumps({"$schema": SCHEMA, "records": records}), encoding="utf-8")

    with AdmissionGate(ReviewerSpecialist()).enter() as context:
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        # 346 MiB x the estimator's 1.25 margin; half the 1,000 MiB budget is not reached.
        assert holder.memory == job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=432.5, companion=True
        )
        assert context.mode == "managed"
        assert context.memory_warning is None
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


def test_memory_claim_of_a_job_without_requests_names_the_selected_target(
    memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.prepared_invocation import OrdinaryRunInvocation

    monkeypatch.setenv("BOOLEY_RUN_ID", "run-8")
    flow, _prepared = _custom_flow("echo")
    # An invocation that lists no requests (its memory estimate is unknown) still has --target.
    prepared = _prepared_for(OrdinaryRunInvocation(flow._run))

    with AdmissionGate(flow, prepared).enter():
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.job == job_slots.JobLabel("run-8", "echo_flow", "echo")


@pytest.mark.parametrize(
    ("value", "deadline"),
    (("soon", None), ("30", 60.0), (None, None)),
    ids=("bad", "set", "unset"),
)
def test_holder_deadline_follows_the_slot_timeout(
    memory_runtime: Path,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    deadline: float | None,
) -> None:
    """The reaper's deadline is twice the exported budget, and an unparseable one is ignored."""
    if value is None:
        monkeypatch.delenv("BOOLEY_SLOT_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_SLOT_TIMEOUT_S", value)
    flow = AsicSynthesizeFlow()

    with AdmissionGate(flow, _selection_prepared(flow, runtime)).enter():
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.timeout_s == deadline


# --- lint and Custom Flow Jobs (#3703) ------------------------------


def _lint_prepared(work_dir: Path) -> tuple[Any, PreparedExecution]:
    """A lint Job over Targets ``a`` and ``b``, prepared as a Target-selection invocation."""
    from booley.flows.lint.flow import LintFlow

    flow = LintFlow()
    return flow, _target_selection(flow, work_dir, "a", "b", drivable_by="lint")


def _custom_flow(target: str) -> tuple[Any, PreparedExecution]:
    """A Custom Flow given *target*, prepared through the real ``prepare_endpoint_invocation``."""
    import argparse

    from booley.flows.base import BooleyFlow

    class EchoFlow(BooleyFlow):
        name = "echo_flow"
        description = "Custom Flow for admission tests"

        def _add_args(self, parser: argparse.ArgumentParser) -> None:
            """This Flow takes no options of its own."""

        def _run(self) -> EndpointOutcome:
            return EndpointOutcome()

    flow = EchoFlow()
    # Private seam: the Flow's parsed request names the Target; preparing it through execute
    # needs a resolved FuseSoC core.
    flow._args = SimpleNamespace(target=target)  # type: ignore[assignment]
    invocation = flow.prepare_endpoint_invocation()
    assert not isinstance(invocation, EndpointOutcome)
    return flow, _prepared_for(invocation)


def _admitted_job(kind: str, work_dir: Path, booley_dir: Path) -> tuple[Any, PreparedExecution]:
    """A lint or Custom Flow Job whose history says it needs a known reservation."""
    if kind == "lint":
        _seed_records(booley_dir, ("a", "lint", 400), ("b", "lint", 600))
        return _lint_prepared(work_dir)
    _seed_records(booley_dir, ("echo", "echo_flow", 400))
    return _custom_flow("echo")


def test_lint_job_reserves_its_largest_target_estimate(
    memory_runtime: Path, runtime: Path
) -> None:
    flow, prepared = _admitted_job("lint", runtime, memory_runtime)

    with AdmissionGate(flow, prepared).enter() as context:
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        # The 600 MiB peak of Target b, with the estimator's headroom.
        # The lint covers two Targets: 2 x 1.25 s of own wall-clock.
        assert holder.memory == job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=750.0, wall_s=2.5
        )
        assert context.mode == "managed"

    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


def test_custom_flow_job_reserves_its_flow_phase(memory_runtime: Path) -> None:
    # With and without a Target: the history key's Target is the one given, else empty.
    for target in ("echo", ""):
        # The larger record under another Target is what a Job keyed by the wrong Target
        # would be sized from (875, not 500).
        _seed_records(memory_runtime, ("other", "echo_flow", 700), (target, "echo_flow", 400))
        flow, prepared = _custom_flow(target)

        with AdmissionGate(flow, prepared).enter():
            (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
            expected = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=500.0, wall_s=1.25)
            assert holder.memory == expected, target

        assert _held(memory_runtime, job_slots.CLASS_MEMORY) == [], target


@pytest.mark.parametrize(("kind", "need_mb"), (("lint", 750), ("custom", 500)))
def test_lint_and_custom_flow_jobs_queue_behind_a_run_alone_job(
    kind: str,
    need_mb: int,
    memory_runtime: Path,
    runtime: Path,
    queued_once: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A *kind* Job waits (QUEUED, position 1) while a run-alone Job holds the budget."""
    flow, prepared = _admitted_job(kind, runtime, memory_runtime)
    holder = job_slots.JobLabel("run-1", "synth", "t1")

    with _holding(
        memory_runtime, job_slots.MemoryClaim(budget_mb=1000.0, run_alone=True), job=holder
    ) as (first,):
        with pytest.raises(_QueuedError), AdmissionGate(flow, prepared).enter():
            pytest.fail("the Job entered while a run-alone Job held the budget")

        # Queued behind the first holder, then withdrawn when the wait ended.
        assert [token.seq for token in _held(memory_runtime, job_slots.CLASS_MEMORY)] == [
            first.seq
        ]
    assert capsys.readouterr().err == (
        f"[admission] {flow.name}: queued (position 1): needs {need_mb} MiB; "
        "0 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-1 (synth, t1) whole budget\n"
    )


# --- Seat Caps for licensed EDA tools (#3244) -----------------------


def _fpga_prepared(work_dir: Path) -> tuple[Any, PreparedExecution]:
    """An FPGA Job over Target ``a`` (its Vivado Job needs one seat), as Target selection."""
    flow = FpgaImplFlow()
    return flow, _target_selection(flow, work_dir, "a", drivable_by="fpga")


def _write_jobs(booley_dir: Path, seats: str | None, *, fpga_enabled: bool = True) -> None:
    """Write ``[jobs]``, with *seats* as ``[jobs.seats]`` if given, and the FPGA Flow's switch."""
    seats_table = "" if seats is None else f"\n[jobs.seats]\n{seats}\n"
    flows_table = "" if fpga_enabled else "\n[flows.fpga]\nenabled = false\n"
    (booley_dir / "booley.toml").write_text(
        f"[jobs]\n{seats_table}{flows_table}", encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("kind", "refuse", "fix"),
    (
        pytest.param("fpga", lambda _patch: None, "add [jobs.seats] vivado", id="seat_cap"),
        pytest.param(
            "synth",
            lambda patch: _refuse_slot(patch, job_slots.ClaimLostError("vanished")),
            "withdrawn (booley_cancel)",
            id="claim_lost",
        ),
        pytest.param(
            "synth",
            lambda patch: patch.setenv(RUNTIME_EXECUTION_ENV, "not-an-execution-id"),
            f"invalid {RUNTIME_EXECUTION_ENV}",
            id="invalid_runtime_env",
        ),
    ),
)
def test_cli_prints_an_admission_refusal_once(
    kind: str,
    refuse: Callable[[pytest.MonkeyPatch], None],
    fix: str,
    memory_runtime: Path,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _build_harness(kind, runtime, monkeypatch)
    _write_jobs(memory_runtime, None)  # [jobs] without [jobs.seats]: Vivado has no Seat Cap
    refuse(monkeypatch)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime / "runtime"))  # no live progress region
    harness.prepare()

    result = harness.flow.execute_cli(["--target", "demo"])

    assert result.exit_code == EXIT_ERROR
    assert fix in result.outcome.report_text
    assert capsys.readouterr().err == f"{result.outcome.report_text}\n"
    # A built-in Flow's CLI configures no logging, so logging's last-resort handler
    # would print any WARNING record on stderr too; under pytest it lands in caplog.
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_fpga_job_queues_for_its_vivado_seat_while_memory_fits(
    memory_runtime: Path,
    runtime: Path,
    queued_once: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_jobs(memory_runtime, "vivado = 1")
    _seed_records(memory_runtime, ("a", "fpga_impl", 400))
    flow, prepared = _fpga_prepared(runtime)
    seat = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=100.0, seats=(("vivado", 1),))

    with _holding(memory_runtime, seat) as (first,):
        # 100 + 500 MiB fit the 1,000 MiB budget: only the seat holds the Job back.
        with pytest.raises(_QueuedError), AdmissionGate(flow, prepared).enter():
            pytest.fail("the Job entered while the only Vivado seat was held")

        assert [token.seq for token in _held(memory_runtime, job_slots.CLASS_MEMORY)] == [
            first.seq
        ]
    assert capsys.readouterr().err == (
        "[admission] fpga: queued (position 1): "
        "waits for a vivado seat (1 of 1 held); "
        "needs 500 MiB; 900 MiB free of the 1,000 MiB Memory Budget; "
        f"largest holders: pid {os.getpid()} 100 MiB\n"
    )


def test_fpga_job_runs_beside_another_vivado_job_under_a_cap_of_two(
    memory_runtime: Path, runtime: Path
) -> None:
    _write_jobs(memory_runtime, "vivado = 2")
    _seed_records(memory_runtime, ("a", "fpga_impl", 400))
    flow, prepared = _fpga_prepared(runtime)
    seat = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=100.0, seats=(("vivado", 2),))

    with (
        _holding(memory_runtime, seat) as (first,),
        AdmissionGate(flow, prepared).enter(),
    ):
        holders = _held(memory_runtime, job_slots.CLASS_MEMORY)
        (mine,) = (token for token in holders if token.seq != first.seq)
        assert mine.memory == job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=500.0, wall_s=1.25, seats=(("vivado", 2),)
        )
        assert len(holders) == 2


@pytest.mark.parametrize(
    ("seats", "refusal"),
    (
        pytest.param(None, "[jobs.seats] vivado = <seats you own>", id="missing"),
        pytest.param("vivado = 0", "[jobs.seats].vivado", id="non_positive"),
    ),
)
def test_fpga_job_without_a_usable_vivado_seat_cap_is_refused_before_admission(
    seats: str | None, refusal: str, memory_runtime: Path, runtime: Path
) -> None:
    _write_jobs(memory_runtime, seats)
    flow, prepared = _fpga_prepared(runtime)

    with (
        pytest.raises(EndpointRejectedError) as refused,
        endpoint_admission.admission(flow, prepared),
    ):
        pytest.fail("the Job was admitted without a usable Vivado seat cap")

    assert refused.value.outcome.exit_code == EXIT_ERROR
    assert refusal in refused.value.outcome.report_text
    assert refused.value.outcome.report_text.startswith("fpga: ")
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


def test_jobs_owing_no_seat_ignore_seat_caps(memory_runtime: Path, runtime: Path) -> None:
    # A synth Job runs no licensed tool: a bad [jobs.seats] refuses nothing and adds no seat.
    _write_jobs(memory_runtime, "vivado = 0")
    _seed_history(memory_runtime, ("a", 400))
    flow = AsicSynthesizeFlow()
    with AdmissionGate(flow, _selection_prepared(flow, runtime)).enter():
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory == job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=500.0, wall_s=1.25
        )

    # A disabled FPGA Flow reaches its own refusal instead of a seat refusal.
    _write_jobs(memory_runtime, None, fpga_enabled=False)
    _seed_records(memory_runtime, ("a", "fpga_impl", 400))
    fpga, prepared = _fpga_prepared(runtime)
    with AdmissionGate(fpga, prepared).enter():
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory == job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=500.0, wall_s=1.25
        )


def test_memory_admitted_job_shrinks_when_a_phase_ends(
    memory_runtime: Path, runtime: Path
) -> None:
    from booley.flows.reservation_phases import report_phase_end

    _seed_history(memory_runtime, ("a", 400), ("b", 200))
    flow = AsicSynthesizeFlow()

    with AdmissionGate(flow, _target_selection(flow, runtime, "a", "b")).enter():
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory is not None and holder.memory.reservation_mb == 500.0
        report_phase_end("a", "synth")
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory is not None and holder.memory.reservation_mb == 250.0
        # The last planned phase's end changes nothing: the claim is held to the end.
        report_phase_end("b", "synth")
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory is not None and holder.memory.reservation_mb == 250.0

    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []
    # The shrinker is gone with the claim: a late report touches nothing.
    from booley.flows import reservation_phases

    # Private seam: the shrinker is a context variable with no public reader.
    assert reservation_phases._active.get() is None


# --- requeue of an OOM-killed Job (#3762, ADR 0087) ---------------------------------

REQUEUE_REASON = "requeued after out-of-memory"
REQUEUE_MARKER = (
    "[requeue] requeued after out-of-memory; the output above is from the killed attempt"
)
# One attempt's behavior: it may print, note an OOM kill, and returns its outcome.
Attempt = Callable[[], EndpointOutcome]


def _killed(text: str = "attempt 1 verdict", exit_code: int = 1) -> EndpointOutcome:
    """An attempt that noted an own OOM kill and returns *exit_code*."""
    from booley.runtime.endpoint_execution import note_job_oom_kill

    note_job_oom_kill()
    return EndpointOutcome(exit_code=exit_code, report_text=text)


def _requeue_harness(
    kind: str, memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> _Harness:
    """A harness whose Job is sized from the Resource History: 400 MiB, reserving 500 MiB."""
    from booley.flows.job_estimates import JobRequest
    from booley.flows.prepared_invocation import TargetSelectionInvocation
    from booley.flows.sim.record_invocation import SimulationRecordInvocation

    _seed_history(memory_runtime, ("demo", 400))
    sized = lambda _self: (JobRequest(flow="synth", target="demo"),)  # noqa: E731
    monkeypatch.setattr(TargetSelectionInvocation, "job_requests", sized)
    monkeypatch.setattr(SimulationRecordInvocation, "job_requests", sized)
    return _build_harness(kind, runtime, monkeypatch)


def _per_attempt(
    harness: _Harness, memory_runtime: Path, *attempts: Attempt
) -> list[job_slots.SlotToken]:
    """Make attempt N run ``attempts[N]`` (the last repeats); return each attempt's held claim."""
    claims: list[job_slots.SlotToken] = []

    def behavior() -> EndpointOutcome:
        number = len(harness.invocations) - 1
        (claim,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        claims.append(claim)
        return attempts[min(number, len(attempts) - 1)]()

    harness.behavior = behavior
    return claims


def test_oom_killed_job_is_requeued_once_to_run_alone(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    claims = _per_attempt(
        harness, memory_runtime, _killed, lambda: EndpointOutcome(report_text="attempt 2 verdict")
    )

    result = harness.execute()

    assert len(harness.invocations) == 2
    first, second = claims
    assert first.memory == job_slots.MemoryClaim(
        budget_mb=1000.0, reservation_mb=500.0, wall_s=1.25
    )
    assert first.queue_reason is None
    # The requeued attempt claims the whole budget, with the reason in its entry.
    assert second.memory == job_slots.MemoryClaim(budget_mb=1000.0, run_alone=True, wall_s=1.25)
    assert second.queue_reason == REQUEUE_REASON
    # The caller sees the requeue and the final attempt's result.
    assert result.exit_code == 0
    assert result.outcome.detail["oom_requeue"] == {"reason": REQUEUE_REASON, "attempts": 2}
    assert "Requeued after out-of-memory" in result.outcome.report_text
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


def test_requeued_job_narrates_the_requeue_reason_while_queued(
    memory_runtime: Path,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    _per_attempt(harness, memory_runtime, _killed, EndpointOutcome)
    real_acquire = job_slots.SlotStore.acquire

    def narrating_acquire(
        self: job_slots.SlotStore, job_class: str, **kwargs: Any
    ) -> job_slots.SlotToken:
        # A claim that is no longer waiting narrates nothing: no entry exists yet.
        kwargs["on_queued"](0)
        # As if the claim had to wait once: a waiting entry exists while it narrates.
        waiting = self.submit(
            job_class,
            pid=os.getpid(),
            memory=kwargs["memory"],
            queue_reason=kwargs["queue_reason"],
        )
        kwargs["on_queued"](0)
        self.release(waiting)
        return real_acquire(self, job_class, **kwargs)

    monkeypatch.setattr(job_slots.SlotStore, "acquire", narrating_acquire)

    harness.execute()

    lines = [
        line for line in capsys.readouterr().err.splitlines() if line.startswith("[admission]")
    ]
    assert len(lines) == 2
    assert lines[0].startswith("[admission] synth: queued (position 1): needs 500 MiB")
    assert lines[0].count(REQUEUE_REASON) == 0
    assert lines[1].startswith(
        f"[admission] synth: queued (position 1): waits to run alone ({REQUEUE_REASON})"
    )
    assert "memory estimate unknown" not in lines[1]


def test_requeue_never_waits_behind_its_own_claim(memory_runtime: Path, runtime: Path) -> None:
    """A deferred release can leave this process's claim behind: refuse, never wait for ourselves."""
    flow = AsicSynthesizeFlow()
    prepared = _selection_prepared(flow, runtime)

    with _holding(memory_runtime, job_slots.MemoryClaim(budget_mb=1000.0, run_alone=True)) as (
        leftover,
    ):
        with (
            pytest.raises(EndpointRejectedError, match="still holds its claim") as refused,
            AdmissionGate(flow, prepared, requeued=True).enter(),
        ):
            pytest.fail("the requeued claim queued behind its own process")
        assert refused.value.outcome.oom_killed is True
        assert [token.seq for token in _held(memory_runtime, job_slots.CLASS_MEMORY)] == [
            leftover.seq
        ]


def test_second_oom_kill_fails_the_job_with_its_oom_outcome(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    _per_attempt(harness, memory_runtime, lambda: _killed("attempt verdict"))

    result = harness.execute()

    # Never a third attempt: the second kill fails the Job as it would without a requeue.
    assert len(harness.invocations) == 2
    assert result.exit_code == 1
    assert result.outcome.oom_killed
    assert "attempt verdict" in result.outcome.report_text
    assert "second attempt" in result.outcome.report_text


def test_requeued_job_reports_only_its_final_attempt(
    memory_runtime: Path,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def first() -> EndpointOutcome:
        print("attempt 1 verdict")
        return _killed("attempt 1 report")

    def second() -> EndpointOutcome:
        print("attempt 2 verdict")
        return EndpointOutcome(report_text="attempt 2 report")

    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    _per_attempt(harness, memory_runtime, first, second)

    result = harness.execute()

    # One report, attempt 2's own.
    (report_path,) = (runtime / "reports" / "synth").glob("*/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "attempt 2 report" in report["report_text"]
    assert "attempt 1" not in report_path.read_text(encoding="utf-8")
    assert report["detail"]["oom_requeue"]["attempts"] == 2
    assert "attempt 1" not in result.outcome.report_text
    # The streams keep the killed attempt's output, with the marker line before attempt 2's.
    assert capsys.readouterr().out.splitlines() == [
        "attempt 1 verdict",
        REQUEUE_MARKER,
        "attempt 2 verdict",
    ]


def test_requeued_attempt_reruns_the_same_prepared_request(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _requeue_harness("sim", memory_runtime, runtime, monkeypatch)
    _per_attempt(harness, memory_runtime, _killed, EndpointOutcome)

    harness.execute()

    # The same prepared simulation (Targets, Test Runs, drawn seeds) twice,
    # each under its own borrowed admission.
    first, second = harness.run_prepared
    assert first is second
    first_admission, second_admission = harness.run_admissions
    assert isinstance(first_admission, AdmissionContext)
    assert isinstance(second_admission, AdmissionContext)
    assert first_admission is not second_admission
    assert isinstance(second_admission.outer_token, job_slots.SlotToken)
    assert second_admission.outer_token.queue_reason == REQUEUE_REASON


def _requeue_failure_result(
    failure: Exception,
    memory_runtime: Path,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_attempt: Callable[[_Harness], Attempt] = lambda _harness: _killed,
) -> ExecutionResult:
    """Run a Job whose requeued claim fails with *failure* (its first claim is real)."""
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    _per_attempt(harness, memory_runtime, first_attempt(harness))
    real_acquire = job_slots.SlotStore.acquire

    def acquire(self: job_slots.SlotStore, job_class: str, **kwargs: Any) -> job_slots.SlotToken:
        if kwargs.get("queue_reason") is not None:
            raise failure
        return real_acquire(self, job_class, **kwargs)

    monkeypatch.setattr(job_slots.SlotStore, "acquire", acquire)
    result = harness.execute()
    assert len(harness.invocations) == 1  # the requeued attempt never started
    return result


def test_job_cancelled_while_requeued_ends_cancelled(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = job_slots.ClaimLostError("withdrawn")
    result = _requeue_failure_result(failure, memory_runtime, runtime, monkeypatch)

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith("CANCELLED:")
    assert "did not start again" in result.outcome.report_text
    assert result.outcome.detail["oom_requeue"]["reason"] == REQUEUE_REASON
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


@pytest.mark.skipif(sys.platform == "win32", reason="the command kills itself with SIGKILL")
def test_killed_attempt_runs_no_command_after_its_oom_kill(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real Flow command stops the attempt at its first OOM kill: the next never starts."""
    import itertools

    from booley.flows import job_resources

    # The container's OOM counter rises over attempt 1's first command only.
    counts = itertools.chain((0, 1), itertools.repeat(1))
    monkeypatch.setattr(
        job_resources,
        "cgroup_memory_events",
        lambda: job_resources.MemoryEvents(*[next(counts)] * 2),
    )
    marker = runtime / "attempts.txt"
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    # Private seam: the Flow's command runner has no public caller short of a real EDA tool.
    run_command = harness.flow._execute_local

    def attempt() -> EndpointOutcome:
        number = len(harness.invocations)
        run_command(
            [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
        )
        run_command(
            [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('attempt {number}\\n')"]
        )
        return EndpointOutcome(exit_code=1 if number == 1 else 0)

    harness.behavior = attempt

    harness.execute()

    assert len(harness.invocations) == 2
    assert marker.read_text(encoding="utf-8").splitlines() == ["attempt 2"]


# --- progress of a requeued Job (#3763) ----------------------------------------------

PROGRESS_RUN_ID = "run-3763"


def _progress_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the Job the run id its progress documents are stamped with."""
    monkeypatch.setenv("BOOLEY_RUN_ID", PROGRESS_RUN_ID)


def _checkpoint(harness: _Harness, outcome: Callable[[], EndpointOutcome]) -> Attempt:
    """An attempt that checkpoints a ``running`` progress document, then ends as *outcome*."""
    from booley.flows.progress_lifecycle import progress_document, write_progress_json

    def attempt() -> EndpointOutcome:
        directory = harness.flow.reserve_invocation_dir()
        assert directory is not None
        document = progress_document(
            flow="synth",
            run_id=PROGRESS_RUN_ID,
            phase="running",
            targets=("demo",),
            completed_targets=(),
            detail={},
        )
        write_progress_json(directory / "progress.json", document)
        return outcome()

    return attempt


def _progress_of_job(runtime: Path) -> dict[str, Any] | None:
    """What a caller polling the Job reads as its progress."""
    # Private seam: an MCP poll reads a Job's progress through this reader, reachable
    # publicly only from a live MCP session; read_progress_for_run lacks its fallbacks.
    from booley.mcp.job_results import _progress_for_run_id

    return _progress_for_run_id("synth", PROGRESS_RUN_ID, report_dirs=(runtime / "reports",))


def test_caller_sees_no_progress_from_the_killed_attempt(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _progress_run(monkeypatch)

    # The final attempt runs: nothing of the killed one is visible while it runs, or after.
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    seen_by_final_attempt: list[dict[str, Any] | None] = []

    def final_attempt() -> EndpointOutcome:
        seen_by_final_attempt.append(_progress_of_job(runtime))
        return EndpointOutcome()

    _per_attempt(harness, memory_runtime, _checkpoint(harness, _killed), final_attempt)
    harness.execute()
    assert seen_by_final_attempt == [None]
    assert _progress_of_job(runtime) is None

    # Withdrawn while queued again: the Job ends with no report, and still no progress.
    _requeue_failure_result(
        job_slots.ClaimLostError("withdrawn"),
        memory_runtime,
        runtime,
        monkeypatch,
        first_attempt=lambda harness: _checkpoint(harness, _killed),
    )
    assert _progress_of_job(runtime) is None


def test_progress_retirement_failure_does_not_stop_the_requeue(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows import progress_lifecycle

    _progress_run(monkeypatch)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("progress is held")

    # Private seam: the only way to fail the retirement write without a file-permission trick,
    # which does not hold on Windows.
    monkeypatch.setattr(progress_lifecycle, "_replace_if_unchanged", refuse)
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    _per_attempt(harness, memory_runtime, _checkpoint(harness, _killed), EndpointOutcome)

    result = harness.execute()

    assert len(harness.invocations) == 2  # the requeue went ahead
    assert result.exit_code == EXIT_ERROR
    error = result.outcome.detail["completion_error"]
    assert error["operation"] == "retire the killed attempt's progress"
    assert "progress is held" in error["message"]


def _killed_but_passed() -> EndpointOutcome:
    return _killed(exit_code=0)


def _cancelled_after_kill() -> EndpointOutcome:
    from booley.runtime.endpoint_execution import EXIT_CANCELLED

    return _killed(exit_code=EXIT_CANCELLED)


def _cancelled_after_skipped_command() -> EndpointOutcome:
    from booley.runtime.endpoint_execution import (
        EXIT_CANCELLED,
        job_stopped_after_oom,
        note_job_oom_kill,
    )

    note_job_oom_kill()
    assert job_stopped_after_oom()  # a command was skipped
    return EndpointOutcome(exit_code=EXIT_CANCELLED)


@pytest.mark.parametrize(
    ("case", "outcome"),
    (
        # Armed, but it fails without an OOM kill.
        pytest.param("failed", lambda: EndpointOutcome(exit_code=1), id="failed"),
        # OOM-killed, but the Job still succeeded.
        pytest.param("killed_but_passed", _killed_but_passed, id="killed_but_passed"),
        pytest.param("cancelled_after_kill", _cancelled_after_kill, id="cancelled_after_kill"),
        pytest.param(
            "cancelled_after_skipped_command",
            _cancelled_after_skipped_command,
            id="cancelled_after_skipped_command",
        ),
        # Under a parent's claim the Job holds none of its own: nothing to requeue.
        pytest.param("inherited_claim", _killed, id="inherited_claim"),
    ),
)
def test_job_that_is_not_requeued_runs_once_and_keeps_its_progress(
    case: str,
    outcome: Callable[[], EndpointOutcome],
    memory_runtime: Path,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.flows.progress_lifecycle import read_progress_for_run

    _progress_run(monkeypatch)
    if case == "inherited_claim":
        monkeypatch.setenv(job_slots.INHERITED_SLOT_ENV, "memory/1")
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    attempt = _checkpoint(harness, outcome)
    if case == "inherited_claim":
        harness.behavior = attempt
    else:
        _per_attempt(harness, memory_runtime, attempt)

    harness.execute()

    assert len(harness.invocations) == 1
    found = read_progress_for_run((runtime / "reports",), "synth", PROGRESS_RUN_ID)
    assert found is not None
    assert found[1]["phase"] == "running"
    assert found[1]["run_id"] == PROGRESS_RUN_ID


# --- Verilator -j from the container's cores and the running Jobs (#3246) ------------


@pytest.fixture
def eight_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """8 available cores, as admission counts them for an admitted Job's ``-j``."""
    monkeypatch.setattr(endpoint_admission, "available_cpu_count", lambda: 8)


def _verilator_inspection(*make_options: str) -> Any:
    from tests.flows.sim.test_build_parallelism import _inspection

    return _inspection(modern=make_options)


def _verilator_arguments_in_admitted_job(
    memory_runtime: Path, runtime: Path, *inspections: Any
) -> list[tuple[str, ...]]:
    """The Verilator backend arguments each inspection gets inside one admitted Job."""
    from booley.flows.sim.build_parallelism import verilator_backend_arguments

    # A history that fits beside the other Jobs' reservations.
    _seed_history(memory_runtime, ("a", 40))
    flow = AsicSynthesizeFlow()
    with AdmissionGate(flow, _selection_prepared(flow, runtime)).enter():
        return [verilator_backend_arguments(inspection) for inspection in inspections]


@pytest.mark.usefixtures("eight_cores")
@pytest.mark.parametrize(
    ("others", "jobs_option"), ((0, "-j8"), (1, "-j4"), (2, "-j2"), (9, "-j1"))
)
def test_memory_admitted_verilator_build_shares_cores_with_the_running_jobs(
    others: int, jobs_option: str, memory_runtime: Path, runtime: Path
) -> None:
    from booley.flows.sim.build_parallelism import verilator_backend_arguments

    # Other running Jobs -> the -j this Job gets: 8 // (others + 1), rounded down.
    reservation = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=50.0)
    _seed_history(memory_runtime, ("a", 40))
    flow = AsicSynthesizeFlow()
    expected = (jobs_option, "VM_PARALLEL_BUILDS=1")
    with (
        _holding(memory_runtime, *([reservation] * others)),
        AdmissionGate(flow, _selection_prepared(flow, runtime)).enter(),
    ):
        inspection = _verilator_inspection()
        assert verilator_backend_arguments(inspection) == expected
        # The count is fixed at the Job's start: a Job that arrives later changes nothing.
        with _holding(memory_runtime, reservation):
            assert verilator_backend_arguments(inspection) == expected


@pytest.mark.usefixtures("eight_cores")
def test_memory_admitted_job_keeps_a_project_set_thread_count(
    memory_runtime: Path, runtime: Path
) -> None:
    with _holding(memory_runtime, job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=50.0)):
        authored, plain = _verilator_arguments_in_admitted_job(
            memory_runtime,
            runtime,
            _verilator_inspection("-j2"),
            _verilator_inspection(),
        )

    # The authored -j2 passes through untouched; the same Job's other Target is shared.
    assert authored == ()
    assert plain == ("-j4", "VM_PARALLEL_BUILDS=1")


def test_failed_thread_count_releases_the_memory_reservation(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(*_args: object) -> int:
        raise OSError("slot directory unreadable")

    monkeypatch.setattr(endpoint_admission, "job_threads_at_start", unreadable)
    _seed_history(memory_runtime, ("a", 40))
    flow = AsicSynthesizeFlow()
    with (
        pytest.raises(OSError, match="unreadable"),
        AdmissionGate(flow, _selection_prepared(flow, runtime)).enter(),
    ):
        pytest.fail("admission must not be entered")

    # The claim was admitted before the count failed; nothing may stay reserved.
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []
