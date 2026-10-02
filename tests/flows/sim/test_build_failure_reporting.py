"""Build timeout/OOM evidence must reach the agent-visible report (issue #882).

Every consumer of an infrastructure build outcome (campaign Build Result,
typed campaign error, endpoint report, legacy report, coverage build) must lead
with the actionable reason and a bounded, marker-free output tail.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import booley.flows.sim.build as build_module
import booley.flows.sim.campaign.coverage_execution as coverage_execution_module
from booley.flows.base import SubprocessResult
from booley.flows.sim.build import classify_build_outcome
from booley.flows.sim.build_parallelism import verilator_backend_arguments
from booley.flows.sim.campaign.codec import (
    MAX_DETAIL_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution import SimulationOptions
from booley.flows.sim.flow import MissingExecutableError, SimulateFlow
from booley.flows.sim.verilator_coverage import SimulationBuildResult
from booley.mcp.base import EXIT_ERROR
from tests.flows.sim import test_campaign_crash_matrix as crash_matrix
from tests.flows.sim.test_build_parallelism import _inspection as _parallelism_inspection
from tests.flows.sim.test_campaign_crash_matrix import _admission
from tests.flows.sim.test_campaign_phase5_adversarial import (
    _FailedCoverageBuild,
    _run_coverage_campaign,
)
from tests.flows.sim.test_execution_engine import _handle, _prepared, _run_execution
from tests.flows.sim.test_verilator_coverage_execution import (
    _build_coverage,
    _execution_fixture,
)

TIMEOUT_REASON = "build timed out after 600 s (raise [flows.sim].build_timeout_ms)"
OOM_REMEDY = (
    "for Verilator, lower build parallelism with -j<N> in the Target's "
    "flow_options.make_options, or raise [sandbox].memory and recreate the Sandbox"
)
HEAD_LINE = "make: Entering directory '/work/build' HEADMARKER"
LAST_LINE = "last line before kill"


def _long_output() -> str:
    """Build output whose head must be dropped and whose tail must survive."""
    lines = [HEAD_LINE, "g++ " + "-I/very/long/include/path " * 80]
    lines += [f"compiling unit {index}" for index in range(40)]
    lines.append(LAST_LINE)
    return "\n".join(lines) + "\n"


def _timed_out_result(stdout: str | None = None) -> SubprocessResult:
    return SubprocessResult(
        returncode=-9,
        stdout=_long_output() if stdout is None else stdout,
        timed_out=True,
    )


def _timeout_outcome():
    return classify_build_outcome(_timed_out_result(), "abc123", timeout_s=600)


def test_timed_out_build_report_leads_with_timeout_limit_and_knob() -> None:
    result = _timed_out_result()
    outcome = classify_build_outcome(result, "abc123", timeout_s=600)

    assert outcome.reason == TIMEOUT_REASON
    assert outcome.output == result.stdout
    report = build_module.build_failure_report(outcome)
    assert report.splitlines()[0] == outcome.reason
    assert LAST_LINE in report
    assert HEAD_LINE not in report


@pytest.mark.parametrize(
    ("case", "stdout", "peak", "expected_peak"),
    [
        (
            "with_record",
            "gcc\nmake: *** Error 137\nBOOLEY_BUILD_STAGE token=abc123 rc=137\n",
            31744.4,
            "31744 MB",
        ),
        ("no_record", "gcc\nmake: *** Error 137\n", 31744.4, "31744 MB"),
        ("unknown_peak", "gcc\nmake: *** Error 137\n", None, "unknown"),
        (
            "design_diagnostic_present",
            "%Error: rtl/top.sv:4:3: syntax error\nmake: *** Error 137\n"
            "BOOLEY_BUILD_STAGE token=abc123 rc=1\n",
            31744.4,
            "31744 MB",
        ),
    ],
)
def test_oom_build_names_oom_evidence_and_remedy(
    case: str, stdout: str, peak: float | None, expected_peak: str
) -> None:
    del case
    outcome = classify_build_outcome(
        SubprocessResult(returncode=137, stdout=stdout, oom_kill_delta=1, peak_rss_mb=peak),
        "abc123",
        timeout_s=600,
    )

    peak_text = "build peak RSS unknown" if peak is None else f"build peak RSS {expected_peak}"
    assert outcome.reason == (
        f"build failed while the Sandbox recorded an OOM kill ({peak_text}; {OOM_REMEDY})"
    )
    assert outcome.failure_kind == "infrastructure"
    report = build_module.build_failure_report(outcome)
    assert report.splitlines()[0] == outcome.reason
    assert "Error 137" in report
    assert "BOOLEY_BUILD_STAGE" not in report


def test_oom_remedy_knob_disables_automatic_jobs() -> None:
    assert verilator_backend_arguments(_parallelism_inspection(modern=("-j2",))) == ()


def test_compile_error_still_shows_compiler_lines(tmp_path: Path) -> None:
    diagnostic = "%Error: rtl/top.sv:4:3: syntax error"
    outcome = classify_build_outcome(
        SubprocessResult(
            returncode=1, stdout=f"{diagnostic}\nBOOLEY_BUILD_STAGE token=abc123 rc=1\n"
        ),
        "abc123",
    )
    assert outcome.design_failed

    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    result = _run_execution(
        handle,
        prepared,
        lambda _command, timeout: SubprocessResult(
            returncode=1, stdout=f"{diagnostic}\nBOOLEY_BUILD_STAGE token=abc123 rc=1\n"
        ),
        ("smoke",),
        cocotb=False,
    )
    assert result.infrastructure_failure is None
    assert diagnostic in result.tests[0].error_tail


def _engine_timeout_outcome(tmp_path: Path):
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    return _run_execution(
        handle,
        prepared,
        lambda _command, timeout: _timed_out_result(),
        ("smoke",),
        cocotb=False,
        options=SimulationOptions(build_timeout_ms=600_000),
    )


def test_engine_build_timeout_outcome_is_reason_first(tmp_path: Path) -> None:
    outcome = _engine_timeout_outcome(tmp_path)

    failure = outcome.infrastructure_failure
    assert failure is not None
    assert failure.message == TIMEOUT_REASON
    assert failure.detail.splitlines()[0] == TIMEOUT_REASON
    assert HEAD_LINE not in failure.detail
    assert LAST_LINE in failure.detail
    assert outcome.builds[-1].timed_out


class _InfrastructureGroup:
    """Fake build group whose build ended as the real engine reported it."""

    artifact_paths: tuple[Path, ...] = ()

    def __init__(self, root: Path, outcome) -> None:
        self.build_root = root
        self._outcome = outcome

    def planning_disclosure(self):
        return {}

    def compile(self):
        return SimpleNamespace(passed=False)

    def finish_build_failure(self):
        return self._outcome


def _first_build_result(store: CampaignStore) -> dict[str, object]:
    (path,) = store.root.glob("**/build-result.json")
    return json.loads(path.read_text())


def _run_private_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome):
    monkeypatch.setattr(
        crash_matrix,
        "_FailedBuildGroup",
        lambda root, _infrastructure: _InfrastructureGroup(root, outcome),
    )
    executor, request, invocation = crash_matrix._failed_build_case(
        tmp_path, monkeypatch, "compile_spawn", crash_matrix._CrashOnce("never")
    )
    return executor, request, invocation


def _run_shared_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome):
    from tests.flows.sim.test_campaign_phase3 import _handle as shared_handle
    from tests.flows.sim.test_campaign_phase3 import _named_manifest

    plan = create_simulation_campaign_plan(_named_manifest(("tail", "quick")))
    project = tmp_path / "project"
    project.mkdir()
    handle = shared_handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    root = tmp_path / "engine-build"
    root.mkdir()

    class Execution:
        @contextmanager
        def ordinary_group(self, _handle, _names):
            yield _InfrastructureGroup(root, outcome)

    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: Execution(),  # type: ignore[arg-type,return-value]
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    request = NewCampaignRunRequest(
        plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
    )
    return executor, request, invocation


@pytest.mark.parametrize("path", ["shared", "private"])
def test_campaign_build_timeout_raises_typed_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    outcome = _engine_timeout_outcome(tmp_path / "engine")
    runner = _run_shared_timeout if path == "shared" else _run_private_timeout
    executor, request, invocation = runner(tmp_path, monkeypatch, outcome)

    with pytest.raises(build_module.SimulationBuildInfrastructureError) as caught:
        SimulationCampaign(executor).run(request)

    assert not isinstance(caught.value, SimulationCampaignIntegrityError)
    assert str(caught.value).splitlines()[0] == TIMEOUT_REASON
    store = CampaignStore(invocation / "targets" / "sim" / "campaign")
    result = _first_build_result(store)
    assert result["state"] == "infrastructure_error"
    observation = result["observation"]
    assert observation["message"] == TIMEOUT_REASON
    assert observation["detail"]["text"].splitlines()[0] == TIMEOUT_REASON
    assert store.scan().interrupted


def test_campaign_endpoint_reports_build_timeout_first(tmp_path: Path, capsys) -> None:
    outcome = _timeout_outcome()
    error = build_module.SimulationBuildInfrastructureError("sim", outcome)
    flow = SimulateFlow()
    invocation = tmp_path / "reports" / "1"
    invocation.mkdir(parents=True)
    with (
        patch.object(SimulateFlow, "reserve_invocation_dir", return_value=invocation),
        patch.object(SimulateFlow, "_write_campaign_progress"),
        patch.object(SimulateFlow, "_plan_campaign_baselines", return_value=([], {})),
        patch.object(SimulateFlow, "_candidate_campaign_requests", return_value=[]),
        patch.object(SimulateFlow, "_publish_and_run_campaign_requests", side_effect=error),
    ):
        endpoint = flow._run_ordinary_campaigns(["sim"], {"sim": []}, _admission())

    assert endpoint.exit_code == EXIT_ERROR
    first_line = endpoint.report_text.splitlines()[0]
    assert TIMEOUT_REASON in first_line
    assert endpoint.detail["eda_tool_error"] == "build_infrastructure"
    assert endpoint.detail["build_stage"]["timed_out"] is True
    assert LAST_LINE in endpoint.report_text
    assert HEAD_LINE not in endpoint.report_text
    del capsys


def test_legacy_build_infrastructure_result_uses_shared_tail(capsys) -> None:
    outcome = classify_build_outcome(
        _timed_out_result("BOOLEY_BUILD_STAGE token=stale rc=0\n" + _long_output()),
        "abc123",
        timeout_s=600,
    )
    error = build_module.SimulationBuildInfrastructureError("sim", outcome)

    result = SimulateFlow._build_infrastructure_result(error)

    assert TIMEOUT_REASON in result.report_text.splitlines()[0]
    assert "BOOLEY_BUILD_STAGE" not in result.report_text
    assert LAST_LINE in result.report_text
    capsys.readouterr()


def _timed_out_coverage_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    execution, target, _raw, _captured = _execution_fixture(tmp_path, monkeypatch)
    execution._options = SimulationOptions(build_timeout_ms=600_000)
    original = execution._invoke

    def invoke(command, *, timeout):
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            return _timed_out_result()
        return original(command, timeout=timeout)

    execution._invoke = invoke
    return _build_coverage(execution, target)


def test_coverage_build_timeout_is_infrastructure_with_reason_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = _timed_out_coverage_build(tmp_path, monkeypatch)

    assert build.success is False
    assert build.infrastructure_error is True
    assert build.reason == TIMEOUT_REASON
    assert build.output.splitlines()[0] == TIMEOUT_REASON


def test_coverage_build_timeout_reports_not_run_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from booley.flows.sim.coverage_invocation import (
        CoverageInvocationRequest,
        prepare_coverage_invocation,
    )
    from booley.flows.sim.coverage_transaction import run_coverage_target
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution, Progress

    # Separate roots: both fixtures write the same FuseSoC core.
    (tmp_path / "adapter").mkdir()
    (tmp_path / "coverage_project").mkdir()
    timed_out = _timed_out_coverage_build(tmp_path / "adapter", monkeypatch)
    tmp_path = tmp_path / "coverage_project"
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    class TimedOut(NativeExecution):
        def build(self, request):
            return timed_out

    outcome = run_coverage_target(plan, TimedOut(), Progress())

    assert outcome.exit_code == 2
    assert outcome.detail["passed"] is None
    assert outcome.detail["simulation"] == "not_run"
    assert outcome.detail["collection"] == "infrastructure_error"


def test_coverage_campaign_build_failure_message_is_reason(tmp_path: Path) -> None:
    failing = _FailedCoverageBuild(infrastructure=True)
    failing.build = lambda _request: SimulationBuildResult(  # type: ignore[method-assign]
        False,
        f"{TIMEOUT_REASON}\n--- output tail ---\n{LAST_LINE}",
        infrastructure_error=True,
        reason=TIMEOUT_REASON,
    )
    with pytest.raises(coverage_execution_module._CoverageAggregateError):
        _run_coverage_campaign(tmp_path, failing)

    store = CampaignStore(tmp_path / "reports/1/targets/sim_0/campaign")
    observation = _first_build_result(store)["observation"]
    assert observation["message"] == TIMEOUT_REASON
    assert LAST_LINE in observation["detail"]["text"]


def test_coverage_campaign_build_timeout_is_not_an_integrity_error(tmp_path: Path) -> None:
    failing = _FailedCoverageBuild(infrastructure=True)
    failing.build = lambda _request: SimulationBuildResult(  # type: ignore[method-assign]
        False, f"{TIMEOUT_REASON}\ntail", infrastructure_error=True, reason=TIMEOUT_REASON
    )
    with pytest.raises(coverage_execution_module._CoverageAggregateError) as caught:
        _run_coverage_campaign(tmp_path, failing)

    assert not isinstance(caught.value, SimulationCampaignIntegrityError)
    assert TIMEOUT_REASON in str(caught.value).splitlines()[0]
    store = CampaignStore(tmp_path / "reports/1/targets/sim_0/campaign")
    assert store.scan().interrupted


def test_reasonless_coverage_infrastructure_keeps_its_message(tmp_path: Path) -> None:
    failing = _FailedCoverageBuild(infrastructure=True)
    failing.build = lambda _request: SimulationBuildResult(  # type: ignore[method-assign]
        False, "coverage Verilator identity mismatch", infrastructure_error=True
    )
    with pytest.raises(coverage_execution_module._CoverageAggregateError):
        _run_coverage_campaign(tmp_path, failing)

    store = CampaignStore(tmp_path / "reports/1/targets/sim_0/campaign")
    message = _first_build_result(store)["observation"]["message"]
    assert message == "coverage Verilator identity mismatch"


def test_legacy_missing_tool_still_reported_as_missing_executable(tmp_path: Path) -> None:
    for preamble in ("", "x" * 3000 + "\n"):
        outcome = _run_execution(
            _handle(tmp_path),
            _prepared(_handle(tmp_path), cocotb=False),
            lambda _command, timeout, preamble=preamble: SubprocessResult(
                returncode=127, stdout=f"{preamble}/bin/sh: 1: verilator: not found\n"
            ),
            ("smoke",),
            cocotb=False,
        )
        with pytest.raises(MissingExecutableError) as caught:
            SimulateFlow()._project_execution_outcome(outcome)
        assert caught.value.binary == "verilator"


def test_build_report_fits_codec_detail_ceiling() -> None:
    hostile = (
        '\\"\t漢字' * 200
        + "\x1b[31mred\x1b[0m\x00\n"
        + "y" * 10_000
        + "\n"
        + "BOOLEY_BUILD_STAGE token=abc123 rc=137\n"
    )
    outcome = classify_build_outcome(
        SubprocessResult(returncode=137, stdout=hostile, oom_kill_delta=1, peak_rss_mb=31744.4),
        "abc123",
        timeout_s=600,
    )

    report = build_module.build_failure_report(outcome)

    assert len(canonical_json_bytes({"text": report})) <= MAX_DETAIL_BYTES
    assert "\x1b" not in report
    assert "\x00" not in report
    assert "BOOLEY_BUILD_STAGE" not in report


def test_ansi_prefixed_marker_line_is_dropped_from_tail() -> None:
    tail = build_module.build_output_tail(
        "compiling\n\x1b[31mBOOLEY_BUILD_STAGE token=abc rc=1\x1b[0m\nlast\n"
    )

    assert "BOOLEY_BUILD_STAGE" not in tail
    assert tail == "compiling\nlast"


def test_hostile_oom_build_output_publishes_and_decodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = (
        '\\"\t漢字' * 200
        + "\x1b[31mred\x1b[0m\x00\n"
        + "y" * 10_000
        + "\nBOOLEY_BUILD_STAGE token=abc123 rc=137\n"
    )
    handle = _handle(tmp_path / "engine")
    outcome = _run_execution(
        handle,
        _prepared(handle, cocotb=False),
        lambda _command, timeout: SubprocessResult(
            returncode=137, stdout=hostile, oom_kill_delta=1, peak_rss_mb=31744.4
        ),
        ("smoke",),
        cocotb=False,
    )
    executor, request, invocation = _run_shared_timeout(tmp_path, monkeypatch, outcome)

    with pytest.raises(build_module.SimulationBuildInfrastructureError):
        SimulationCampaign(executor).run(request)

    store = CampaignStore(invocation / "targets" / "sim" / "campaign")
    result = _first_build_result(store)
    text = result["observation"]["detail"]["text"]
    assert result["state"] == "infrastructure_error"
    assert "OOM kill" in text.splitlines()[0]
    assert "\x1b" not in text
    assert "BOOLEY_BUILD_STAGE" not in text
    assert len(canonical_json_bytes({"text": text})) <= MAX_DETAIL_BYTES
    assert store.scan().interrupted


def test_campaign_resume_failure_reports_build_timeout_first() -> None:
    error = build_module.SimulationBuildInfrastructureError("sim", _timeout_outcome())

    with patch("booley.flows.sim.flow._fresh_campaign_recovery_detail", return_value={}):
        endpoint = SimulateFlow()._campaign_resume_failure(
            SimpleNamespace(), SimpleNamespace(), Path(), error
        )

    assert endpoint.exit_code == EXIT_ERROR
    assert TIMEOUT_REASON in endpoint.report_text.splitlines()[0]
    assert endpoint.detail["eda_tool_error"] == "build_infrastructure"
    assert endpoint.detail["build_stage"]["timed_out"] is True
    assert LAST_LINE in endpoint.report_text
    assert HEAD_LINE not in endpoint.report_text


@pytest.mark.parametrize("executable", ["verilator_bin", "iverilog"])
def test_build_owned_loader_startup_is_infrastructure(executable):
    text = f"{executable}: error while loading shared libraries: libx.so: missing"
    outcome = classify_build_outcome(
        SubprocessResult(
            returncode=127, stdout=text + "\nBOOLEY_BUILD_STAGE token=abc123 rc=127\n"
        ),
        "abc123",
        timeout_s=600,
    )
    assert outcome.failure_kind == "infrastructure"
    assert "libx.so" in outcome.reason
