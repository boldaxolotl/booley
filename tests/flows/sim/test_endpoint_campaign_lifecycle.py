"""Public lifecycle contract for prepared Simulation Campaign execution."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    data = tmp_path / ".booley_project"
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
    data = tmp_path / ".booley_project"
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
    data = tmp_path / ".booley_project"
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
    data = tmp_path / ".booley_project"
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


@pytest.mark.parametrize("grade", ["pass", "fail"])
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
        path = Path(
            next(
                line.removeprefix("campaign manifest: ")
                for line in output.err.splitlines()
                if line.startswith("campaign manifest: ")
            )
        )
        assert path.is_absolute() and path.is_file()
        seen.append(path)
        return execution

    monkeypatch.setattr(
        "booley.flows.sim.flow.OrdinaryHdlSerialExecutor",
        lambda **kwargs: OrdinaryHdlSerialExecutor(**kwargs, execution_factory=factory),
    )
    result = flow.execute_cli(
        _campaign_cli_args(
            work_dir=str(tmp_path),
            report_dir=str(tmp_path / "reports"),
            target="sim",
            test=(
                "count",
                "reset",
            ),
        )
    )
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
        assert capsys.readouterr().err == f"campaign manifest: {seen[0]}\n"
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
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    stream = BufferedConsole()
    monkeypatch.setattr("booley.flows.sim.flow.sys.stderr", stream)

    def execution(*_args):
        assert stream.pending == ""
        manifest = Path(stream.getvalue().strip().removeprefix("campaign manifest: "))
        assert manifest.is_absolute() and manifest.is_file()
        return NativeExecution()

    result = SimulateFlow(coverage_execution=execution).execute_cli(
        _campaign_cli_args(
            work_dir=str(tmp_path),
            report_dir=str(tmp_path / "reports"),
            target="sim_0",
            coverage=True,
        )
    )
    assert result.exit_code == 0


def test_partial_publication_discloses_only_committed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.flows.sim.test_coverage_invocation import project

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator"))
    data = tmp_path / ".booley_project"
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
    assert "targets/sim_0/campaign/manifest.json" in result.outcome.report_text
    assert "targets/sim_1/campaign/manifest.json" not in result.outcome.report_text
