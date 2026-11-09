"""One Flow-neutral admission path for every endpoint.

The same admission outcomes (queue full, withdrawn queued run, invalid runtime
identity, no-slot cases, release at run end) must hold for Simulation and for a
non-Simulation Flow, and must not depend on the endpoint's name.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from booley.config.jobs import SlotCaps
from booley.flows import endpoint_admission
from booley.flows.endpoint_admission import AdmissionContext, AdmissionIdentityError
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
    flow = FLOW_TYPES[request.param]()
    typed_request = flow.request_type(
        target="demo", work_dir=runtime, report_dir=runtime / "reports"
    )
    observed = _Harness(flow=flow, request=typed_request)

    def run() -> EndpointOutcome:
        observed.invocations.append(object())
        return observed.behavior()

    if request.param == "sim":

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
