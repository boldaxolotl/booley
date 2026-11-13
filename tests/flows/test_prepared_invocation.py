"""Each prepared invocation kind drives the endpoint lifecycle through public ``execute``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from booley.flows.endpoint_session import PreparedExecution
from booley.flows.endpoint_state import EndpointState
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.flows.report_headline import assert_paired_headline
from tests.oom_priority_support import FakeProc

# The Criterion key each kind judges, planned before the run and reported after it.
PLANNED_KEYS = {
    "ordinary": "echo_ok",
    "target_selection": "lint_clean_chosen",
    "campaign": "sim_pass_chosen",
}


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A session runtime with no ambient state, identity or log variables."""
    from booley.runtime import runtime_context

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    for name in (
        "BOOLEY_STATE_FILE",
        "BOOLEY_TICKET_FILE",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_RUNTIME_DIR",
        "BOOLEY_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path


def _bind_strict_state(runtime: Path, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Write a strict state whose only Criterion is *key* and point the run at it."""
    from booley.criteria.state import DevelopmentState

    path = runtime / "state.json"
    state = DevelopmentState.load(path)
    state.init_criteria({key: True}, strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(runtime / "logs"))


def _ordinary(runtime: Path) -> tuple[Any, Any]:
    """A Custom Flow: preparation hands back its own run."""
    import argparse

    from booley.flows.base import BooleyFlow

    class EchoOkFlow(BooleyFlow):
        name = "echo_ok_flow"
        description = "Custom Flow judging echo_ok"
        satisfies = ["echo_ok"]  # noqa: RUF012 -- mirrors the declared ClassVar shape

        def _add_args(self, parser: argparse.ArgumentParser) -> None:
            """This Flow takes no options of its own."""

        def _run(self) -> EndpointOutcome:
            self.set_criterion("echo_ok", True)
            return EndpointOutcome()

    return EchoOkFlow(), [
        "-C",
        str(runtime),
        "--target",
        "echo",
        "--report-dir",
        str(runtime / "reports"),
    ]


def _target_selection(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """Lint over a Target selected automatically from a real core."""
    from tests.flows.test_target_omission import write_targets

    write_targets(runtime, "lint")

    def run(self: LintFlow) -> EndpointOutcome:
        self.set_criterion("lint_clean_chosen", True)
        return EndpointOutcome()

    monkeypatch.setattr(LintFlow, "_run", run)
    return LintFlow(), LintFlow.request_type(
        target="", work_dir=runtime, report_dir=runtime / "reports"
    )


def _campaign(
    runtime: Path, monkeypatch: pytest.MonkeyPatch, admissions: list[object]
) -> tuple[Any, Any]:
    """Simulation whose Campaign preflight is prepared and run through class-level hooks."""
    from booley.flows.sim.flow import PreparedSimulationEndpoint
    from tests.flows.test_target_omission import write_targets
    from tests.target_test_support import make_target_handle

    # A real core: the generic Target preparation validates the requested Target.
    write_targets(runtime, "sim")

    handle = make_target_handle(runtime, "chosen")

    def prepare(self: SimulateFlow) -> PreparedSimulationEndpoint:
        return PreparedSimulationEndpoint((handle,), None, ("chosen",), {"chosen": ["smoke"]})

    def run_prepared(
        self: SimulateFlow, prepared: PreparedSimulationEndpoint, admission: object | None
    ) -> EndpointOutcome:
        admissions.append(admission)
        self.set_criterion("sim_pass_chosen", True)
        return EndpointOutcome()

    monkeypatch.setattr(SimulateFlow, "prepare_simulation_endpoint", prepare)
    monkeypatch.setattr(SimulateFlow, "run_prepared_simulation", run_prepared)
    return SimulateFlow(), SimulateFlow.request_type(
        target="chosen", work_dir=runtime, report_dir=runtime / "reports"
    )


def _install_acceptance_owner(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Install an external acceptance owner with identity; return the keys it records.

    Mirrors ``external_acceptance_owner`` in test_transport_contract. A Custom Flow
    configures its owner when constructed, so call this before building any Flow.
    """
    from tests.criteria.ledger_support import FixtureRecorder

    recorded: list[str] = []

    class KeyRecorder(FixtureRecorder):
        def record_changes(self, state: Any, changes: Any, **kwargs: Any) -> Any:
            recorded.extend(change.key for change in changes)
            return super().record_changes(state, changes, **kwargs)

    recorder = KeyRecorder(
        log_dir=runtime / "logs", identity={"generation": "d" * 32, "authored_sha256": "e" * 64}
    )
    configure = EndpointState.configure_flow_execution

    def configure_external(endpoint: EndpointState, adapter: Any) -> None:
        configure(endpoint, adapter)
        endpoint._acceptance_recorder = recorder

    monkeypatch.setattr(EndpointState, "configure_flow_execution", configure_external)
    return recorded


def _spy_lifecycle(monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], list[object]]:
    """Return the invocations handed to ``invoke_endpoint`` and the admissions entered."""
    invocations: list[object] = []
    entered: list[object] = []
    original_invoke, original_admission = EndpointState.invoke_endpoint, EndpointState.admission

    def spy_invoke(self: EndpointState, prepared: PreparedExecution, **kwargs: Any) -> Any:
        invocations.append(prepared.invocation)
        return original_invoke(self, prepared, **kwargs)

    @contextmanager
    def record_admission(
        self: EndpointState, prepared: PreparedExecution, *, requeued: bool = False
    ) -> Iterator[Any]:
        with original_admission(self, prepared, requeued=requeued) as borrowed:
            entered.append(borrowed)
            yield borrowed

    monkeypatch.setattr(EndpointState, "invoke_endpoint", spy_invoke)
    monkeypatch.setattr(EndpointState, "admission", record_admission)
    return invocations, entered


@pytest.mark.parametrize("kind", ["ordinary", "target_selection", "campaign"])
def test_prepared_invocation_kind_drives_lifecycle(
    kind: str, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.prepared_invocation import OrdinaryRunInvocation, TargetSelectionInvocation
    from booley.flows.sim.record_invocation import SimulationCampaignInvocation

    expected_type = {
        "ordinary": OrdinaryRunInvocation,
        "target_selection": TargetSelectionInvocation,
        "campaign": SimulationCampaignInvocation,
    }[kind]
    key = PLANNED_KEYS[kind]
    _bind_strict_state(runtime, monkeypatch, key)
    recorded = _install_acceptance_owner(runtime, monkeypatch)

    campaign_admissions: list[object] = []
    if kind == "ordinary":
        flow, request = _ordinary(runtime)
    elif kind == "target_selection":
        flow, request = _target_selection(runtime, monkeypatch)
    else:
        flow, request = _campaign(runtime, monkeypatch, campaign_admissions)
    invocations, entered = _spy_lifecycle(monkeypatch)

    # A Custom Flow has only the CLI entry; the built-in kinds take their typed request.
    result = flow.execute_cli(request) if kind == "ordinary" else flow.execute(request)

    assert result.exit_code == 0, result.outcome.report_text
    (invocation,) = invocations
    assert type(invocation) is expected_type
    # Only the campaign borrows the admission context the run entered once.
    assert invocation.borrows_admission is (kind == "campaign")
    assert len(entered) == 1
    if kind == "campaign":
        assert campaign_admissions == entered
        assert campaign_admissions[0] is not None
    assert recorded == [key]
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == (key, True)
    assert_paired_headline(runtime / "reports", flow.name, (key, True))


def test_lint_and_custom_flow_hold_a_memory_reservation_through_the_run(runtime: Path) -> None:
    for kind in ("ordinary", "target_selection"):
        # Each case gets its own patches, Project and state.
        with pytest.MonkeyPatch.context() as patch:
            case_dir = runtime / kind
            case_dir.mkdir()
            patch.chdir(case_dir)
            patch.setenv("BOOLEY_RUNTIME_DIR", str(case_dir / "runtime"))
            _assert_holds_reservation_through_run(kind, case_dir, patch)


def _assert_holds_reservation_through_run(
    kind: str, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Custom Flow and lint hold one Memory Reservation while they run."""
    from booley.flows import endpoint_admission
    from booley.runtime import job_slots

    booley_dir = runtime / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text('[jobs]\nmemory_budget = "1000m"\n', encoding="utf-8")
    # A fake /proc with no process: ranking never touches the worker's real OOM priority.
    monkeypatch.setattr("booley.runtime.oom_priority.PROC", FakeProc())
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    monkeypatch.setattr(endpoint_admission, "resolve_common_booley_dir", lambda: booley_dir)
    key = PLANNED_KEYS[kind]
    _bind_strict_state(runtime, monkeypatch, key)
    _install_acceptance_owner(runtime, monkeypatch)
    if kind == "ordinary":
        flow, request = _ordinary(runtime)
    else:
        flow, request = _target_selection(runtime, monkeypatch)

    store = job_slots.SlotStore(job_slots.slots_dir(booley_dir))
    held_during_run: list[int] = []
    flow_type = type(flow)
    original_run = flow_type._run

    def observing_run(self: Any) -> EndpointOutcome:
        held_during_run.append(len(store.observe(job_slots.CLASS_MEMORY)[0]))
        return original_run(self)

    monkeypatch.setattr(flow_type, "_run", observing_run)

    result = flow.execute_cli(request) if kind == "ordinary" else flow.execute(request)

    assert result.exit_code == 0, result.outcome.report_text
    assert held_during_run == [1]
    assert store.observe(job_slots.CLASS_MEMORY)[0] == []
