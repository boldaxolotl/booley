"""One Flow-neutral admission path for every endpoint.

The same admission outcomes (withdrawn queued run, invalid runtime
identity, no-slot cases, release at run end) must hold for Simulation and for a
non-Simulation Flow, and must not depend on the endpoint's name.
"""

from __future__ import annotations

import json
import os
import shutil
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
    _Claim,
)
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.endpoint_state import EndpointState
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EndpointOutcome,
    EndpointRejectedError,
    ExecutionResult,
)
from booley.runtime.execution_records import RUNTIME_EXECUTION_ENV
from tests.oom_priority_support import (
    PROC_PATH,
    FakeProc,
    inert_oom_proc,  # noqa: F401  (fixture, used by memory_runtime)
    oom_priority,
)

FLOW_TYPES = {"sim": SimulateFlow, "synth": AsicSynthesizeFlow}


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A session runtime with no ambient identity, slot or state variables."""
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.chdir(tmp_path)
    for name in (
        "BOOLEY_STATE_FILE",
        "BOOLEY_TICKET_FILE",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_RUNTIME_DIR",
        RUNTIME_EXECUTION_ENV,
        job_slots.INHERITED_SLOT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


class _FakeStore:
    """A slot store that records which claims were released."""

    def __init__(self) -> None:
        self.released: list[object] = []

    def release(self, token: object) -> None:
        self.released.append(token)


@dataclass
class _Harness:
    """One real Flow wired so its invocation body and admission are observable."""

    flow: SimulateFlow | AsicSynthesizeFlow
    request: Any
    invocations: list[object] = field(default_factory=list[object])
    admissions: list[AdmissionContext | None] = field(
        default_factory=list[AdmissionContext | None]
    )
    behavior: Callable[[], EndpointOutcome] = EndpointOutcome
    # The admission each invocation body received from its prepared invocation.
    run_admissions: list[object | None] = field(default_factory=list[object | None])
    # The prepared simulation each Simulation Campaign body received.
    run_prepared: list[object] = field(default_factory=list[object])
    # Prepares the Flow's real invocation kind; runs once, when the test first executes.
    prepare: Callable[[], None] = lambda: None
    # The execution adapter the Flow runs under; ``None`` is the Standalone one.
    adapter: Any = None

    def execute(self) -> ExecutionResult:
        self.prepare()
        self.prepare = lambda: None
        return self.flow.execute(self.request, adapter=self.adapter)


@pytest.fixture(params=("sim", "synth"))
def harness(
    request: pytest.FixtureRequest, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> _Harness:
    """Simulation and a non-Simulation Flow, reduced to their invocation body.

    Both run their real prepared-invocation kind: the Simulation Campaign (which
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
            from booley.flows.sim.record_invocation import SimulationCampaignInvocation
            from tests.flows.invocation_doubles import prepare_as

            invocation = SimulationCampaignInvocation(
                flow, PreparedSimulationEndpoint((), None, (), {})
            )
            prepare_as(monkeypatch, flow, invocation)
    else:
        # Class-level, so the real Target-selection kind runs the double.
        monkeypatch.setattr(type(flow), "_run", lambda _self: run())

        def prepare() -> None:
            from booley.flows.prepared_invocation import TargetSelectionInvocation
            from tests.flows.invocation_doubles import prepare_as

            prepare_as(monkeypatch, flow, TargetSelectionInvocation(flow, None, flow._run))

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


def _use_slot(monkeypatch: pytest.MonkeyPatch, store: _FakeStore, claim: object) -> None:
    """Admit every Flow Job with *claim*, as if its Memory Reservation were granted."""
    monkeypatch.setattr(
        endpoint_admission,
        "_acquire_memory_reservation",
        lambda _endpoint, _prepared, *, requeued=False: _Claim(store, claim),  # type: ignore[arg-type]
    )


def _refuse_slot(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    def refuse(_endpoint: EndpointState, _prepared: object, *, requeued: bool = False) -> object:
        raise failure

    monkeypatch.setattr(endpoint_admission, "_acquire_memory_reservation", refuse)


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


def test_admitted_endpoint_borrows_admission_context(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, claim = _FakeStore(), object()
    _use_slot(monkeypatch, store, claim)

    assert harness.execute().exit_code == 0

    (borrowed,) = harness.admissions
    assert isinstance(borrowed, AdmissionContext)
    assert borrowed.mode == "managed"
    assert borrowed.outer_token is claim


def test_invalid_runtime_identity_names_endpoint(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUNTIME_EXECUTION_ENV, "not-an-execution-id")
    monkeypatch.setattr(
        EndpointState,
        "_acquire_job_slot",
        lambda _self: pytest.fail("slot acquisition must not run"),
    )

    result = harness.execute()

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith(
        f"{harness.flow.name}: invalid {RUNTIME_EXECUTION_ENV}: "
    )
    assert harness.invocations == []


def test_disagreeing_token_identity_names_endpoint(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed token linked to another execution is rejected and still released."""

    class Token:
        execution_id = "a" * 32

    store, claim = _FakeStore(), Token()
    monkeypatch.setenv(RUNTIME_EXECUTION_ENV, "b" * 32)
    _use_slot(monkeypatch, store, claim)

    result = harness.execute()

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith(
        f"{harness.flow.name}: invalid {RUNTIME_EXECUTION_ENV}: "
    )
    assert harness.invocations == []
    assert store.released == [claim]


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
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
    behavior: Callable[[], EndpointOutcome],
) -> None:
    store, claim = _FakeStore(), object()
    _use_slot(monkeypatch, store, claim)
    harness.behavior = behavior

    if ending == "cancelled":
        with pytest.raises(KeyboardInterrupt):
            harness.execute()
    else:
        result = harness.execute()
        assert result.exit_code == (0 if ending == "success" else EXIT_ERROR)

    assert len(harness.invocations) == 1
    (borrowed,) = harness.admissions
    assert isinstance(borrowed, AdmissionContext)
    assert borrowed.outer_token is claim
    assert store.released == [claim]


def _prepared(*, dry_run: bool = False, borrowing: bool = False) -> PreparedExecution:
    """A prepared execution; *borrowing* selects an invocation that takes admission."""
    from booley.flows.prepared_invocation import OrdinaryRunInvocation

    return PreparedExecution(
        display_target=None,
        display_label=None,
        dry_run=dry_run,
        non_persisting_dry_run=dry_run,
        invocation=_borrowing_invocation()
        if borrowing
        else OrdinaryRunInvocation(EndpointOutcome),
    )


def _borrowing_invocation() -> object:
    """A prepared invocation of the borrowing kind, as a Simulation Campaign is."""
    from booley.flows.sim.flow import PreparedSimulationEndpoint
    from booley.flows.sim.record_invocation import SimulationCampaignInvocation

    return SimulationCampaignInvocation(
        SimulateFlow(), PreparedSimulationEndpoint((), None, (), {})
    )


def test_dry_run_invocation_runs_without_admission(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-persisting dry run takes no slot and writes no numbered report."""
    monkeypatch.setattr(
        EndpointState,
        "_acquire_job_slot",
        lambda _self: pytest.fail("a dry run must not take a slot"),
    )
    harness.request = replace(harness.request, dry_run=True)

    result = harness.execute()

    assert result.exit_code == 0
    assert len(harness.invocations) == 1
    assert harness.admissions == [None]
    # Only the Simulation Campaign borrows admission, and it saw none.
    if isinstance(harness.flow, SimulateFlow):
        assert harness.run_admissions == [None]
    assert not (harness.request.report_dir / harness.flow.name).exists()


class _StubEndpoint:
    """The least an admitted endpoint needs, with the real slot acquisition."""

    endpoint_kind = "flow"
    _pre_run_head: str | None = None

    def __init__(self, name: str, job_class: str | None) -> None:
        self.name = name
        self._job_class = job_class
        self.acquisitions = 0

    def _resolve_job_class(self) -> str | None:
        return self._job_class

    def _memory_admitted(self) -> bool:
        # The production policy, so the Specialist guard exercises it rather than a copy.
        return EndpointState._memory_admitted(self)  # type: ignore[arg-type]

    def _get_head_sha(self) -> str | None:
        return None

    def _criterion_binding_gate(self) -> EndpointOutcome | None:
        return None

    def _acquire_job_slot(self) -> tuple[object | None, object | None]:
        self.acquisitions += 1
        return endpoint_admission._acquire_job_slot(self)  # type: ignore[arg-type]


@pytest.mark.usefixtures("runtime")
@pytest.mark.parametrize("case", ("unclassed", "inherited", "no_runtime"))
def test_no_slot_cases_admit_unmanaged(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = _StubEndpoint("custom", None if case == "unclassed" else job_slots.CLASS_LIGHT)
    if case == "inherited":
        monkeypatch.setenv(job_slots.INHERITED_SLOT_ENV, "light/slot")
    if case == "no_runtime":
        monkeypatch.setattr(job_slots, "slots_dir", lambda _project=None: None)
    monkeypatch.setattr(
        job_slots,
        "SlotStore",
        lambda *_args, **_kwargs: pytest.fail("no slot store may be opened"),
    )

    with endpoint_admission.admission(endpoint, _prepared()) as borrowed:  # type: ignore[arg-type]
        assert isinstance(borrowed, AdmissionContext)
        assert borrowed.mode == "unmanaged"
        assert borrowed.slot_store is None
        assert borrowed.outer_token is None
    assert endpoint.acquisitions == 1


@pytest.mark.usefixtures("runtime")
@pytest.mark.parametrize("name", ("sim", "other"))
def test_admission_ignores_endpoint_name(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Simulation payload on a differently named endpoint is admitted identically."""
    endpoint = _StubEndpoint(name, job_slots.CLASS_LIGHT)
    store, claim = _FakeStore(), object()
    monkeypatch.setattr(endpoint, "_acquire_job_slot", lambda: (store, claim))
    prepared = _prepared(borrowing=True)

    with endpoint_admission.admission(endpoint, prepared) as borrowed:  # type: ignore[arg-type]
        assert isinstance(borrowed, AdmissionContext)
        assert (borrowed.mode, borrowed.outer_token) == ("managed", claim)
    assert store.released == [claim]

    def withdrawn() -> object:
        raise job_slots.ClaimLostError("vanished")

    monkeypatch.setattr(endpoint, "_acquire_job_slot", withdrawn)
    with (
        pytest.raises(EndpointRejectedError) as rejected,
        endpoint_admission.admission(endpoint, prepared),  # type: ignore[arg-type]
    ):
        pytest.fail("a withdrawn run must not be admitted")
    assert rejected.value.outcome.exit_code == EXIT_ERROR
    assert rejected.value.outcome.report_text == (
        f"CANCELLED: this queued '{name}' run was withdrawn (booley_cancel) before it started."
    )


def test_binding_gate_rejects_before_slot_for_non_flow_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _StubEndpoint("custom", job_slots.CLASS_LIGHT)
    endpoint.endpoint_kind = "specialist"
    rejection = EndpointOutcome(exit_code=EXIT_ERROR, report_text="no authorized binding")
    monkeypatch.setattr(endpoint, "_criterion_binding_gate", lambda: rejection)
    monkeypatch.setattr(
        endpoint, "_acquire_job_slot", lambda: pytest.fail("binding gate runs before the slot")
    )

    with (
        pytest.raises(EndpointRejectedError) as rejected,
        endpoint_admission.admission(endpoint, _prepared()),  # type: ignore[arg-type]
    ):
        pytest.fail("a rejected endpoint must not be admitted")
    assert rejected.value.outcome is rejection


@pytest.mark.usefixtures("runtime")
@pytest.mark.parametrize(
    "failure",
    (
        job_slots.ClaimLostError("vanished"),
        AdmissionIdentityError("mismatch"),
    ),
    ids=("claim_lost", "identity"),
)
def test_body_failures_are_not_remapped_to_admission_rejections(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only entering admission is mapped; the invocation body's errors pass through."""
    endpoint = _StubEndpoint("sim", job_slots.CLASS_LIGHT)
    store, claim = _FakeStore(), object()
    monkeypatch.setattr(endpoint, "_acquire_job_slot", lambda: (store, claim))

    with (
        pytest.raises(type(failure)),
        endpoint_admission.admission(endpoint, _prepared(borrowing=True)),  # type: ignore[arg-type]
    ):
        raise failure
    assert store.released == [claim]


def test_invocation_exception_is_normalized(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected invocation failure becomes the stable exit-2 report, and cleans up."""
    from booley.runtime.exception_diagnostics import exception_report_text

    store, claim = _FakeStore(), object()
    _use_slot(monkeypatch, store, claim)
    harness.behavior = _fail
    stdout_before = sys.stdout

    result = harness.execute()

    assert result.exit_code == EXIT_ERROR
    expected = exception_report_text(harness.flow.name, RuntimeError("endpoint failed"))
    # The persisted diagnostic path trails the stable "<name> failed: <Type>: <message>" text.
    assert result.outcome.report_text.startswith(expected)
    assert sys.stdout is stdout_before
    assert store.released == [claim]


def test_publication_resources_outlive_final_report(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invocation resources and the slot are held until the final report is written."""
    store, claim = _FakeStore(), object()
    _use_slot(monkeypatch, store, claim)
    flow = harness.flow
    events: list[str] = []
    at_final_report: list[tuple[list[str], list[object]]] = []

    def behavior() -> EndpointOutcome:
        flow.publication_resources.callback(events.append, "resources_released")
        if isinstance(flow, SimulateFlow):
            # The numbered report directory the final report writes into.
            assert flow.reserve_invocation_dir() is not None
        return EndpointOutcome()

    original_write_report = type(flow).write_report

    def spy_write_report(self: object, result: EndpointOutcome) -> object:
        at_final_report.append((list(events), list(store.released)))
        return original_write_report(self, result)  # type: ignore[arg-type]

    monkeypatch.setattr(type(flow), "write_report", spy_write_report)
    harness.behavior = behavior

    assert harness.execute().exit_code == 0

    assert at_final_report
    assert at_final_report[-1] == ([], [])
    assert events == ["resources_released"]
    assert store.released == [claim]


def test_target_authorization_precedes_job_admission(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulation refuses an unauthorized prepared Target before taking a Job Slot."""
    from booley.criteria.state import DevelopmentState
    from tests.target_test_support import make_target_handle

    state_path = runtime / "state.json"
    state = DevelopmentState.load(state_path)
    # Binds the requested token ``demo`` (so the generic gate passes), nothing else.
    state.init_criteria({"sim_pass_demo": True}, strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    # Real preparation reaches the real ``authorize_targets`` with a Target no Criterion binds.
    stub = make_target_handle(runtime, "other")
    monkeypatch.setattr(SimulateFlow, "prepare_target_endpoint", lambda _self: None)
    monkeypatch.setattr(
        SimulateFlow,
        "_prepare_campaign_targets",
        lambda _self: ((stub,), None, ("other",), {}),
    )
    monkeypatch.setattr(SimulateFlow, "_validate_prepared_selection", lambda *_args: None)
    monkeypatch.setattr(
        EndpointState,
        "_acquire_job_slot",
        lambda _self: pytest.fail("an unauthorized Target must not reach admission"),
    )
    ran: list[object] = []
    monkeypatch.setattr(
        SimulateFlow, "run_prepared_simulation", lambda *_args: ran.append(object())
    )
    flow = SimulateFlow()

    result = flow.execute(
        flow.request_type(target="demo", work_dir=runtime, report_dir=runtime / "reports")
    )

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == (
        "sim: Goal Mode has no authorized Criterion binding for Target(s): other"
    )
    assert ran == []
    # Preparation itself returns the refusal, so no prepared invocation reaches admission.
    refusal = flow.prepare_endpoint_invocation()
    assert isinstance(refusal, EndpointOutcome)
    assert refusal.report_text == result.outcome.report_text


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
) -> Path:
    """The runtime with a real slots root and a 1,000 MiB Memory Budget."""
    booley_dir = runtime / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text('[jobs]\nmemory_budget = "1000m"\n', encoding="utf-8")
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    monkeypatch.setattr(endpoint_admission, "resolve_common_booley_dir", lambda: booley_dir)
    return booley_dir


def _selection_prepared(flow: AsicSynthesizeFlow, work_dir: Path) -> PreparedExecution:
    """The preparation of *flow* as a Target-selection invocation over its one Target ``a``."""
    from booley.flows.prepared_invocation import TargetSelectionInvocation
    from tests.target_test_support import make_target_handle

    handle = make_target_handle(work_dir, "a", drivable_by=("synth",))
    flow._state = SimpleNamespace(criteria={})  # type: ignore[assignment]
    flow._args = SimpleNamespace(work_dir=work_dir, target="a")
    selection = SimpleNamespace(handles=(handle,), matches=lambda *_args: True)
    return PreparedExecution(
        display_target=None,
        display_label=None,
        dry_run=False,
        non_persisting_dry_run=False,
        invocation=TargetSelectionInvocation(flow, selection, flow._run),  # type: ignore[arg-type]
    )


def _held(booley_dir: Path, job_class: str) -> list[job_slots.SlotToken]:
    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir))
    return store.observe(job_class)[0]


def test_memory_admission_claims_the_estimated_reservation(
    memory_runtime: Path, runtime: Path
) -> None:
    _seed_history(memory_runtime, ("a", 400))
    flow = AsicSynthesizeFlow()

    with AdmissionGate(flow, _selection_prepared(flow, runtime)).enter() as context:
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
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


@pytest.mark.parametrize("budget", ("lots", None))
def test_memory_admission_survives_a_budget_it_cannot_compute(
    budget: str | None, memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad ``[jobs].memory_budget`` or unreadable MemTotal serializes the Job; never refuses."""
    if budget is None:
        (memory_runtime / "booley.toml").unlink()
        monkeypatch.setattr(
            "booley.flows.memory_admission.compute_memory_budget",
            lambda _config: (_ for _ in ()).throw(OSError("MemTotal unreadable")),
        )
    else:
        (memory_runtime / "booley.toml").write_text(f'[jobs]\nmemory_budget = "{budget}"\n')

    with AdmissionGate(AsicSynthesizeFlow()).enter() as context:
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory == job_slots.MemoryClaim(budget_mb=0.0, run_alone=True)
        assert context.memory_warning is not None
        assert context.memory_warning.startswith("Memory Budget unavailable (")


def test_over_budget_job_result_carries_the_memory_warning(
    runtime: Path, memory_runtime: Path
) -> None:
    from booley.flows.job_estimates import JobRequest
    from booley.flows.prepared_invocation import TargetSelectionInvocation
    from booley.flows.sim.record_invocation import SimulationCampaignInvocation

    # A 2,000 MiB peak is a 2,500 MiB estimate against the 1,000 MiB budget.
    _seed_history(memory_runtime, ("a", 2000))
    request = (JobRequest("synth", "a"),)
    warning = "Memory estimate 2500 MiB exceeds the Memory Budget of 1000 MiB; this Job ran alone."

    for kind in ("sim", "synth"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(TargetSelectionInvocation, "job_requests", lambda _self: request)
            patch.setattr(SimulationCampaignInvocation, "job_requests", lambda _self: request)
            harness = _build_harness(kind, runtime, patch)
            harness.behavior = lambda: EndpointOutcome(report_text="RESULT: PASS")

            result = harness.execute()

        assert result.exit_code == 0, kind
        assert result.outcome.detail["memory_admission_warning"] == warning, kind
        assert result.outcome.report_text.splitlines()[:2] == ["RESULT: PASS", warning], kind
        (borrowed,) = harness.admissions
        assert borrowed is not None and borrowed.memory_warning == warning, kind


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


def test_only_a_memory_admitted_job_lends_work_item_sizing(
    memory_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sizing a Campaign needs for its work items' own claims comes with the Job's claim (#3772)."""
    with AdmissionGate(AsicSynthesizeFlow()).enter() as context:
        assert context.memory_sizing is not None
        assert context.memory_sizing.booley_dir == memory_runtime
        assert context.memory_sizing.warnings == []
    # A Job under its parent's claim takes none of its own, so it has nothing to lend.
    monkeypatch.setenv(job_slots.INHERITED_SLOT_ENV, "memory/entry")
    with AdmissionGate(AsicSynthesizeFlow()).enter() as context:
        assert context.memory_sizing is None


def test_specialist_keeps_its_light_slot_under_memory_admission(
    memory_runtime: Path,
) -> None:
    endpoint = _StubEndpoint("reviewer", job_slots.CLASS_LIGHT)

    with endpoint_admission.admission(endpoint, _prepared()) as borrowed:  # type: ignore[arg-type]
        assert isinstance(borrowed, AdmissionContext)
        assert borrowed.mode == "managed"
        assert borrowed.memory_warning is None
        assert len(_held(memory_runtime, job_slots.CLASS_LIGHT)) == 1
        assert not (job_slots.slots_dir(memory_runtime) / job_slots.CLASS_MEMORY).exists()
    assert endpoint.acquisitions == 1


def _memory_store(root: Path, *holders: tuple[int, float, str]) -> job_slots.SlotStore:
    """A store whose memory class holds each ``(pid, MiB, run ID)`` as a labelled synth Job."""
    import os

    store = job_slots.SlotStore(root)
    for pid, reservation_mb, run_id in holders:
        token = store.submit(
            job_slots.CLASS_MEMORY,
            pid=os.getpid(),  # a live process, or the reaper would drop the entry
            memory=job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=reservation_mb),
            job=job_slots.JobLabel(run_id, "synth", f"t{pid}"),
        )
        assert store.refresh(token).state == job_slots.HOLDING
    return store


def test_memory_queue_narration_names_the_need_and_the_largest_holders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    store = _memory_store(tmp_path / "slots", (1, 200.0, "run-1"), (2, 500.0, "run-2"))
    pid = os.getpid()
    store.submit(
        job_slots.CLASS_MEMORY,
        pid=pid,
        memory=job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=400.0),
    )
    endpoint = SimpleNamespace(name="synth", _display_identity=None)

    endpoint_admission._memory_narrator(endpoint, store, pid)(0)  # type: ignore[arg-type]

    assert capsys.readouterr().err == (
        "[slot] synth: waiting for memory (position 1): needs 400 MiB; 300 MiB free "
        "of the 1,000 MiB Memory Budget; largest holders: run-2 (synth, t2) 500 MiB, "
        "run-1 (synth, t1) 200 MiB\n"
    )


def test_memory_narrator_says_nothing_once_the_claim_has_started(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    store = job_slots.SlotStore(tmp_path / "slots")
    endpoint = SimpleNamespace(name="synth", _display_identity=None)

    endpoint_admission._memory_narrator(endpoint, store, os.getpid())(0)  # type: ignore[arg-type]

    assert capsys.readouterr().err == ""


def test_slot_queue_narration_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Store:
        def describe_holders(self, job_class: str) -> str:
            return f"pid 7 ({job_class})"

    endpoint = SimpleNamespace(name="sim", _display_identity=None)
    narrate = endpoint_admission._queue_narrator(
        endpoint,  # type: ignore[arg-type]
        Store(),  # type: ignore[arg-type]
        job_slots.CLASS_LIGHT,
        "light slot",
    )

    narrate(0)

    assert capsys.readouterr().err == (
        "[slot] sim: waiting for light slot (position 1); held by pid 7 (light)\n"
    )


def test_memory_claim_names_its_run_flow_and_target(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_history(memory_runtime, ("a", 400))
    monkeypatch.setenv("BOOLEY_RUN_ID", "run-7")
    flow = AsicSynthesizeFlow()

    with AdmissionGate(flow, _selection_prepared(flow, runtime)).enter():
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.job == job_slots.JobLabel("run-7", "synth", "a")


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


def test_unparseable_holder_timeout_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_SLOT_TIMEOUT_S", "soon")
    assert endpoint_admission._holder_deadline_seconds() is None
    monkeypatch.setenv("BOOLEY_SLOT_TIMEOUT_S", "30")
    assert endpoint_admission._holder_deadline_seconds() == 60.0
    monkeypatch.delenv("BOOLEY_SLOT_TIMEOUT_S")
    assert endpoint_admission._holder_deadline_seconds() is None


# --- lint and Custom Flow Jobs (#3703) ------------------------------


def _prepared_for(invocation: Any) -> PreparedExecution:
    return PreparedExecution(
        display_target=None,
        display_label=None,
        dry_run=False,
        non_persisting_dry_run=False,
        invocation=invocation,
    )


def _lint_prepared(work_dir: Path) -> tuple[Any, PreparedExecution]:
    """A lint Job over Targets ``a`` and ``b``, prepared as a Target-selection invocation."""
    from booley.flows.lint.flow import LintFlow
    from booley.flows.prepared_invocation import TargetSelectionInvocation
    from tests.target_test_support import make_target_handle

    flow = LintFlow()
    handles = tuple(make_target_handle(work_dir, name, drivable_by=("lint",)) for name in "ab")
    flow._state = SimpleNamespace(criteria={})  # type: ignore[assignment]
    flow._args = SimpleNamespace(work_dir=work_dir, target="a,b")
    selection = SimpleNamespace(handles=handles, matches=lambda *_args: True)
    invocation = TargetSelectionInvocation(flow, selection, flow._run)  # type: ignore[arg-type]
    return flow, _prepared_for(invocation)


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
        assert _held(memory_runtime, job_slots.CLASS_LIGHT) == []
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


class _QueuedError(Exception):
    """Raised from the queue narrator to stop the wait once the Job is known to be QUEUED."""


def test_lint_and_custom_flow_jobs_queue_behind_a_run_alone_job(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for kind in ("lint", "custom"):
        _assert_queues_behind_run_alone_job(kind, memory_runtime, runtime, monkeypatch)


def _assert_queues_behind_run_alone_job(
    kind: str, memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A *kind* Job waits (QUEUED, position 0) while a run-alone Job holds the budget."""
    import os

    flow, prepared = _admitted_job(kind, runtime, memory_runtime)
    store = job_slots.SlotStore(job_slots.slots_dir(memory_runtime))
    pid = os.getpid()
    first = store.acquire(
        job_slots.CLASS_MEMORY,
        pid=pid,
        argv=endpoint_admission._proc_cmdline(pid) or [],
        memory=job_slots.MemoryClaim(budget_mb=1000.0, run_alone=True),
    )
    positions: list[int] = []

    def narrator(*_args: object, **_kwargs: object) -> Callable[[int], None]:
        def narrate(position: int) -> None:
            positions.append(position)
            raise _QueuedError

        return narrate

    monkeypatch.setattr(endpoint_admission, "_memory_narrator", narrator)

    try:
        with pytest.raises(_QueuedError), AdmissionGate(flow, prepared).enter():
            pytest.fail("the Job entered while a run-alone Job held the budget")

        # Queued behind the first holder, then withdrawn when the wait ended.
        assert positions == [0]
        remaining = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert [token.seq for token in remaining] == [first.seq]
    finally:
        store.release(first)


# --- Seat Caps for licensed EDA tools (#3244) -----------------------


def _fpga_prepared(work_dir: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True):
    """An FPGA Job over Target ``a`` (its Vivado Job needs one seat), as Target selection."""
    from booley.flows.fpga.flow import FpgaImplFlow
    from booley.flows.prepared_invocation import TargetSelectionInvocation
    from tests.target_test_support import make_target_handle

    flow = FpgaImplFlow()
    handle = make_target_handle(work_dir, "a", drivable_by=("fpga",))
    flow._state = SimpleNamespace(criteria={})  # type: ignore[assignment]
    flow._args = SimpleNamespace(work_dir=work_dir, target="a")
    monkeypatch.setattr(FpgaImplFlow, "_flow_enabled", lambda _self: enabled)
    selection = SimpleNamespace(handles=(handle,), matches=lambda *_args: True)
    invocation = TargetSelectionInvocation(flow, selection, flow._run)  # type: ignore[arg-type]
    return flow, _prepared_for(invocation)


def _write_jobs(booley_dir: Path, seats: str | None) -> None:
    """Write ``[jobs]`` with the 1,000 MiB Memory Budget, and *seats* as ``[jobs.seats]`` if given."""
    seats_table = "" if seats is None else f"\n[jobs.seats]\n{seats}\n"
    (booley_dir / "booley.toml").write_text(
        f'[jobs]\nmemory_budget = "1000m"\n{seats_table}', encoding="utf-8"
    )


def _hold_vivado_seat(booley_dir: Path, cap: int) -> tuple[job_slots.SlotStore, Any]:
    """A running Job that holds one Vivado seat and 100 MiB, as another process would."""
    import os

    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir))
    pid = os.getpid()
    token = store.acquire(
        job_slots.CLASS_MEMORY,
        pid=pid,
        argv=endpoint_admission._proc_cmdline(pid) or [],
        memory=job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=100.0, seats=(("vivado", cap),)
        ),
    )
    return store, token


def test_fpga_job_queues_for_its_vivado_seat_while_memory_fits(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    _write_jobs(memory_runtime, "vivado = 1")
    _seed_records(memory_runtime, ("a", "fpga_impl", 400))
    flow, prepared = _fpga_prepared(runtime, monkeypatch)
    store, first = _hold_vivado_seat(memory_runtime, cap=1)
    positions: list[int] = []
    waits: list[str] = []

    def narrator(
        _endpoint: object, wait_store: job_slots.SlotStore, pid: int
    ) -> Callable[[int], None]:
        def narrate(position: int) -> None:
            positions.append(position)
            wait = wait_store.memory_wait(pid)
            assert wait is not None
            waits.append(wait.describe())
            raise _QueuedError

        return narrate

    monkeypatch.setattr(endpoint_admission, "_memory_narrator", narrator)

    try:
        # 100 + 500 MiB fit the 1,000 MiB budget: only the seat holds the Job back.
        with pytest.raises(_QueuedError), AdmissionGate(flow, prepared).enter():
            pytest.fail("the Job entered while the only Vivado seat was held")

        assert positions == [0]
        assert waits == [
            "needs 500 MiB; 900 MiB free of the 1,000 MiB Memory Budget; "
            f"waits for a vivado seat (1 of 1 held); largest holders: pid {os.getpid()} 100 MiB"
        ]
        assert [token.seq for token in _held(memory_runtime, job_slots.CLASS_MEMORY)] == [
            first.seq
        ]
    finally:
        store.release(first)


def test_fpga_job_runs_beside_another_vivado_job_under_a_cap_of_two(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_jobs(memory_runtime, "vivado = 2")
    _seed_records(memory_runtime, ("a", "fpga_impl", 400))
    flow, prepared = _fpga_prepared(runtime, monkeypatch)
    store, first = _hold_vivado_seat(memory_runtime, cap=2)

    try:
        with AdmissionGate(flow, prepared).enter():
            holders = _held(memory_runtime, job_slots.CLASS_MEMORY)
            (mine,) = (token for token in holders if token.seq != first.seq)
            assert mine.memory == job_slots.MemoryClaim(
                budget_mb=1000.0, reservation_mb=500.0, wall_s=1.25, seats=(("vivado", 2),)
            )
            assert len(holders) == 2
    finally:
        store.release(first)


def test_fpga_job_without_a_vivado_seat_cap_is_refused_before_admission(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import ExitStack

    _write_jobs(memory_runtime, None)
    flow, prepared = _fpga_prepared(runtime, monkeypatch)

    with pytest.raises(EndpointRejectedError) as refused, ExitStack() as held:
        endpoint_admission._enter_admission(flow, held, prepared)

    assert refused.value.outcome.exit_code == EXIT_ERROR
    assert "[jobs.seats] vivado = <seats you own>" in refused.value.outcome.report_text
    assert refused.value.outcome.report_text.startswith("fpga: ")
    for job_class in job_slots.JOB_CLASSES:
        assert _held(memory_runtime, job_class) == []


def test_fpga_job_with_a_non_positive_vivado_seat_cap_is_refused_before_admission(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import ExitStack

    _write_jobs(memory_runtime, "vivado = 0")
    flow, prepared = _fpga_prepared(runtime, monkeypatch)

    with pytest.raises(EndpointRejectedError) as refused, ExitStack() as held:
        endpoint_admission._enter_admission(flow, held, prepared)

    assert "[jobs.seats].vivado" in refused.value.outcome.report_text
    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


def test_jobs_owing_no_seat_ignore_seat_caps(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _write_jobs(memory_runtime, None)
    _seed_records(memory_runtime, ("a", "fpga_impl", 400))
    fpga, prepared = _fpga_prepared(runtime, monkeypatch, enabled=False)
    with AdmissionGate(fpga, prepared).enter():
        (holder,) = _held(memory_runtime, job_slots.CLASS_MEMORY)
        assert holder.memory == job_slots.MemoryClaim(
            budget_mb=1000.0, reservation_mb=500.0, wall_s=1.25
        )


def _two_target_synth_prepared(flow: AsicSynthesizeFlow, work_dir: Path) -> PreparedExecution:
    """The preparation of *flow* as a Target-selection invocation over Targets ``a`` and ``b``."""
    from booley.flows.prepared_invocation import TargetSelectionInvocation
    from tests.target_test_support import make_target_handle

    handles = tuple(make_target_handle(work_dir, name, drivable_by=("synth",)) for name in "ab")
    flow._state = SimpleNamespace(criteria={})  # type: ignore[assignment]
    flow._args = SimpleNamespace(work_dir=work_dir, target="a,b")
    selection = SimpleNamespace(handles=handles, matches=lambda *_args: True)
    return _prepared_for(TargetSelectionInvocation(flow, selection, flow._run))  # type: ignore[arg-type]


def test_memory_admitted_job_shrinks_when_a_phase_ends(
    memory_runtime: Path, runtime: Path
) -> None:
    from booley.flows.reservation_phases import report_phase_end

    _seed_history(memory_runtime, ("a", 400), ("b", 200))
    flow = AsicSynthesizeFlow()

    with AdmissionGate(flow, _two_target_synth_prepared(flow, runtime)).enter():
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
    from booley.flows.sim.record_invocation import SimulationCampaignInvocation

    _seed_history(memory_runtime, ("demo", 400))
    sized = lambda _self: (JobRequest(flow="synth", target="demo"),)  # noqa: E731
    monkeypatch.setattr(TargetSelectionInvocation, "job_requests", sized)
    monkeypatch.setattr(SimulationCampaignInvocation, "job_requests", sized)
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

    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("[slot]")]
    assert len(lines) == 2
    assert lines[0].startswith("[slot] synth: waiting for memory (position 1): needs 500 MiB")
    assert lines[0].count(REQUEUE_REASON) == 0
    assert lines[1].startswith("[slot] synth: waiting for memory (position 1): waits to run alone")
    assert lines[1].endswith(f"; {REQUEUE_REASON}")


def test_requeue_never_waits_behind_its_own_claim(memory_runtime: Path, runtime: Path) -> None:
    """A deferred release can leave this process's claim behind: refuse, never wait for ourselves."""
    import os

    flow = AsicSynthesizeFlow()
    prepared = _selection_prepared(flow, runtime)
    store = job_slots.SlotStore(job_slots.slots_dir(memory_runtime))
    pid = os.getpid()
    leftover = store.acquire(
        job_slots.CLASS_MEMORY,
        pid=pid,
        argv=endpoint_admission._proc_cmdline(pid) or [],
        memory=job_slots.MemoryClaim(budget_mb=1000.0, run_alone=True),
    )
    try:
        with (
            pytest.raises(EndpointRejectedError, match="still holds its claim") as refused,
            AdmissionGate(flow, prepared, requeued=True).enter(),
        ):
            pytest.fail("the requeued claim queued behind its own process")
        assert refused.value.outcome.oom_killed is True
        assert [token.seq for token in _held(memory_runtime, job_slots.CLASS_MEMORY)] == [
            leftover.seq
        ]
    finally:
        store.release(leftover)


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
    monkeypatch.setattr(job_resources, "cgroup_oom_kill_count", lambda: next(counts))
    marker = runtime / "attempts.txt"
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    flow = harness.flow

    def attempt() -> EndpointOutcome:
        number = len(harness.invocations)
        flow._execute_local(
            [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
        )
        flow._execute_local(
            [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('attempt {number}\\n')"]
        )
        return EndpointOutcome(exit_code=1 if number == 1 else 0)

    harness.behavior = attempt

    harness.execute()

    assert len(harness.invocations) == 2
    assert marker.read_text(encoding="utf-8").splitlines() == ["attempt 2"]


def _goal_bound(harness: _Harness) -> None:
    """Bind *harness*'s Flow to a Goal: its recorder names an acceptance identity."""
    from booley.flows.execution_persistence import StandaloneFlowExecution

    class GoalExecution(StandaloneFlowExecution):
        def acceptance_identity(self) -> dict[str, Any]:
            return {"goal": "g", "criteria_set": "c"}

    harness.adapter = GoalExecution()


def test_goal_bound_job_is_not_requeued_after_an_oom_kill(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The contrast first (a harness patches the Flow class, so one runs at a time).
    standalone = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    assert _attempts_of(standalone, memory_runtime, _killed) == 2

    bound = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    _per_attempt(bound, memory_runtime, _killed)
    _goal_bound(bound)
    result = bound.execute()

    assert len(bound.invocations) == 1
    assert result.exit_code == 1
    assert "oom_requeue" not in result.outcome.detail


def test_unreadable_goal_identity_counts_as_bound(memory_runtime: Path, runtime: Path) -> None:
    from booley.flows.execution_persistence import AcceptanceRecordingError

    class UnreadableIdentity:
        def acceptance_identity(self) -> dict[str, Any]:
            raise AcceptanceRecordingError("the Goal binding cannot be read")

    flow = AsicSynthesizeFlow()
    prepared = _selection_prepared(flow, runtime)

    with AdmissionGate(flow, prepared).enter() as context:
        assert flow.may_requeue_after_oom(prepared, context)
        flow._acceptance_recorder = UnreadableIdentity()  # type: ignore[assignment]
        assert not flow.may_requeue_after_oom(prepared, context)


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

    monkeypatch.setattr(progress_lifecycle, "_replace_if_unchanged", refuse)
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    _per_attempt(harness, memory_runtime, _checkpoint(harness, _killed), EndpointOutcome)

    result = harness.execute()

    assert len(harness.invocations) == 2  # the requeue went ahead
    assert result.exit_code == EXIT_ERROR
    error = result.outcome.detail["completion_error"]
    assert error["operation"] == "retire the killed attempt's progress"
    assert "progress is held" in error["message"]


def test_job_that_is_not_requeued_keeps_its_progress(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.progress_lifecycle import read_progress_for_run

    _progress_run(monkeypatch)
    cases: dict[str, Callable[[], EndpointOutcome]] = {
        # Armed, but it fails without an OOM kill.
        "failed": lambda: EndpointOutcome(exit_code=1),
        # OOM-killed, but the Job still succeeded.
        "killed but passed": lambda: _killed(exit_code=0),
    }
    for name, outcome in cases.items():
        shutil.rmtree(runtime / "reports", ignore_errors=True)
        harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
        _per_attempt(harness, memory_runtime, _checkpoint(harness, outcome))

        harness.execute()

        assert len(harness.invocations) == 1, name
        found = read_progress_for_run((runtime / "reports",), "synth", PROGRESS_RUN_ID)
        assert found is not None, name
        assert found[1]["phase"] == "running", name
        assert found[1]["run_id"] == PROGRESS_RUN_ID, name


def _attempts_of(harness: _Harness, memory_runtime: Path, attempt: Attempt) -> int:
    """How many times the Job's body ran when every attempt behaves as *attempt*."""
    _per_attempt(harness, memory_runtime, attempt)
    harness.execute()
    return len(harness.invocations)


def test_only_a_failed_oom_killed_job_is_requeued(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.endpoint_execution import (
        EXIT_CANCELLED,
        job_stopped_after_oom,
        note_job_oom_kill,
    )

    def failed_without_kill() -> EndpointOutcome:
        return EndpointOutcome(exit_code=1)

    def succeeded_despite_kill() -> EndpointOutcome:
        note_job_oom_kill()  # a killed command in a Job that went on and succeeded
        return EndpointOutcome()

    def cancelled_after_kill() -> EndpointOutcome:
        return _killed(exit_code=EXIT_CANCELLED)

    def cancelled_after_skipped_command() -> EndpointOutcome:
        note_job_oom_kill()
        assert job_stopped_after_oom()  # a command was skipped
        return EndpointOutcome(exit_code=EXIT_CANCELLED)

    not_requeued = (
        failed_without_kill,
        succeeded_despite_kill,
        cancelled_after_kill,
        cancelled_after_skipped_command,
    )
    for attempt in not_requeued:
        harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
        assert _attempts_of(harness, memory_runtime, attempt) == 1, attempt.__name__

    # Under a parent's claim the Job holds none of its own: nothing to requeue.
    monkeypatch.setenv(job_slots.INHERITED_SLOT_ENV, "light/1")
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    harness.behavior = _killed
    harness.execute()
    assert len(harness.invocations) == 1
    monkeypatch.delenv(job_slots.INHERITED_SLOT_ENV)

    # The contrast: an own OOM kill that fails the Job is requeued.
    harness = _requeue_harness("synth", memory_runtime, runtime, monkeypatch)
    assert _attempts_of(harness, memory_runtime, _killed) == 2


# --- Verilator -j from the container's cores and the running Jobs (#3246) ------------


@contextmanager
def _running_memory_jobs(booley_dir: Path, count: int) -> Iterator[None]:
    """Hold *count* other Jobs' Memory Reservations (50 MiB each) in the memory Job Class."""
    import os

    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir))
    pid = os.getpid()
    tokens = [
        store.acquire(
            job_slots.CLASS_MEMORY,
            pid=pid,
            argv=endpoint_admission._proc_cmdline(pid) or [],
            memory=job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=50.0),
        )
        for _ in range(count)
    ]
    try:
        yield
    finally:
        for token in tokens:
            store.release(token)


@pytest.fixture
def eight_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """8 available cores, as admission and the build's ``-j`` read them."""
    import importlib

    # Admission reads the cores from cpu_budget; the build from build_parallelism.
    # Resolved by name so a tree without cpu_budget still reaches its assertions.
    for name in ("booley.runtime.cpu_budget", "booley.flows.sim.build_parallelism"):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        monkeypatch.setattr(module, "_affinity_cpu_count", lambda: 8)
        monkeypatch.setattr(module, "_read_text", lambda _path: None)


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
def test_memory_admitted_verilator_build_shares_cores_with_one_running_job(
    memory_runtime: Path, runtime: Path
) -> None:
    from booley.flows.sim.build_parallelism import verilator_backend_arguments

    _seed_history(memory_runtime, ("a", 40))
    flow = AsicSynthesizeFlow()
    with (
        _running_memory_jobs(memory_runtime, 1),
        AdmissionGate(flow, _selection_prepared(flow, runtime)).enter(),
    ):
        inspection = _verilator_inspection()
        assert verilator_backend_arguments(inspection) == ("-j4", "VM_PARALLEL_BUILDS=1")
        # The count is fixed at the Job's start: a Job that arrives later changes nothing.
        with _running_memory_jobs(memory_runtime, 1):
            assert verilator_backend_arguments(inspection) == ("-j4", "VM_PARALLEL_BUILDS=1")


@pytest.mark.usefixtures("eight_cores")
def test_memory_admitted_verilator_build_alone_and_among_ten_jobs(
    memory_runtime: Path, runtime: Path
) -> None:
    # Other running Jobs -> the -j this Job gets: 8 // (others + 1), rounded down.
    expected = {0: "-j8", 2: "-j2", 9: "-j1"}
    for others, jobs_option in expected.items():
        with _running_memory_jobs(memory_runtime, others):
            (arguments,) = _verilator_arguments_in_admitted_job(
                memory_runtime, runtime, _verilator_inspection()
            )
        assert arguments == (jobs_option, "VM_PARALLEL_BUILDS=1"), f"{others} other Jobs"


@pytest.mark.usefixtures("eight_cores")
def test_memory_admitted_job_keeps_a_project_set_thread_count(
    memory_runtime: Path, runtime: Path
) -> None:
    with _running_memory_jobs(memory_runtime, 1):
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
