"""One Flow-neutral admission path for every endpoint.

The same admission outcomes (queue full, withdrawn queued run, invalid runtime
identity, no-slot cases, release at run end) must hold for Simulation and for a
non-Simulation Flow, and must not depend on the endpoint's name.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.config.jobs import SlotCaps
from booley.flows import endpoint_admission
from booley.flows.endpoint_admission import (
    AdmissionContext,
    AdmissionGate,
    AdmissionIdentityError,
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

FLOW_TYPES = {"sim": SimulateFlow, "synth": AsicSynthesizeFlow}
MAX_HEAVY = 3


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
        self.caps = SlotCaps(max_heavy=MAX_HEAVY)
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
    """Simulation and a heavy non-Simulation Flow, reduced to their invocation body.

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
            return run()

        monkeypatch.setattr(flow, "run_prepared_simulation", run_prepared_simulation)

        def prepare() -> None:
            from booley.flows.sim.campaign_invocation import SimulationCampaignInvocation
            from booley.flows.sim.flow import PreparedSimulationEndpoint
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
        self: EndpointState, prepared: PreparedExecution
    ) -> AbstractContextManager[AdmissionContext | None]:
        @contextmanager
        def record() -> Iterator[AdmissionContext | None]:
            with original_admission(self, prepared) as borrowed:
                observed.admissions.append(borrowed)
                yield borrowed

        return record()

    monkeypatch.setattr(EndpointState, "admission", recording_admission)
    return observed


def _use_slot(monkeypatch: pytest.MonkeyPatch, store: _FakeStore, claim: object) -> None:
    monkeypatch.setattr(EndpointState, "_acquire_job_slot", lambda _self: (store, claim))


def _refuse_slot(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    def refuse(_self: EndpointState) -> object:
        raise failure

    monkeypatch.setattr(EndpointState, "_acquire_job_slot", refuse)


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
    assert borrowed.max_heavy == MAX_HEAVY


def test_full_queue_reports_blocked(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_slot(monkeypatch, job_slots.QueueFullError("heavy queue is full (2 waiting)"))

    result = harness.execute()

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == (
        "BLOCKED: heavy queue is full (2 waiting). Retry when queued work drains."
    )
    assert harness.invocations == []


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
    from booley.flows.sim.campaign_invocation import SimulationCampaignInvocation
    from booley.flows.sim.flow import PreparedSimulationEndpoint

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
    endpoint = _StubEndpoint("custom", None if case == "unclassed" else job_slots.CLASS_HEAVY)
    if case == "inherited":
        monkeypatch.setenv(job_slots.INHERITED_SLOT_ENV, "heavy/slot")
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
        assert borrowed.max_heavy == 1
    assert endpoint.acquisitions == 1


@pytest.mark.usefixtures("runtime")
@pytest.mark.parametrize("name", ("sim", "other"))
def test_admission_ignores_endpoint_name(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Simulation payload on a differently named endpoint is admitted identically."""
    endpoint = _StubEndpoint(name, job_slots.CLASS_HEAVY)
    store, claim = _FakeStore(), object()
    monkeypatch.setattr(endpoint, "_acquire_job_slot", lambda: (store, claim))
    prepared = _prepared(borrowing=True)

    with endpoint_admission.admission(endpoint, prepared) as borrowed:  # type: ignore[arg-type]
        assert isinstance(borrowed, AdmissionContext)
        assert (borrowed.mode, borrowed.outer_token, borrowed.max_heavy) == (
            "managed",
            claim,
            MAX_HEAVY,
        )
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
    endpoint = _StubEndpoint("custom", job_slots.CLASS_HEAVY)
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
        job_slots.QueueFullError("full"),
        job_slots.ClaimLostError("vanished"),
        AdmissionIdentityError("mismatch"),
    ),
    ids=("queue_full", "claim_lost", "identity"),
)
def test_body_failures_are_not_remapped_to_admission_rejections(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only entering admission is mapped; the invocation body's errors pass through."""
    endpoint = _StubEndpoint("sim", job_slots.CLASS_HEAVY)
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


# --- memory admission behind BOOLEY_DEV_MEMORY_ADMISSION (#3701) ---------------------


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
def memory_runtime(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The runtime with the switch set, a real slots root and a 1,000 MiB Memory Budget."""
    booley_dir = runtime / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text('[jobs]\nmemory_budget = "1000m"\n', encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DEV_MEMORY_ADMISSION", "1")
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
    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir), SlotCaps(max_heavy=MAX_HEAVY))
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
        assert _held(memory_runtime, job_slots.CLASS_HEAVY) == []
        assert context.mode == "managed"
        assert context.memory_warning is None
        # A memory-admitted Job has one lane, whatever the heavy cap says.
        assert context.max_heavy == 1

    assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


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
    from booley.flows.sim.campaign_invocation import SimulationCampaignInvocation

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
    # Switch unset: a heavy slot.
    monkeypatch.delenv(job_slots.INHERITED_SLOT_ENV)
    monkeypatch.delenv("BOOLEY_DEV_MEMORY_ADMISSION")
    with AdmissionGate(AsicSynthesizeFlow()).enter() as context:
        assert context.memory_sizing is None
        assert len(_held(memory_runtime, job_slots.CLASS_HEAVY)) == 1


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
        job_slots.CLASS_HEAVY,
        "heavy slot",
    )

    narrate(0)

    assert capsys.readouterr().err == (
        "[slot] sim: waiting for heavy slot (position 1); held by pid 7 (heavy)\n"
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


# --- lint and Custom Flow Jobs under the switch (#3703) ------------------------------


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
        assert _held(memory_runtime, job_slots.CLASS_HEAVY) == []
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
            assert _held(memory_runtime, job_slots.CLASS_HEAVY) == [], target

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
    store = job_slots.SlotStore(job_slots.slots_dir(memory_runtime), SlotCaps(max_heavy=MAX_HEAVY))
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


def test_switch_unset_leaves_lint_and_custom_flow_jobs_unadmitted(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for kind in ("lint", "custom"):
        _assert_unadmitted_without_switch(kind, memory_runtime, runtime, monkeypatch)


def _assert_unadmitted_without_switch(
    kind: str, memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A *kind* Job takes no admission without the switch, and a Reservation with it."""
    flow, prepared = _admitted_job(kind, runtime, memory_runtime)
    monkeypatch.delenv("BOOLEY_DEV_MEMORY_ADMISSION")

    with AdmissionGate(flow, prepared).enter() as context:
        assert context.mode == "unmanaged"
        assert context.outer_token is None
        for job_class in job_slots.JOB_CLASSES:
            assert _held(memory_runtime, job_class) == []

    # The contrast: the same Job under the switch holds a Memory Reservation.
    monkeypatch.setenv("BOOLEY_DEV_MEMORY_ADMISSION", "1")
    flow, prepared = _admitted_job(kind, runtime, memory_runtime)
    with AdmissionGate(flow, prepared).enter():
        assert len(_held(memory_runtime, job_slots.CLASS_MEMORY)) == 1


# --- Seat Caps for licensed EDA tools under the switch (#3244) -----------------------


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

    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir), SlotCaps(max_heavy=MAX_HEAVY))
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


def test_switch_unset_fpga_job_takes_a_heavy_slot_without_a_seat_cap(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_jobs(memory_runtime, None)
    monkeypatch.delenv("BOOLEY_DEV_MEMORY_ADMISSION")
    flow, prepared = _fpga_prepared(runtime, monkeypatch)

    with AdmissionGate(flow, prepared).enter() as context:
        assert context.mode == "managed"
        assert len(_held(memory_runtime, job_slots.CLASS_HEAVY)) == 1
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


def test_switch_unset_phase_ends_change_no_claim(
    memory_runtime: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the switch the real Target-end report runs inside the gate and changes nothing."""
    from booley.flows import reservation_phases

    _seed_history(memory_runtime, ("a", 400), ("b", 200))
    monkeypatch.delenv("BOOLEY_DEV_MEMORY_ADMISSION")
    flow = AsicSynthesizeFlow()
    monkeypatch.setattr(flow, "_synthesize_target", lambda _target: (None, ""))

    with AdmissionGate(flow, _two_target_synth_prepared(flow, runtime)).enter():
        # Heavy Jobs still take a heavy slot; the memory class is untouched.
        (heavy,) = _held(memory_runtime, job_slots.CLASS_HEAVY)
        before = heavy.path.read_bytes()
        flow._run_single_config("a")
        assert reservation_phases._active.get() is None
        assert heavy.path.read_bytes() == before
        assert _held(memory_runtime, job_slots.CLASS_MEMORY) == []


# --- Verilator -j from the container's cores and the running Jobs (#3246) ------------


@contextmanager
def _running_memory_jobs(booley_dir: Path, count: int) -> Iterator[None]:
    """Hold *count* other Jobs' Memory Reservations (50 MiB each) in the memory Job Class."""
    import os

    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir), SlotCaps(max_heavy=MAX_HEAVY))
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
    """8 available cores, and the old per-lane rule pinned to ``-j1`` (1 GiB heavy lane)."""
    import importlib

    from booley.flows.sim import build_parallelism

    # Admission reads the cores from cpu_budget; the lane rule from build_parallelism.
    # Resolved by name so a tree without cpu_budget still reaches its assertions.
    for name in ("booley.runtime.cpu_budget", "booley.flows.sim.build_parallelism"):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        monkeypatch.setattr(module, "_affinity_cpu_count", lambda: 8)
        monkeypatch.setattr(module, "_read_text", lambda _path: None)
    monkeypatch.setattr(
        build_parallelism,
        "_load_common_job_budget",
        lambda _root: {"jobs": {"heavy_memory": "1g"}},
    )


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
