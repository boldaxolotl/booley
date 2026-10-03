"""Public lifecycle contract for prepared Simulation Campaign execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows import endpoint_session
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.sim.flow import SimulateFlow
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult, execute_endpoint


class _Flow:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def run_prepared_simulation(
        self, prepared: object, admission: object | None
    ) -> EndpointOutcome:
        self.events.append("invoke")
        assert prepared == ("target",)
        assert admission == "borrowed-admission"
        return EndpointOutcome(report_text="ok")


class _Endpoint:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.flow = _Flow(self.events)
        self.name = "sim"

    @contextmanager
    def admission(self, prepared: PreparedExecution) -> Iterator[object]:
        assert prepared.simulation == ("target",)
        self.events.append("admit")
        try:
            yield "borrowed-admission"
        finally:
            self.events.append("release")

    def invoke_endpoint(
        self,
        prepared: PreparedExecution,
        *,
        admission: object | None,
        started: float,
    ) -> EndpointOutcome:
        return endpoint_session.invoke_endpoint(
            self,
            prepared,
            admission=admission,
            started=started,  # type: ignore[arg-type]
        )

    def _adapt_outcome(self, outcome: EndpointOutcome) -> EndpointOutcome:
        return outcome

    def _finalize_result(self, outcome: EndpointOutcome) -> None:
        self.events.append("finalize")

    def record_acceptance(self, prepared: PreparedExecution, outcome: EndpointOutcome) -> None:
        self.events.append("acceptance")

    def finish_execution(
        self,
        prepared: PreparedExecution,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        acceptance_recorded: bool,
    ) -> ExecutionResult:
        self.events.append("finish")
        return ExecutionResult(outcome.exit_code, outcome)


def test_prepared_campaign_and_borrowed_admission_are_explicit_values() -> None:
    endpoint = _Endpoint()
    prepared = PreparedExecution(None, None, False, False, simulation=("target",))

    result = execute_endpoint(endpoint, prepared)

    assert result.outcome.report_text == "ok"
    assert endpoint.events == [
        "admit",
        "invoke",
        "finalize",
        "acceptance",
        "finish",
        "release",
    ]
    assert not hasattr(endpoint, "_simulation_prepared")
    assert not hasattr(endpoint, "_simulation_admission_context")


def _request(selector: str) -> SimpleNamespace:
    return SimpleNamespace(
        plan=SimpleNamespace(
            manifest=SimpleNamespace(
                document={
                    "target": {
                        "selector": selector,
                        "vlnv": "acme:demo:target:1.0",
                        "name": selector,
                    }
                }
            )
        )
    )


def _retained_campaign_outcome(outcomes: list[object]) -> EndpointOutcome:
    targets = {f"target-{index}": {"simulation": "pass"} for index, _ in enumerate(outcomes)}
    return EndpointOutcome(detail={"targets": targets}, report_text="campaign verdict")


def test_completed_campaign_survives_per_target_progress_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    outcomes = [object(), object()]
    campaign = SimpleNamespace(run=lambda _request: outcomes.pop(0))
    progress = SimpleNamespace(
        invocation_dir=tmp_path,
        checkpoint=lambda **_kwargs: (_ for _ in ()).throw(OSError("progress unavailable")),
    )
    monkeypatch.setattr(flow, "_campaign_endpoint_outcome", _retained_campaign_outcome)
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda _progress, _outcome: None,
    )

    result = flow._execute_coverage_campaign_requests(
        campaign, [_request("first"), _request("second")], progress
    )

    assert result.exit_code == 2
    assert result.detail["targets"] == {"target-0": {"simulation": "pass"}}
    assert result.detail["pending_targets"] == ["second"]
    assert result.detail["completion_error"]["path"] == str(tmp_path / "progress.json")
    assert len(outcomes) == 1


def test_coverage_authentication_failure_returns_structured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    flow._coverage_prepared = SimpleNamespace(targets=[])
    completed = [object(), object()]
    pending = list(completed)
    campaign = SimpleNamespace(run=lambda _request: pending.pop(0))
    progress = SimpleNamespace(invocation_dir=tmp_path, checkpoint=lambda **_kwargs: None)
    monkeypatch.setattr(flow, "_campaign_endpoint_outcome", _retained_campaign_outcome)
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda _progress, _outcome: (_ for _ in ()).throw(ValueError("untrusted campaign")),
    )

    result = flow._execute_coverage_campaign_requests(
        campaign, [_request("first"), _request("second")], progress
    )

    assert result.exit_code == 2
    assert result.detail["campaigns"] == {}
    assert result.detail["targets"] == {}
    assert "untrusted campaign" in result.report_text
    assert len(pending) == 1


def test_all_campaigns_survive_terminal_progress_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path)
    flow._coverage_prepared = SimpleNamespace(
        targets=[
            SimpleNamespace(handle=SimpleNamespace(selector="first")),
            SimpleNamespace(handle=SimpleNamespace(selector="second")),
        ]
    )
    returned = [object(), object()]
    pending = list(returned)
    campaign = SimpleNamespace(run=lambda _request: pending.pop(0))
    requests = [_request("first"), _request("second")]

    progress = SimpleNamespace(invocation_dir=tmp_path, targets=("first", "second"), outcomes=[])
    terminal_phases: list[str] = []

    def checkpoint(*, complete: bool = False, phase: str | None = None) -> None:
        if complete:
            assert phase is not None
            terminal_phases.append(phase)
            raise OSError("terminal progress unavailable")

    progress.checkpoint = checkpoint
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: tmp_path)
    monkeypatch.setattr(
        flow,
        "_coverage_campaign_session",
        lambda _admission, _invocation: (campaign, requests),
    )
    monkeypatch.setattr("booley.flows.sim.flow.CoverageProgress", lambda *_args: progress)
    monkeypatch.setattr(flow, "_campaign_endpoint_outcome", _retained_campaign_outcome)
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda retained, outcome: retained.outcomes.append(outcome),
    )

    result = flow._run_coverage_campaigns(
        AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False)
    )

    assert result.exit_code == 2
    assert result.detail["targets"] == {
        "target-0": {"simulation": "pass"},
        "target-1": {"simulation": "pass"},
    }
    assert result.detail["completion_error"]["operation"] == "publish coverage progress"
    assert terminal_phases == ["complete", "aborted"]
    assert pending == []


def test_legacy_result_survives_terminal_progress_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = tmp_path
    monkeypatch.setattr(flow, "_prepare_legacy_run", lambda *_args: (["PASS"], True))
    monkeypatch.setattr(
        flow,
        "_legacy_endpoint_outcome",
        lambda *_args: EndpointOutcome(detail={"targets": {"first": {"simulation": "pass"}}}),
    )
    monkeypatch.setattr(
        flow,
        "_write_progress_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("progress unavailable")),
    )

    result = flow._run_legacy_selected_mode(["first"], {}, 0.0, 0.0)

    assert result.exit_code == 2
    assert result.detail["targets"] == {"first": {"simulation": "pass"}}
    assert result.detail["completion_error"]["operation"] == "publish simulation progress"
    assert result.detail["completion_error"]["path"] == str(tmp_path / "progress.json")


@pytest.mark.parametrize("termination", ["disk_budget", "sim_time_stall"])
def test_infrastructure_campaign_reaches_persisted_endpoint_reports(
    tmp_path: Path, termination: str
) -> None:
    import json
    import time

    from booley.flows.endpoint_report_criteria import ReportCriteria
    from booley.flows.endpoint_reporting import write_report
    from booley.flows.sim.campaign.coordinator import (
        CampaignPolicy,
        NewCampaignRunRequest,
        SimulationCampaign,
    )
    from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
    from tests.flows.sim.test_campaign_characterization import (
        _AdapterAbortExecution,
        _plan,
        _unmanaged,
    )

    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    execution = _AdapterAbortExecution(eda_tool="verilator", cocotb=True)
    execution.termination = termination
    execution.failure_kind = "infrastructure"
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,
        execution_factory=lambda _options: execution,
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    campaign = SimulationCampaign(executor).run(
        NewCampaignRunRequest(
            plan,
            tmp_path,
            invocation.parent,
            CampaignPolicy(),
            invocation,
            _unmanaged(),
        )
    )
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._campaign_endpoint_outcome([campaign])
    endpoint = SimpleNamespace(
        args=SimpleNamespace(report_dir=invocation.parent, slug=""),
        flow=flow,
        name="sim",
        endpoint_kind="flow",
        _report_criteria=ReportCriteria(frozen=True),
        _reserved_invocation_dir=invocation,
        _start_time=time.monotonic(),
        _selected_target="sim",
        _eda_tool="verilator",
        _raw_argv=None,
    )
    assert write_report(endpoint, result) == invocation / "report.json"
    assert result.exit_code == 2
    for path in (invocation / "report.json", invocation.parent / "sim.json"):
        report = json.loads(path.read_bytes())
        detail = report["detail"]["campaigns"]["sim"]
        assert report["exit_code"] == 2
        assert detail["complete"] is True
        assert detail["grade"] == "error"
        assert detail["termination_counts"][termination] > 0
        assert detail["observations"][0]["failure_class"] == "infrastructure"
        assert detail["observations"][0]["detail"]["termination"] == termination


def test_campaign_recording_failure_retains_evaluated_final_report(tmp_path):
    import json

    from booley.flows.sim.acceptance import AcceptanceContext, SimulationAcceptanceCoordinator
    from tests.flows.sim.test_campaign_phase2 import _observation, _simulation_outcome
    from tests.mcp_tools.test_base import ConcreteMcpTool

    tool = ConcreteMcpTool()
    tool.endpoint_kind = "specialist"
    tool.parse_args(["--work-dir", str(tmp_path), "--report-dir", str(tmp_path / "reports")])
    tool.read_state()
    tool.state._file_path = tmp_path / "state.json"
    tool.state.init_criteria({"sim_pass_sim": True}, strict=True)

    class FailedRecorder:
        def record_or_verify_transaction(self, *_args, **_kwargs):
            raise OSError("campaign ledger unavailable")

    outcome = _simulation_outcome(tmp_path, [_observation("smoke")], required=("smoke",))
    context = AcceptanceContext(
        "ticket",
        {},
        "generation",
        tool.state,
        FailedRecorder(),
        "invocation",
        observer=tool.record_report_criteria,
    )
    with pytest.raises(OSError):
        SimulationAcceptanceCoordinator().reconcile(outcome, context)
    from booley.flows.endpoint_report_criteria import freeze

    freeze(tool)
    report = json.loads(tool.write_report(EndpointOutcome(exit_code=2)).read_text())
    assert (report["criterion_key"], report["criterion_met"]) == ("sim_pass_sim", True)
    assert report["passed"] is False
    assert tool.state.criteria["sim_pass_sim"].met is False


@pytest.mark.parametrize("conclusive", [True, False])
def test_standalone_campaign_has_only_current_conclusive_workload_verdict(tmp_path, conclusive):
    import json
    from dataclasses import replace

    from booley.flows.sim.acceptance import _record_standalone_campaign
    from tests.flows.sim.test_campaign_phase2 import _observation, _simulation_outcome
    from tests.mcp_tools.test_base import ConcreteMcpTool

    tool = ConcreteMcpTool()
    tool.endpoint_kind = "specialist"
    tool.parse_args(["--work-dir", str(tmp_path), "--report-dir", str(tmp_path / "reports")])
    tool.read_state()
    outcome = _simulation_outcome(tmp_path, [_observation("smoke")], required=("smoke",))
    if not conclusive:
        outcome = replace(outcome, complete=False)
    _record_standalone_campaign(tool, outcome)
    from booley.flows.endpoint_report_criteria import freeze

    freeze(tool)
    report = json.loads(tool.write_report(EndpointOutcome()).read_text())
    assert (report["criterion_key"], report["criterion_met"]) == (
        ("sim_pass_sim", True) if conclusive else ("", None)
    )
    assert tool.state._file_path is None
    assert not (tmp_path / "state.json").exists()


def _public_cycle_campaign(tmp_path, monkeypatch):
    from booley.flows.sim.campaign.coordinator import (
        CampaignPolicy,
        NewCampaignRunRequest,
        SimulationCampaign,
    )
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan
    from tests.flows.sim.test_campaign_phase3 import _admission, _named_manifest, _shared_executor

    project = tmp_path / "project"
    project.mkdir()
    build = tmp_path / "build"
    build.mkdir()
    (build / "simv").write_bytes(b"image")
    (build / "input.bin").write_bytes(b"pristine")
    log = build / "run.log"
    log.write_text("PASS")
    handle = SimpleNamespace(
        identity="acme:lib:dut:1#sim", project_root=project, selector="sim", eda_tool="icarus"
    )
    executor = _shared_executor(build, log, handle, [], [0], [])
    invocation = tmp_path / "reports/sim/1"
    invocation.mkdir(parents=True)
    plan = create_simulation_campaign_plan(_named_manifest(("smoke",)))
    with monkeypatch.context() as local:
        local.setattr(
            "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
            lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
        )
        return SimulationCampaign(executor).run(
            NewCampaignRunRequest(
                plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
            )
        )


@pytest.mark.parametrize("fault", ["none", "metadata", "acceptance"])
def test_public_simulation_publishes_cycle_counts_after_acceptance(tmp_path, monkeypatch, fault):
    import json
    from contextlib import nullcontext

    from booley.flows.flow_session import FlowSession

    campaign = _public_cycle_campaign(tmp_path, monkeypatch)
    flow = SimulateFlow()
    monkeypatch.setattr(flow, "_pre_state_gate", lambda: None)
    monkeypatch.setattr(flow, "prepare_target_endpoint", lambda: None)
    monkeypatch.setattr(flow, "prepare_simulation_endpoint", object)
    monkeypatch.setattr(flow, "_resolve_display_label", lambda: None)
    monkeypatch.setattr(FlowSession, "admission", lambda *_args: nullcontext(None))
    monkeypatch.setattr(
        flow, "run_prepared_simulation", lambda *_args: flow._campaign_endpoint_outcome([campaign])
    )
    if fault == "metadata":
        monkeypatch.setattr(
            flow,
            "persisted_cycle_counts",
            lambda: (_ for _ in ()).throw(RuntimeError("metadata failed")),
        )
    if fault == "acceptance":
        monkeypatch.setattr(
            flow,
            "record_campaign_acceptance",
            lambda _outcomes: (_ for _ in ()).throw(RuntimeError("acceptance failed")),
        )
    reports = tmp_path / "reports"
    execution = flow.execute_cli(
        ["--target", "sim", "--work-dir", str(tmp_path), "--report-dir", str(reports)]
    )
    report = json.loads((reports / "sim.json").read_text())
    assert execution.exit_code == (2 if fault == "acceptance" else 0)
    if fault != "acceptance":
        assert flow.context._simulation_acceptance_outcomes
        projection = json.loads(next(reports.glob("sim/*/targets/*/simulation.json")).read_text())
        assert projection["tests"][0]["cycles"] == 1234
    if fault == "metadata":
        assert report["cycle_counts_error"] == "unavailable"
        assert "cycle_counts" not in report
    else:
        assert report["cycle_counts"] == {"sim": [{"test": "smoke", "cycle_count": 1234}]}
    assert report["detail"]["campaigns"]["sim"]["observations"][0]["cycle_count"] == 1234
    assert "smoke: cycles=1234" in execution.outcome.report_text


@pytest.mark.parametrize("cancelled", [False, True])
def test_ordinary_partial_failure_keeps_report_counts_separate(tmp_path, monkeypatch, cancelled):
    from booley.flows.sim.campaign.coordinator import SimulationCampaignCancellationError
    from tests.flows.sim.test_campaign_phase2 import _observation, _simulation_outcome

    flow = SimulateFlow()
    campaign = _simulation_outcome(
        tmp_path, [_observation("done") | {"cycle_count": 7}], required=("done",)
    )
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: tmp_path)
    monkeypatch.setattr(flow, "_write_campaign_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flow, "_plan_campaign_baselines", lambda *_args: ([], {}))
    monkeypatch.setattr(flow, "_candidate_campaign_requests", lambda *_args: [])

    def run(*_args, outcomes, **_kwargs):
        outcomes.append(campaign)
        error = SimulationCampaignCancellationError if cancelled else RuntimeError
        raise error("second target stopped")

    monkeypatch.setattr(flow, "_publish_and_run_campaign_requests", run)
    from booley.criteria.state import DevelopmentState

    flow._state = DevelopmentState()
    before = dict(flow.context.state.criteria)
    pending = flow.context._pending_criteria_set
    result = flow._run_ordinary_campaigns(
        [],
        {},
        AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False),
    )
    assert result.exit_code != 0
    assert flow.context._simulation_campaign_outcomes == ()
    assert flow.context._simulation_report_outcomes == (campaign,)
    assert flow.context.state.criteria == before
    assert flow.context._pending_criteria_set == pending
    assert not list(tmp_path.rglob("simulation.json"))
    if cancelled:
        assert result.detail == {}


def test_coverage_cancellation_keeps_completed_report_only_counts(tmp_path, monkeypatch):
    from booley.flows.sim.campaign.coordinator import SimulationCampaignCancellationError
    from tests.flows.sim.test_campaign_phase2 import _observation, _simulation_outcome

    flow = SimulateFlow()
    outcome = _simulation_outcome(tmp_path, [_observation("done")], required=("done",))
    responses = iter((outcome,))

    def run(_request):
        try:
            return next(responses)
        except StopIteration:
            raise SimulationCampaignCancellationError("cancelled") from None

    monkeypatch.setattr("booley.flows.sim.flow._retain_coverage_campaign", lambda *_args: None)
    progress = SimpleNamespace(checkpoint=lambda: None)
    from booley.criteria.state import DevelopmentState

    flow._state = DevelopmentState()
    before = dict(flow.context.state.criteria)
    pending = flow.context._pending_criteria_set
    result = flow._execute_coverage_campaign_requests(
        SimpleNamespace(run=run), [_request("first"), _request("second")], progress
    )
    assert result.detail == {}
    assert flow.context._simulation_report_outcomes == (outcome,)
    assert flow.context._simulation_campaign_outcomes == ()
    assert flow.context.state.criteria == before
    assert flow.context._pending_criteria_set == pending
    assert not list(tmp_path.rglob("simulation.json"))
