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


def _refresh_hook_workload_identity(document):
    from booley.flows.sim.campaign.planning import canonical_sha256

    workload = document["workload"]
    variant = document["build_variants"][0]
    digest = canonical_sha256(
        {
            "kind": variant["kind"],
            "source_closure": variant["source_closure"],
            **{
                key: workload[key]
                for key in ("source_recipe", "build_recipe", "eda", "trace", "coverage")
            },
        }
    )
    variant["recipe_sha256"] = digest
    variant["build_variant_id"] = "variant:" + digest.removeprefix("sha256:")
    item = document["work_items"][0]
    item["target"] = document["target"]
    item["role"] = document["target"]["role"]
    item["revision"] = document["target"]["revision"]
    item["build_variant_id"] = variant["build_variant_id"]
    digest = canonical_sha256(
        {
            key: value
            for key, value in item.items()
            if key not in {"work_item_id", "fingerprint_sha256"}
        }
    )
    item["fingerprint_sha256"] = digest
    item["work_item_id"] = "item:0000:" + digest.removeprefix("sha256:")[:16]


def _pre_sim_plan(access, commands, transform):
    import json

    from booley.flows.sim.campaign.codec import canonical_json_bytes
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan
    from booley.flows.sim.campaign.planning import finalize_manifest
    from tests.flows.sim.test_campaign_phase3_integrity import _legacy_disclosures, _manifest_for

    document = json.loads(canonical_json_bytes(_manifest_for(("alpha",), access=access).document))
    document.pop("fingerprints")
    if transform is not None:
        transform(document)
    document["workload"]["source_recipe"]["pre_sim_commands"] = list(commands)
    _refresh_hook_workload_identity(document)
    disclosures = _legacy_disclosures() if access == "legacy-per-test" else {}
    if disclosures:
        document["planning_disclosures"] = [disclosures["alpha"]]
    plan = create_simulation_campaign_plan(finalize_manifest(document))
    return plan, disclosures


def _hook_project(tmp_path, commands, live_commands):
    import json

    project = tmp_path / "project"
    project.mkdir()
    config = project / ".booley_project"
    config.mkdir()
    (config / "booley.toml").write_text(
        "[flows.sim]\npre_run_commands = "
        + json.dumps(commands if live_commands is None else live_commands)
        + "\n"
    )
    return project


def _hook_executor(build, disclosures, mutate_snapshot, checkpoint):
    from tests.flows.sim.test_campaign_phase3_integrity import _executor

    return _executor(
        build,
        {"compile": 0, "durable_reuse": 0, "launch": 0},
        disclosures=disclosures,
        mutate_snapshot=mutate_snapshot,
        checkpoint=checkpoint,
    )


def _successful_pre_sim_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access: str,
    *,
    commands: tuple[str, ...] = ("echo hook-out", "echo hook-err >&2"),
    mutate_snapshot: bool = False,
    live_commands: tuple[str, ...] | None = None,
    transform=None,
    invocation_directory: Path | None = None,
    checkpoint=None,
    observer=None,
):
    from booley.flows.sim.campaign.coordinator import (
        SimulationCampaign,
    )
    from tests.flows.sim.test_campaign_phase3_integrity import (
        _handle,
    )

    project = _hook_project(tmp_path, commands, live_commands)
    plan, disclosures = _pre_sim_plan(access, commands, transform)
    build = tmp_path / "build"
    build.mkdir()
    (build / "simv").write_bytes(b"image")
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.simulation_target_environment",
        lambda _handle: {},
    )
    invocation = invocation_directory or tmp_path / "reports/1"
    invocation.mkdir(parents=True, exist_ok=True)
    campaign = SimulationCampaign(
        _hook_executor(build, disclosures, mutate_snapshot, checkpoint)
    ).run(_observed_campaign_request(plan, project, invocation, observer))
    return invocation, campaign


def _observed_campaign_request(plan, project, invocation, observer):
    from booley.flows.sim.campaign.coordinator import CampaignPolicy, NewCampaignRunRequest
    from tests.flows.sim.test_campaign_phase3_integrity import _admission

    return NewCampaignRunRequest(
        plan,
        project,
        invocation.parent,
        CampaignPolicy(),
        invocation,
        _admission(),
        pre_sim_firing_published=observer,
    )


def _public_hook_endpoint(flow, invocation):
    import time

    from booley.flows.endpoint_report_criteria import ReportCriteria

    endpoint = SimpleNamespace(
        args=SimpleNamespace(report_dir=invocation.parent, slug=""),
        flow=flow,
        name="sim",
        endpoint_kind="flow",
        _report_criteria=ReportCriteria(frozen=True),
        _reserved_invocation_dir=invocation,
        _start_time=time.monotonic(),
        _selected_target="sim",
        _eda_tool="icarus",
        _raw_argv=None,
        _console_publication_requested=True,
        _stdout_witness=None,
    )
    return endpoint


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_successful_pre_sim_firing_reaches_public_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    access: str,
) -> None:
    import json
    import re

    from booley.flows.endpoint_reporting import _publish_console_report, write_report

    invocation, campaign = _successful_pre_sim_campaign(tmp_path, monkeypatch, access)
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._campaign_endpoint_outcome([campaign])
    endpoint = _public_hook_endpoint(flow, invocation)
    write_report(endpoint, result)
    _publish_console_report(endpoint, result)
    lines = re.findall(
        r"pre_run_commands \(2 line\(s\)\) for sim/alpha: rc=0 in \d+\.\ds",
        result.report_text,
    )
    assert len(lines) == 1
    assert capsys.readouterr().out.count(lines[0]) == 1
    report = json.loads((invocation.parent / "sim.json").read_bytes())
    assert report["detail"]["pre_sim_lines"] == lines
    assert (invocation / "report.json").read_bytes() == (
        invocation.parent / "sim.json"
    ).read_bytes()
    sidecars = list(invocation.glob("targets/sim/campaign/work-items/*/attempts/*/pre-sim/*.json"))
    assert len(sidecars) == 1
    evidence = json.loads(sidecars[0].read_bytes())
    assert evidence["returncode"] == 0
    assert evidence["stdout_tail"] == "hook-out\n"
    assert evidence["stderr_tail"] == "hook-err\n"


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_failed_hook_reports_real_exit_code_without_changing_adapter_grade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    invocation, campaign = _successful_pre_sim_campaign(
        tmp_path, monkeypatch, access, commands=("echo failed-out; echo failed-err >&2; exit 7",)
    )
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._campaign_endpoint_outcome([campaign])
    assert result.exit_code == 1
    assert len(result.detail["pre_sim_lines"]) == 1
    assert "rc=7" in result.detail["pre_sim_lines"][0]
    assert campaign.pre_sim_firings[0].document["stderr_tail"] == "failed-err\n"


def test_downstream_integrity_failure_retains_authenticated_current_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.campaign import SimulationCampaignIntegrityError

    flow = SimulateFlow()
    flow._current_published_pre_sim_keys = set()
    with pytest.raises(SimulationCampaignIntegrityError):
        _successful_pre_sim_campaign(
            tmp_path,
            monkeypatch,
            "immutable",
            mutate_snapshot=True,
            observer=flow._record_pre_sim_firing,
        )
    flow.context._reserved_invocation_dir = tmp_path / "reports/1"
    result = flow._attach_published_pre_sim(
        EndpointOutcome(exit_code=2, report_text="integrity error")
    )
    assert len(result.detail["pre_sim_lines"]) == 1
    assert "rc=0" in result.detail["pre_sim_lines"][0]
    assert result.detail["pre_sim_runs"][0]["terminal"] is False


def test_endpoint_reauthentication_never_renders_corrupt_hook_as_authoritative(
    tmp_path, monkeypatch
):
    invocation, campaign = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._campaign_endpoint_outcome([campaign])
    assert result.detail["pre_sim_current"] == 1
    firing = campaign.pre_sim_firings[0]
    firing.path.write_bytes(firing.path.read_bytes() + b" ")
    result = flow._attach_published_pre_sim(result)
    assert result.exit_code == 2 and result.criterion_met is False
    assert "pre_run_commands" not in result.report_text
    assert "pre_sim_lines" not in result.detail
    assert "pre_sim_runs" not in result.detail
    assert result.detail["pre_sim_unauthenticated_references"]


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


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_campaign_executor_uses_frozen_hook_commands_despite_live_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    _invocation, outcome = _successful_pre_sim_campaign(
        tmp_path,
        monkeypatch,
        access,
        commands=("echo frozen-hook",),
        live_commands=("echo live-hook; exit 7",),
    )
    assert outcome.complete
    assert len(outcome.pre_sim_firings) == 1
    firing = outcome.pre_sim_firings[0].document
    assert firing["returncode"] == 0
    assert firing["stdout_tail"] == "frozen-hook\n"
    assert firing["command_count"] == 1


def _ordinary_public_hook_fixture(tmp_path, monkeypatch):
    import json
    import os

    from booley.flows.sim.execution import SimulationExecution, SimulationOptions
    from tests.flows.sim.test_execution_engine import (
        _subprocess_invoker,
        _write_stale_compiler_fixture,
    )

    project, _source = _write_stale_compiler_fixture(tmp_path)
    (project / ".booley_project/booley.toml").write_text(
        "[flows.sim]\npre_run_commands = " + json.dumps(["echo public-hook"]) + "\n"
    )
    (project / ".booley_project/tests.toml").write_text('[sim_a]\ntests = ["a", "b"]\n')
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    flow = SimulateFlow()
    flow._simulation_execution_override = SimulationExecution(
        invoke=_subprocess_invoker(project), options=SimulationOptions(timeout_ms=5000)
    )
    return project, flow


def test_public_ordinary_prepared_endpoint_reports_each_shared_build_launch(tmp_path, monkeypatch):
    import json

    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports")
    )
    assert result.exit_code == 0, result.outcome.report_text
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    lines = report["detail"]["pre_sim_lines"]
    assert len(lines) == report["detail"]["pre_sim_current"] == 2
    assert any("for sim_a/a: rc=0" in line for line in lines)
    assert any("for sim_a/b: rc=0" in line for line in lines)
    assert all(report["report_text"].count(line) == 1 for line in lines)
    assert (tmp_path / "reports/sim/1/report.json").read_bytes() == (
        tmp_path / "reports/sim.json"
    ).read_bytes()
    manifest = tmp_path / "reports/sim/1/targets/sim_a/campaign/manifest.json"
    from booley.flows.sim.campaign import read_pre_sim_firings

    firings = read_pre_sim_firings(manifest)
    assert len(firings) == 2
    campaign_root = manifest.parent
    assert len(list(campaign_root.glob("build-variants/*/attempts/*/build-result.json"))) == 1
    assert all(firing.document["stdout_tail"] == "public-hook\n" for firing in firings)


@pytest.mark.parametrize("mode", ["dry_run", "elab_only"])
def test_public_nonrunning_modes_create_no_hook_firings(tmp_path, monkeypatch, mode):
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    options = {"dry_run": True} if mode == "dry_run" else {"mode": "elab_only"}
    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports", **options)
    )
    assert result.exit_code == 0, result.outcome.report_text
    assert not list((tmp_path / "reports").glob("**/pre-sim/*.json"))
    assert "pre_run_commands (" not in result.outcome.report_text


def test_legacy_abort_progress_failure_preserves_collected_firings(tmp_path, monkeypatch):
    import time

    from booley.flows.sim.execution.contract import PreSimEvidence
    from booley.flows.sim.execution.pre_sim_reporting import pre_sim_document

    flow = SimulateFlow()

    def prepare(*_args):
        flow._legacy_pre_sim_runs = [
            (
                "sim",
                pre_sim_document(
                    PreSimEvidence(
                        ("true",),
                        ("alpha",),
                        "passed",
                        0.1,
                        returncode=0,
                        stdout_tail="hook-out",
                    )
                ),
            )
        ]
        return EndpointOutcome(exit_code=2, report_text="later build failed")

    monkeypatch.setattr(flow, "_prepare_legacy_run", prepare)

    def failed_progress(*_args, **_kwargs):
        raise OSError("progress unavailable")

    monkeypatch.setattr(flow, "_write_progress_report", failed_progress)
    result = flow._run_legacy_selected_mode(["sim"], {}, time.monotonic(), 0.0)
    assert result.exit_code == 2
    assert result.detail["pre_sim_runs"][0]["stdout_tail"] == "hook-out"
    assert len(result.detail["pre_sim_lines"]) == result.report_text.count("pre_run_commands") == 1


def _cocotb_hook_plan(document):
    import hashlib

    names = ["alpha", "beta"]
    document["work_items"][0].update(
        kind="cocotb_batch", selection={"kind": "named", "names": names}, arguments=names
    )
    source = b"alpha\nbeta\n"
    document["required_suite"].update(
        names=names,
        source_bytes=len(source),
        source_sha256="sha256:" + hashlib.sha256(source).hexdigest(),
    )


def test_actual_hook_fires_once_for_two_test_cocotb_batch(tmp_path, monkeypatch):
    from dataclasses import replace

    from booley.flows.sim.campaign import read_pre_sim_firings
    from booley.flows.sim.execution.contract import (
        SimulationArtifactEvidence,
        SimulationTestOutcome,
    )
    from tests.flows.sim.test_campaign_phase3_integrity import _Group

    original = _Group.launch_snapshot

    def two_results(group, snapshot, run_cwd):
        outcome = original(group, snapshot, run_cwd)
        import json

        transport = snapshot / "cocotb-results.json"
        transport.write_text(
            json.dumps(
                {
                    "state": "ok",
                    "tests": [{"name": name, "status": "pass"} for name in group.names],
                }
            )
        )
        return replace(
            outcome,
            tests=tuple(
                SimulationTestOutcome(name=name, verdict="pass", passed=True)
                for name in group.names
            ),
            artifacts=(
                SimulationArtifactEvidence(
                    "cocotb_results_json", str(transport), transport.stat().st_size, group.names
                ),
            ),
        )

    monkeypatch.setattr(_Group, "launch_snapshot", two_results)
    invocation, outcome = _successful_pre_sim_campaign(
        tmp_path, monkeypatch, "immutable", transform=_cocotb_hook_plan
    )
    assert outcome.aggregate_grade == "pass"
    firings = read_pre_sim_firings(invocation / "targets/sim/campaign/manifest.json")
    assert len(firings) == 1
    assert firings[0].document["test_names"] == ("alpha", "beta")
    assert firings[0].document["stdout_tail"] == "hook-out\n"


def _public_ticket_hook_adapter(tmp_path, project):
    from booley.criteria.state import DevelopmentState
    from booley.evidence.acceptance import ResolvedFlowAcceptance
    from booley.ticket_board.flow_execution import TicketAcceptanceRecorder

    log_dir = tmp_path / "logs/ticket"
    state_path = log_dir / ".runtime/booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "ticket"
    state.init_criteria({"sim_pass_sim_a": True}, strict=True)
    state.work_dir = str(project)
    state.save()

    class PreparedTicketRecorder(TicketAcceptanceRecorder):
        def validate_and_resolve(self, request):
            # Ticket admission is a fixture; recording uses the actual ledger owner.
            request.state_file = state_path
            request.slug = "ticket"
            return ResolvedFlowAcceptance(ticket_backed=True)

    recorder = PreparedTicketRecorder(
        log_dir=log_dir,
        ticket_identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    return recorder, state_path, log_dir


@pytest.mark.parametrize("corrupt", [False, True])
def test_public_ticket_integrity_downgrade_never_commits_or_publishes_true(
    tmp_path, monkeypatch, corrupt
):
    import json

    from booley.criteria.state import DevelopmentState
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    adapter, state_path, log_dir = _public_ticket_hook_adapter(tmp_path, project)
    project_outcome = flow._campaign_endpoint_outcome

    def corrupt_after_projection(outcomes):
        result = project_outcome(outcomes)
        if corrupt:
            path = outcomes[0].pre_sim_firings[0].path.parents[3] / "result.json"
            path.write_text("{}")
        return result

    monkeypatch.setattr(flow, "_campaign_endpoint_outcome", corrupt_after_projection)
    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports"),
        adapter=adapter,
    )
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    state = DevelopmentState.load(state_path)
    assert result.exit_code == (2 if corrupt else 0), result.outcome.report_text
    assert report["criterion_met"] is (None if corrupt else True)
    assert state.criteria["sim_pass_sim_a"].met is (not corrupt)
    assert bool(state.acceptance_transactions) is (not corrupt)
    assert bool(list((log_dir / "acceptance/transactions").glob("*.json"))) is (not corrupt)
    if corrupt:
        assert "Simulation Campaign integrity failure" in report["report_text"]
        assert report["detail"]["pre_sim_current"] == 2
        assert len(report["detail"]["pre_sim_lines"]) == 2
