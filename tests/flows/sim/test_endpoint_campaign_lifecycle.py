"""Public lifecycle contract for prepared Simulation Campaign execution."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_WORK_DIR_NOTICE = "booley: --work-dir is deprecated; use --project instead (removal after one compatibility release)\n"

from booley.flows import endpoint_session
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTransportIdentity,
    write_adapter_result,
)
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.flow import SimulateFlow
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult, execute_endpoint
from tests.flows.sim.test_campaign_characterization import _AdapterAbortExecution, _plan


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


def _pre_sim_manifest(access, commands, transform):
    import json

    from booley.flows.sim.campaign.codec import canonical_json_bytes
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
    return finalize_manifest(document), disclosures


def _pre_sim_plan(access, commands, transform):
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan

    manifest, disclosures = _pre_sim_manifest(access, commands, transform)
    return create_simulation_campaign_plan(manifest), disclosures


def _hook_project(tmp_path, commands, live_commands):
    import json

    project = tmp_path / "project"
    project.mkdir()
    config = project / ".booley"
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
        _acceptance_recorder=flow.context._acceptance_recorder,
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
        lambda _admission, _invocation, _published: (campaign, requests),
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
    flow._args = SimpleNamespace(
        target=["sim"], result_verbosity="brief", report_dir=invocation.parent
    )
    result = flow._campaign_endpoint_outcome([campaign])
    endpoint = SimpleNamespace(
        args=SimpleNamespace(report_dir=invocation.parent, slug=""),
        flow=flow,
        name="sim",
        endpoint_kind="flow",
        _acceptance_recorder=flow.context._acceptance_recorder,
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


def _campaign_cli_args(
    *, work_dir, report_dir, target="", coverage=False, resume_from=None, test=()
):
    args = ["--work-dir", str(work_dir), "--report-dir", str(report_dir)]
    if target:
        args.extend(("--target", target))
    if coverage:
        args.append("--coverage")
    if resume_from is not None:
        args.extend(("--resume-from", str(resume_from)))
    for name in test:
        args.extend(("--test", name))
    return args


@pytest.mark.parametrize("target_count", [1, 2])
def test_cli_discloses_committed_manifest_before_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target_count: int,
) -> None:
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",) * target_count)
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text(
        "".join(f'[sim_{index}]\ntests = ["half"]\n' for index in range(target_count))
    )
    observed: list[Path] = []

    def execution(*_args):
        captured = capsys.readouterr()
        lines = [
            line for line in captured.err.splitlines() if line.startswith("campaign manifest: ")
        ]
        assert len(lines) == (target_count if not observed else 0)
        for line in lines:
            manifest = Path(line.removeprefix("campaign manifest: "))
            assert manifest.is_absolute() and manifest.is_file()
            observed.append(manifest)
        return NativeExecution()

    flow = SimulateFlow(coverage_execution=execution)
    result = flow.execute_cli(
        _campaign_cli_args(
            work_dir=str(tmp_path),
            report_dir=str(tmp_path / "reports with spaces"),
            target=",".join(f"sim_{index}" for index in range(target_count)),
            coverage=True,
        )
    )
    assert result.exit_code == 0
    output = capsys.readouterr()
    assert observed
    assert f"  manifest: {observed[0]}" in output.out
    assert f"  report: {(tmp_path / 'reports with spaces/sim.json').resolve()}" in output.out
    assert output.out.count("  manifest:") == target_count
    assert [path.parents[1].name for path in observed] == [
        f"sim_{index}" for index in range(target_count)
    ]


@pytest.mark.parametrize(
    "failure",
    ["before:manifest_commit", "after:manifest_commit", "executor", "cancel", "interrupt"],
)
def test_cli_recovery_paths_survive_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    from booley.flows.sim.campaign.coordinator import SimulationCampaignCancellationError
    from tests.flows.sim.test_coverage_invocation import project

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    seen: list[str] = []

    def execution(*_args):
        seen.append(capsys.readouterr().err)
        assert "campaign manifest: " in seen[0]
        if failure == "interrupt":
            raise KeyboardInterrupt()
        if failure == "cancel":
            raise SimulationCampaignCancellationError("cancelled")
        raise RuntimeError("executor failed")

    flow = SimulateFlow(coverage_execution=execution)

    def checkpoint(boundary: str) -> None:
        if boundary == failure:
            raise KeyboardInterrupt()

    monkeypatch.setattr(flow, "_campaign_publication_checkpoint", checkpoint)
    argv = _campaign_cli_args(
        work_dir=str(tmp_path), report_dir=str(tmp_path / "reports"), target="sim_0", coverage=True
    )
    if failure in {"before:manifest_commit", "after:manifest_commit", "interrupt"}:
        with pytest.raises(KeyboardInterrupt):
            flow.execute_cli(argv)
    else:
        result = flow.execute_cli(argv)
        assert result.exit_code in {2, 130}
        assert "  manifest: " in result.outcome.report_text
        assert (
            f"  report: {(tmp_path / 'reports/sim.json').resolve()}" in result.outcome.report_text
        )
    _assert_recovery_announcement(capsys, seen, failure)


def _assert_recovery_announcement(capsys, seen, failure):
    output = capsys.readouterr()
    announcements = "".join(seen) + output.err
    assert announcements.count("campaign manifest: ") == (
        0 if failure == "before:manifest_commit" else 1
    )
    if failure != "before:manifest_commit":
        path = Path(
            next(
                line.removeprefix("campaign manifest: ")
                for line in announcements.splitlines()
                if line.startswith("campaign manifest: ")
            )
        )
        assert path.is_file()


def test_typed_campaign_execution_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    result = flow.execute(
        SimRequest(
            work_dir=tmp_path, report_dir=tmp_path / "reports", target="sim_0", coverage=True
        )
    )
    assert result.exit_code == 0
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert "  manifest: " in result.outcome.report_text


def test_same_flow_second_failure_does_not_reuse_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    argv = _campaign_cli_args(
        work_dir=str(tmp_path), report_dir=str(tmp_path / "reports"), target="sim_0", coverage=True
    )
    first = flow.execute_cli(argv)
    manifest_line = next(
        line for line in first.outcome.report_text.splitlines() if line.startswith("  manifest:")
    )
    capsys.readouterr()

    def fail(boundary: str) -> None:
        if boundary == "before:manifest_commit":
            raise OSError("publication failed")

    monkeypatch.setattr(flow, "_campaign_publication_checkpoint", fail)
    second = flow.execute_cli(argv)
    assert second.exit_code == 2
    assert "manifest:" not in second.outcome.report_text
    assert manifest_line not in second.outcome.report_text
    assert "campaign manifest: " not in capsys.readouterr().err


class _CardExecution(_AdapterAbortExecution):
    passing = False

    def _invoke_process(self, command, *, timeout):
        result = super()._invoke_process(command, timeout=timeout)
        if self.passing and "BOOLEY_BUILD_STAGE" not in command[-1]:
            tokens = shlex.split(command[-1])
            selected = tuple(
                token.split("=", 1)[1] for token in tokens if token.startswith("--selected-test=")
            )
            identity = AdapterTransportIdentity(
                "cocotb",
                tokens[tokens.index("--attempt-token") + 1],
                tokens[tokens.index("--target-identity") + 1],
                selected,
                Path(tokens[tokens.index("--adapter-result") + 1]),
            )
            write_adapter_result(
                identity,
                AdapterResult(
                    True,
                    False,
                    0,
                    selected,
                    test_results=tuple(AdapterTestResult(name, "pass") for name in selected),
                ),
            )
            assert self.cocotb_path is not None
            payload = json.loads(self.cocotb_path.read_text())
            for test in payload["tests"]:
                test.update(status="pass", failure="")
            self.cocotb_path.write_text(json.dumps(payload))
        return result


@pytest.mark.parametrize("grade", ["pass", "fail", "interrupt"])
def test_ordinary_cli_manifest_and_card_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], grade: str
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    flow = SimulateFlow()
    monkeypatch.setattr(flow, "_ordinary_campaign_plan", lambda *_args, **_kwargs: plan)
    seen: list[Path] = []

    execution = _CardExecution(eda_tool="verilator", cocotb=True)
    execution.passing = grade == "pass"

    def factory(_options):
        output = capsys.readouterr()
        announcements = [
            line.removeprefix("campaign manifest: ")
            for line in output.err.splitlines()
            if line.startswith("campaign manifest: ")
        ]
        assert len(announcements) == 1
        path = Path(announcements[0])
        assert path.is_absolute() and path.is_file()
        seen.append(path)
        if grade == "interrupt":
            raise KeyboardInterrupt()
        return execution

    monkeypatch.setattr(
        "booley.flows.sim.flow.OrdinaryHdlSerialExecutor",
        lambda **kwargs: OrdinaryHdlSerialExecutor(**kwargs, execution_factory=factory),
    )
    argv = _campaign_cli_args(
        work_dir=str(tmp_path),
        report_dir=str(tmp_path / "reports"),
        target="sim",
        test=(
            "count",
            "reset",
        ),
    )
    if grade == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            flow.execute_cli(argv)
        assert len(seen) == 1 and seen[0].is_file()
        assert "campaign manifest: " not in capsys.readouterr().err
        return
    result = flow.execute_cli(argv)
    assert seen
    assert result.exit_code == (0 if grade == "pass" else 1)
    assert f"  manifest: {seen[0]}" in result.outcome.report_text
    assert f"  report: {(tmp_path / 'reports/sim.json').resolve()}" in result.outcome.report_text
    assert result.outcome.report_text.count("  manifest:") == 1

    _assert_ordinary_cli_resume(flow, plan, seen, tmp_path, monkeypatch, capsys, result, factory)


def _assert_ordinary_cli_resume(flow, plan, seen, tmp_path, monkeypatch, capsys, result, factory):
    capsys.readouterr()
    destination = tmp_path / "resumed reports"
    monkeypatch.setattr("booley.flows.sim.campaign.resume.git_full_sha", lambda *_args: "abc123")
    monkeypatch.setattr(flow, "_resume_campaign_plan", lambda *_args, **_kwargs: plan)

    def resume_executor(_coverage):
        checkpoints = list(destination.glob("sim/[0-9]*/progress.json"))
        assert len(checkpoints) == 1
        preparing = json.loads(checkpoints[0].read_text())
        assert preparing["complete"] is False
        assert preparing["completed_targets"] == []
        assert preparing["active"][0]["stage"] == "preparing"
        assert preparing["active"][0]["operation"] == "publication"
        assert capsys.readouterr().err == _WORK_DIR_NOTICE + f"campaign manifest: {seen[0]}\n"
        return OrdinaryHdlSerialExecutor(
            invoke=lambda *_args, **_kwargs: None, execution_factory=factory
        )

    monkeypatch.setattr(flow, "_resume_campaign_executor", resume_executor)
    resumed = flow.execute_cli(
        _campaign_cli_args(
            work_dir=str(tmp_path), report_dir=str(destination), resume_from=str(seen[0])
        )
    )
    assert resumed.exit_code == result.exit_code
    report = json.loads((destination / "sim.json").read_bytes())
    assert report.get("eda_tool") == plan.manifest.document["workload"]["eda"]["kind"]
    checkpoint = json.loads(next(destination.glob("sim/[0-9]*/progress.json")).read_text())
    assert checkpoint["complete"] is True
    assert checkpoint["completed_targets"] == ["sim"]
    assert "active" not in checkpoint
    assert f"  manifest: {seen[0]}" in resumed.outcome.report_text
    assert f"  report: {(destination / 'sim.json').resolve()}" in resumed.outcome.report_text


def test_cli_manifest_is_flushed_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    class BufferedConsole(io.StringIO):
        def __init__(self):
            super().__init__()
            self.pending = ""

        def write(self, value):
            self.pending += value
            return len(value)

        def flush(self):
            super().write(self.pending)
            self.pending = ""

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    stream = BufferedConsole()
    monkeypatch.setattr("booley.flows.sim.flow.sys.stderr", stream)

    def execution(*_args):
        assert stream.pending == ""
        manifest = Path(
            stream.getvalue()
            .removeprefix(_WORK_DIR_NOTICE)
            .strip()
            .removeprefix("campaign manifest: ")
        )
        assert manifest.is_absolute() and manifest.is_file()
        return NativeExecution()

    result = SimulateFlow(coverage_execution=execution).execute_cli(
        [
            "--quiet",
            *_campaign_cli_args(
                work_dir=str(tmp_path),
                report_dir=str(tmp_path / "reports"),
                target="sim_0",
                coverage=True,
            ),
        ]
    )
    assert result.exit_code == 0


def test_partial_publication_discloses_only_committed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.flows.sim.test_coverage_invocation import project

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator"))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n[sim_1]\ntests = ["half"]\n')
    commits = 0

    def checkpoint(boundary):
        nonlocal commits
        if boundary == "before:manifest_commit":
            commits += 1
            if commits == 2:
                raise OSError("second publication failed")

    def executor(*_args):
        raise AssertionError("work must wait for every manifest")

    result = SimulateFlow(
        coverage_execution=executor, campaign_publication_checkpoint=checkpoint
    ).execute_cli(
        _campaign_cli_args(
            work_dir=str(tmp_path),
            report_dir=str(tmp_path / "reports"),
            target="sim_0,sim_1",
            coverage=True,
        )
    )
    output = capsys.readouterr()
    assert result.exit_code == 2
    assert output.err.count("campaign manifest: ") == 1
    assert result.outcome.report_text.count("  manifest: ") == 1
    manifest = Path(
        next(
            line.removeprefix("  manifest: ")
            for line in result.outcome.report_text.splitlines()
            if line.startswith("  manifest: ")
        )
    )
    assert manifest.parts[-4:] == ("targets", "sim_0", "campaign", "manifest.json")
    assert manifest.is_absolute() and manifest.is_file()


def test_failure_paths_deduplicate_and_start_without_blank_line(tmp_path: Path) -> None:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path / "reports")
    path = tmp_path / "manifest.json"
    result = flow._with_campaign_paths(EndpointOutcome(), [path, path])
    assert result.report_text == (
        f"  manifest: {path.resolve()}\n  report: {(tmp_path / 'reports/sim.json').resolve()}"
    )


def test_campaign_recording_failure_retains_evaluated_final_report(tmp_path):
    from booley.flows.sim.acceptance import AcceptanceContext, SimulationAcceptanceCoordinator
    from tests.flows.report_headline import final_report_omits_headline
    from tests.flows.sim.test_campaign_phase2 import _observation, _simulation_outcome
    from tests.mcp_tools.test_base import unbound_specialist

    tool = unbound_specialist(tmp_path, "--work-dir", str(tmp_path))
    tool.state._file_path = tmp_path / "state.json"
    tool.state.init_criteria({"sim_pass_sim": True}, strict=True)

    class FailedRecorder:
        def acceptance_identity(self):
            return {}

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
    report = final_report_omits_headline(tool, EndpointOutcome(exit_code=2))
    assert report["passed"] is False
    assert tool.state.criteria["sim_pass_sim"].met is False


@pytest.mark.parametrize("conclusive", [True, False])
def test_standalone_campaign_has_only_current_conclusive_workload_verdict(tmp_path, conclusive):
    from dataclasses import replace

    from booley.flows.sim.acceptance import _record_standalone_campaign
    from tests.flows.report_headline import final_report_omits_headline
    from tests.flows.sim.test_campaign_phase2 import _observation, _simulation_outcome
    from tests.mcp_tools.test_base import unbound_specialist

    tool = unbound_specialist(tmp_path, "--work-dir", str(tmp_path))
    outcome = _simulation_outcome(tmp_path, [_observation("smoke")], required=("smoke",))
    if not conclusive:
        outcome = replace(outcome, complete=False)
    _record_standalone_campaign(tool, outcome)
    final_report_omits_headline(tool, EndpointOutcome())
    if conclusive:
        assert tool.state.criteria["sim_pass_sim"].met is True
        assert tool.state.criteria["sim_pass_sim"].detail["selected_tests"] == ["smoke"]
    else:
        assert "sim_pass_sim" not in tool.state.criteria
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


def _cycle_ticket(tmp_path):
    from booley.criteria.state import DevelopmentState
    from tests.criteria.ledger_support import FixtureRecorder

    path = tmp_path / "state.json"
    state = DevelopmentState.load(path)
    state.slug, state.ticket_type = "ticket", "implementation"
    key = "sim_pass_sim_smoke"
    params = {
        "target": "acme:lib:dut:1#sim",
        "_target_selector": "sim",
        "test_selector": "smoke",
        "required_tests": ["smoke"],
        "minimum_total": 1,
    }
    state.init_criteria({key: True}, criterion_params={key: params}, strict=True)
    state.save()
    recorder = FixtureRecorder(
        log_dir=tmp_path / "logs",
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    return path, state, key, recorder


def _public_cycle_flow(campaign, monkeypatch):
    from contextlib import nullcontext

    from booley.flows.flow_session import FlowSession

    flow = SimulateFlow()
    monkeypatch.setattr(flow, "_pre_state_gate", lambda: None)
    monkeypatch.setattr(FlowSession, "_criterion_binding_gate", lambda _self: None)
    monkeypatch.setattr(flow, "prepare_target_endpoint", lambda: None)
    monkeypatch.setattr(flow, "prepare_simulation_endpoint", object)
    monkeypatch.setattr(flow, "_resolve_display_label", lambda: None)
    monkeypatch.setattr(FlowSession, "admission", lambda *_args: nullcontext(None))
    monkeypatch.setattr(
        flow, "run_prepared_simulation", lambda *_args: flow._campaign_endpoint_outcome([campaign])
    )
    return flow


def _cycle_publication_fault(flow, monkeypatch, failure):
    import json

    from booley.flows import endpoint_reporting
    from booley.flows.sim import flow as module

    original_dumps, original_write = json.dumps, endpoint_reporting.atomic_write_json
    writes = []

    def encode(value, *args, **kwargs):
        rows = value.get("cycle_counts", value) if isinstance(value, dict) else {}
        if failure == "metadata" and rows == {"sim": [{"test": "smoke", "cycle_count": 1234}]}:
            raise ValueError("cycle metadata cannot encode")
        return original_dumps(value, *args, **kwargs)

    def write(destination, document):
        writes.append(destination)
        if failure in {"atomic", "atomic_console"} and len(writes) == 1:
            raise OSError("atomic write failed")
        return original_write(destination, document)

    def fail(*args, **kwargs):
        raise OSError("injected " + str(failure))

    monkeypatch.setattr(json, "dumps", encode)
    monkeypatch.setattr(endpoint_reporting, "atomic_write_json", write)
    if failure in {"console", "atomic_console"}:
        monkeypatch.setattr(type(flow.context), "_publish_console_report", fail)
    if failure == "acceptance":
        monkeypatch.setattr(flow, "record_campaign_acceptance", fail)
    if failure in {"render", "structured"}:
        monkeypatch.setattr(
            module,
            "_campaign_test_run_lines" if failure == "render" else "_campaign_structured_details",
            fail,
        )
    return writes


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "metadata",
        "console",
        "atomic",
        "atomic_console",
        "acceptance",
        "render",
        "structured",
    ],
)
def test_public_cycle_metadata_preserves_durable_accepted_criterion(
    tmp_path, monkeypatch, failure
):
    import json

    from booley.criteria.state import DevelopmentState

    campaign = _public_cycle_campaign(tmp_path, monkeypatch)
    path, _state, key, recorder = _cycle_ticket(tmp_path)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))
    flow = _public_cycle_flow(campaign, monkeypatch)
    writes = _cycle_publication_fault(flow, monkeypatch, failure)
    reports = tmp_path / "reports"
    result = flow.execute_cli(
        ["--target", "sim", "--work-dir", str(tmp_path), "--report-dir", str(reports)],
        adapter=recorder,
    )
    report = json.loads((reports / "sim.json").read_text())
    assert result.exit_code == (0 if failure in {None, "metadata"} else 2)
    saved = DevelopmentState.load(path)
    assert (saved.criteria[key].met is True) == (failure != "acceptance")
    assert len(saved.acceptance_transactions) == int(failure != "acceptance")
    if failure == "metadata":
        assert report["passed"] is True and report["cycle_counts_error"] == "unavailable"
        assert "cycle_counts" not in report
    else:
        assert report["cycle_counts"] == {"sim": [{"test": "smoke", "cycle_count": 1234}]}
    numbered = sorted(reports.glob("sim/*/report.json"))
    assert len(numbered) == (2 if failure in {"console", "atomic_console"} else 1)
    assert len(writes) == {"atomic": 3, "console": 4, "atomic_console": 5}.get(failure, 2)
    assert json.loads(numbered[-1].read_text()) == report
    if failure in {None, "metadata"}:
        projection = json.loads(next(reports.glob("sim/*/targets/*/simulation.json")).read_text())
        assert projection["tests"][0]["cycles"] == 1234
    if failure not in {"render", "structured"}:
        assert report["detail"]["campaigns"]["sim"]["observations"][0]["cycle_count"] == 1234
        assert "smoke: cycles=1234" in result.outcome.report_text


@pytest.mark.parametrize("cancelled", [False, True])
def test_ordinary_partial_failure_keeps_report_counts_separate(tmp_path, monkeypatch, cancelled):
    from booley.flows.sim.campaign.coordinator import SimulationCampaignCancellationError

    flow = SimulateFlow()
    campaign = _public_cycle_campaign(tmp_path, monkeypatch)
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: tmp_path)
    monkeypatch.setattr(flow, "_write_campaign_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flow, "_plan_campaign_baselines", lambda *_args: ([], {}))
    monkeypatch.setattr(flow, "_candidate_campaign_requests", lambda *_args: [])

    def run(*_args, outcomes, **_kwargs):
        outcomes.append(campaign)
        error = SimulationCampaignCancellationError if cancelled else RuntimeError
        raise error("second target stopped")

    monkeypatch.setattr(flow, "_publish_and_run_campaign_requests", run)
    result = flow._run_ordinary_campaigns(
        [],
        {},
        AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False),
    )
    assert result.exit_code != 0
    assert flow.context._simulation_campaign_outcomes == ()
    if cancelled:
        assert result.detail == {}
    _assert_partial_acceptance_policy(flow, result, tmp_path, campaign)


def test_coverage_cancellation_keeps_completed_report_only_counts(tmp_path, monkeypatch):
    from booley.flows.sim.campaign.coordinator import SimulationCampaignCancellationError

    flow = SimulateFlow()
    outcome = _native_cycle_campaign(tmp_path, monkeypatch)
    responses = iter((outcome,))

    def run(_request):
        try:
            return next(responses)
        except StopIteration:
            raise SimulationCampaignCancellationError("cancelled") from None

    monkeypatch.setattr("booley.flows.sim.flow._retain_coverage_campaign", lambda *_args: None)
    progress = SimpleNamespace(checkpoint=lambda: None)
    result = flow._execute_coverage_campaign_requests(
        SimpleNamespace(run=run), [_request("first"), _request("second")], progress
    )
    assert result.detail == {}
    assert flow.context._simulation_campaign_outcomes == ()
    assert all(item["cycle_count"] is None for item in outcome.observations)
    _assert_partial_acceptance_policy(flow, result, tmp_path, outcome)


def _native_cycle_campaign(tmp_path, monkeypatch):
    from booley.flows.sim.campaign.coordinator import SimulationCampaign
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _prepare_coverage_origin

    returned = []
    original = SimulationCampaign.run

    def capture(self, request):
        outcome = original(self, request)
        returned.append(outcome)
        return outcome

    monkeypatch.setattr(SimulationCampaign, "run", capture)
    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    result = flow.execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    )
    assert result.exit_code == 0
    return returned[0]


def _assert_partial_acceptance_policy(flow, result, tmp_path, campaign):
    import json

    from booley.flows.endpoint_acceptance import record_acceptance
    from booley.flows.endpoint_reporting import write_report
    from booley.flows.sim.request import SimRequest

    path, state, _key, recorder = _cycle_ticket(tmp_path)
    flow._args = SimRequest(target="sim", work_dir=tmp_path, report_dir=tmp_path / "reports")
    context = flow.context
    context._state = state
    context.configure_flow_execution(recorder)
    context._pending_criteria_set = ("preexisting",)
    before = path.read_bytes()
    projections = {p: p.read_bytes() for p in tmp_path.rglob("simulation.json")}
    record_acceptance(context, PreparedExecution(None, None, False, False), result)
    assert path.read_bytes() == before
    assert context.state.criteria
    assert context._pending_criteria_set == ()
    assert not list((tmp_path / "logs").rglob("*.json"))
    assert {p: p.read_bytes() for p in tmp_path.rglob("simulation.json")} == projections
    with context.publication_resources:
        context._finalize_result(result)
        report_path = write_report(context, result)
    report = json.loads(report_path.read_text())
    assert report["cycle_counts"] == {
        str(campaign.target["selector"]): [
            {"test": row["test"], "cycle_count": row["cycle_count"]}
            for row in campaign.observations
        ]
    }


@pytest.mark.parametrize("unnamed", [False, True])
def test_authenticated_nullable_counts_round_trip(tmp_path, monkeypatch, unnamed):
    from dataclasses import replace

    from booley.flows.sim.request import SimRequest
    from tests.flows.sim import test_campaign_phase3 as fixtures

    if unnamed:
        campaign = _unnamed_cycle_campaign(tmp_path, monkeypatch)
    else:
        original = fixtures._shared_outcome

        def null_outcome(*args):
            outcome = original(*args)
            return replace(
                outcome, tests=tuple(replace(test, cycles=None) for test in outcome.tests)
            )

        monkeypatch.setattr(fixtures, "_shared_outcome", null_outcome)
        campaign = _public_cycle_campaign(tmp_path, monkeypatch)
    flow = SimulateFlow()
    flow._args = SimRequest(target="sim", work_dir=tmp_path, report_dir=tmp_path / "reports")
    result = flow._campaign_endpoint_outcome([campaign])
    _assert_nullable_cycle_report(flow, result, tmp_path, unnamed)


def _unnamed_cycle_campaign(tmp_path, monkeypatch):
    from booley.flows.sim.campaign.coordinator import (
        CampaignPolicy,
        NewCampaignRunRequest,
        SimulationCampaign,
    )
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan
    from tests.flows.sim import test_campaign_phase3 as fixtures

    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()
    handle = fixtures._handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    invocation = tmp_path / "reports/sim/1"
    invocation.mkdir(parents=True)
    plan = create_simulation_campaign_plan(fixtures._unfiltered_cocotb_manifest())
    executor = fixtures._failed_shared_executor(
        tmp_path / "engine-build", {"compile": 0, "launch": 0}
    )
    return SimulationCampaign(executor).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, fixtures._admission()
        )
    )


def _assert_nullable_cycle_report(flow, result, tmp_path, unnamed):
    import json

    from booley.flows.endpoint_reporting import write_report

    with flow.context.publication_resources:
        path = write_report(flow.context, result)
    report = json.loads(path.read_text())
    expected = [{"test": None if unnamed else "smoke", "cycle_count": None}]
    assert report["cycle_counts"]["sim"] == expected
    assert report == json.loads((tmp_path / "reports/sim.json").read_text())
    preview = report["detail"]["campaigns"]["sim"]["observations"]
    assert preview[0]["cycle_count"] is None
    assert preview[0]["test"] == expected[0]["test"]


@pytest.mark.parametrize("failure", ["run", "retention"])
def test_coverage_failure_persists_only_returned_outcomes(tmp_path, monkeypatch, failure):
    from booley.flows.sim.campaign.coordinator import SimulationCampaign
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _prepare_coverage_origin

    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    core = tmp_path / "counter.core"
    target = core.read_text().split("targets:\n", 1)[1]
    core.write_text(
        core.read_text() + target.replace("sim_0:", "sim_1:") + target.replace("sim_0:", "sim_2:")
    )
    (tmp_path / ".booley/tests.toml").write_text(
        '[sim_0]\ntests=["reset"]\n[sim_1]\ntests=["reset"]\n[sim_2]\ntests=["reset"]\n'
    )
    original_run = SimulationCampaign.run
    original_retain = __import__("booley.flows.sim.flow", fromlist=["_retain_coverage_campaign"])
    retain = original_retain._retain_coverage_campaign
    returned = []

    def run(campaign, request):
        if failure == "run" and len(returned) == 1:
            raise RuntimeError("second run failed before outcome")
        outcome = original_run(campaign, request)
        returned.append(outcome)
        return outcome

    def retain_or_fail(progress, outcome):
        if failure == "retention" and len(returned) == 2:
            raise OSError("completed second retention failed")
        return retain(progress, outcome)

    monkeypatch.setattr(SimulationCampaign, "run", run)
    monkeypatch.setattr(original_retain, "_retain_coverage_campaign", retain_or_fail)
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    result = flow.execute(
        SimRequest(
            target="sim_0,sim_1,sim_2", work_dir=tmp_path, coverage=True, report_dir=reports
        )
    )
    assert result.exit_code == 2
    assert len(returned) == (1 if failure == "run" else 2)
    _assert_coverage_failure_counts(flow, reports, returned)


def _assert_coverage_failure_counts(flow, reports, returned):
    import json

    assert flow.context._simulation_campaign_outcomes == tuple(returned[:1])
    report = json.loads((reports / "sim/1/report.json").read_text())
    expected = {
        f"sim_{index}": [{"test": "reset", "cycle_count": None}] for index in range(len(returned))
    }
    assert report["cycle_counts"] == expected
    assert "sim_2" not in report["cycle_counts"]
    assert report["detail"]["pending_targets"] == ["sim_1", "sim_2"]


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
    (project / ".booley/booley.toml").write_text(
        "[flows.sim]\npre_run_commands = " + json.dumps(["echo public-hook"]) + "\n"
    )
    (project / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a", "b"]\n')
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    flow = SimulateFlow()
    # Publication tests must allow for Git for Windows shell startup under CI load.
    flow._simulation_execution_override = SimulationExecution(
        invoke=_subprocess_invoker(project),
        options=SimulationOptions(timeout_ms=60_000 if os.name == "nt" else 5000),
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

    from tests.flows.sim.test_campaign_manifest_codec import _selection_test_runs

    names = ["alpha", "beta"]
    document["work_items"][0].update(
        kind="cocotb_batch",
        selection={"kind": "named", "names": names},
        arguments=names,
        test_runs=_selection_test_runs(names),
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
    from tests.criteria.ledger_support import FixtureRecorder

    log_dir = tmp_path / "logs/ticket"
    state_path = log_dir / ".runtime/booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "ticket"
    state.init_criteria({"sim_pass_sim_a": True}, strict=True)
    state.work_dir = str(project)
    state.save()

    class PreparedTicketRecorder(FixtureRecorder):
        def validate_and_resolve(self, request):
            # Ticket admission is a fixture; recording uses the actual ledger owner.
            request.state_file = state_path
            request.slug = "ticket"
            return ResolvedFlowAcceptance(ticket_backed=True)

    recorder = PreparedTicketRecorder(
        log_dir=log_dir,
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
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


def test_ordinary_resume_retains_returned_outcome_when_terminal_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = SimulateFlow()
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    returned = SimpleNamespace(target=plan.manifest.document["target"])
    phases = []
    monkeypatch.setattr(flow, "_run_validated_resume_campaign", lambda *_args: returned)

    def checkpoint(_targets, outcomes, *, phase, complete):
        phases.append(phase)
        if complete:
            assert outcomes == [returned]
            raise OSError("required terminal write unavailable")

    monkeypatch.setattr(flow, "_write_campaign_progress", checkpoint)
    outcome, error = flow._execute_ordinary_resume_progress(
        SimpleNamespace(manifest=plan.manifest),
        tmp_path,
        AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False),
        lambda _path: None,
    )
    assert outcome is returned
    assert isinstance(error, OSError)
    assert phases == ["starting", "running", "complete", "aborted"]
    assert flow.context.cycle_report_json is None


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_prepared_ordinary_invoker_observes_owned_attempt_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access: str,
) -> None:
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    with (project / ".booley/booley.toml").open("a") as config:
        config.write(f'pre_sim_build_access = "{access}"\n')
    original = flow._execute_boundary
    observations = []
    reports = tmp_path / "reports"

    def invoke(command, *, timeout=None, cwd=None):
        invocation = flow.context._reserved_invocation_dir
        if invocation is not None and (invocation / "progress.json").exists():
            progress = json.loads((invocation / "progress.json").read_text())
            for active in progress.get("active", []):
                if active["stage"] == "executing":
                    assert progress["complete"] is False
                    assert progress["completed_targets"] == []
                    pointer = active["log"]
                    assert pointer["live"] is True and pointer["complete"] is False
                    log = project / pointer["path"]
                    assert log.is_file()
                    assert "flow=sim" in log.read_text().splitlines()[0]
                    assert str(log.parent) in command[-1]
                observations.append(active["stage"])
        return original(command, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(flow, "_execute_boundary", invoke)
    result = flow.execute(SimRequest(target="sim_a", work_dir=project, report_dir=reports))
    assert result.exit_code == 0, result.outcome.report_text
    assert "building" in observations
    assert observations.count("executing") == 2
    terminal = json.loads((reports / "sim/1/progress.json").read_text())
    assert terminal["completed_targets"] == ["sim_a"]
    assert "active" not in terminal


@pytest.mark.timeout(180)
def test_prepared_campaign_active_write_failure_preserves_durable_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.flows.sim import live_progress
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    control_reports = tmp_path / "control-reports"
    control = SimulateFlow().execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=control_reports)
    )
    assert control.exit_code == 0, control.outcome.report_text
    control_campaign = control_reports / "sim/1/targets/sim_a/campaign"
    control_records = sorted(
        (
            json.loads(path.read_text())
            for path in control_campaign.glob("work-items/*/result.json")
        ),
        key=lambda record: record["observations"][0]["test"],
    )
    publish = live_progress.write_progress_json
    failures = []

    def fail_active(path, payload, **kwargs):
        if "lock_timeout_s" in kwargs:
            failures.append(payload["phase"])
            raise OSError("advisory checkpoint unavailable")
        return publish(path, payload, **kwargs)

    monkeypatch.setattr(live_progress, "write_progress_json", fail_active)
    reports = tmp_path / "reports"
    result = flow.execute(SimRequest(target="sim_a", work_dir=project, report_dir=reports))
    assert result.exit_code == 0, result.outcome.report_text
    assert failures
    assert "observation_error" in result.outcome.detail
    assert "campaign_error" not in result.outcome.detail
    campaign = reports / "sim/1/targets/sim_a/campaign"
    results = [json.loads(path.read_text()) for path in campaign.glob("work-items/*/result.json")]
    assert len(results) == 2
    assert all(record["grade"] == "pass" for record in results)
    results.sort(key=lambda record: record["observations"][0]["test"])
    for record, expected in zip(results, control_records, strict=True):
        assert {key: record[key] for key in ("grade", "state", "observations", "diagnostics")} == {
            key: expected[key] for key in ("grade", "state", "observations", "diagnostics")
        }
    builds = [
        json.loads(path.read_text())
        for path in campaign.glob("build-variants/*/attempts/*/build-result.json")
    ]
    assert len(builds) == 1
    assert builds[0]["state"] == "ready"
    final = json.loads((reports / "sim/1/progress.json").read_text())
    assert final["completed_targets"] == ["sim_a"]
    assert final["observation_health"]["error"] == "OSError: advisory checkpoint unavailable"


def test_hook_success_survives_build_failure(tmp_path, monkeypatch):
    from tests.flows.sim.test_campaign_phase3_integrity import _Group

    def fail_build(group):
        sidecars = list(
            tmp_path.glob("reports/1/targets/sim/campaign/work-items/*/attempts/*/pre-sim/*.json")
        )
        assert len(sidecars) == 1, "hook must be published before the build fails"
        return SimpleNamespace(passed=False)

    from dataclasses import replace

    from tests.flows.sim.test_campaign_phase3 import _failed_shared_outcome

    failed = _failed_shared_outcome()
    failed = replace(failed, tests=(replace(failed.tests[0], name="alpha"),))
    monkeypatch.setattr(_Group, "finish_build_failure", lambda self: failed, raising=False)
    monkeypatch.setattr(_Group, "compile", fail_build)
    invocation, campaign = _successful_pre_sim_campaign(tmp_path, monkeypatch, "legacy-per-test")
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._campaign_endpoint_outcome([campaign])
    assert result.exit_code == 1
    assert len(campaign.pre_sim_firings) == 1
    firing = campaign.pre_sim_firings[0]
    assert firing.document["status"] == "passed" and firing.document["returncode"] == 0
    assert firing.document["stdout_tail"] == "hook-out\n"
    assert firing.document["stderr_tail"] == "hook-err\n"
    assert "rc=0" in result.detail["pre_sim_lines"][0]
    terminal = json.loads((firing.path.parents[3] / "result.json").read_text())
    assert terminal["state"] == "blocked_by_build"
    build = json.loads((firing.path.parent.parent / "private-build/build-result.json").read_text())
    assert build["state"] == "design_failure"
    assert build["observation"]["detail"]["text"] == "compiler tail"


def completed_campaign_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reports: Path | None = None
) -> dict[str, Any]:
    """Produce an actual endpoint report with a deterministic fake EDA executable."""
    from booley.flows.sim.request import SimRequest
    from tests.flows.test_endpoint_reporting import fake_accounting

    fake_accounting(monkeypatch)
    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    reports = reports or tmp_path / "reports"
    result = flow.execute(SimRequest(target="sim_a", work_dir=project, report_dir=reports))
    assert result.exit_code == 0, result.outcome.report_text
    return json.loads((reports / "sim.json").read_bytes())


def test_completed_campaign_report_records_tool_and_resources(tmp_path, monkeypatch):
    report = completed_campaign_report(tmp_path, monkeypatch)
    assert report.get("eda_tool") == "icarus"
    assert report.get("resources") == {"cpu_user_s": 3.75, "cpu_sys_s": 0.75, "peak_rss_mb": 256}


@pytest.mark.parametrize("mode", ["simulate", "elab_only"])
def test_campaign_report_records_tools_by_target(tmp_path, monkeypatch, mode):
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    (project / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a"]\n[sim_b]\ntests = ["a"]\n')
    result = flow.execute(
        SimRequest(
            target="sim_a,sim_b", mode=mode, work_dir=project, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 0, result.outcome.report_text
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    assert report.get("eda_tools") == {"sim_a": "icarus", "sim_b": "icarus"}
    assert "eda_tool" not in report


def test_coverage_campaign_report_records_tool(tmp_path, monkeypatch):
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _prepare_coverage_origin

    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    )
    assert result.exit_code == 0
    assert json.loads((reports / "sim.json").read_bytes()).get("eda_tool") == "verilator"


def test_partial_campaign_planning_keeps_known_and_unknown_tools(tmp_path, monkeypatch):
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    (project / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a"]\n[sim_b]\ntests = ["a"]\n')
    original = flow._ordinary_campaign_plan

    def plan(target, *args, **kwargs):
        if target == "sim_b":
            raise ValueError("second Target unavailable")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(flow, "_ordinary_campaign_plan", plan)
    result = flow.execute(
        SimRequest(target="sim_a,sim_b", work_dir=project, report_dir=tmp_path / "reports")
    )
    assert result.exit_code == 2
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    assert report.get("eda_tools") == {"sim_a": "icarus", "sim_b": None}
