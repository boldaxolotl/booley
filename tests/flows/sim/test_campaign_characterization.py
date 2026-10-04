"""Compatibility guardrails for the simulation execution refactor."""

from __future__ import annotations

import json
import shlex
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTransportIdentity,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.build_session import SimulationBuildSession
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
from booley.flows.sim.execution.contract import (
    SimulationArtifactEvidence,
    SimulationInfrastructureFailure,
)
from booley.flows.sim.trace_recipe import TraceMode
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.targets.domain import TargetHandle
from tests.flows.sim.test_campaign_phase5_adversarial import _plan, _unmanaged
from tests.flows.sim.test_flow import SimulateFlow, _make_flow


@pytest.fixture()
def _legacy_build_transport():
    """Scope the legacy canned-process path to compatibility tests only."""
    with patch.object(
        SimulationExecution,
        "_run_groups_with_session",
        lambda self, handle, groups: [self._run_group(handle, group) for group in groups],
    ):
        yield


@pytest.mark.parametrize(
    ("process", "exit_code", "verdict", "sva_errors"),
    [
        pytest.param(
            SubprocessResult(returncode=0, stdout="[SIM_RESULT] PASSED\n"),
            EXIT_SUCCESS,
            "pass",
            0,
            id="pass-sentinel",
        ),
        pytest.param(
            SubprocessResult(returncode=0, stdout="[SIM_RESULT] FAILED\n"),
            EXIT_FAILURE,
            "fail",
            0,
            id="fail-sentinel",
        ),
        pytest.param(
            SubprocessResult(
                returncode=0,
                stdout=('[SIM_RESULT] FAILED\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'),
            ),
            EXIT_SUCCESS,
            "pass",
            0,
            id="authenticated-summary-overrides-fail-sentinel",
        ),
        pytest.param(
            SubprocessResult(
                returncode=0,
                stdout=('[SIM_RESULT] PASSED\n[SIM_SUMMARY] {"passed":false,"sva_errors":2}\n'),
            ),
            EXIT_FAILURE,
            "fail",
            2,
            id="dirty-sva-overrides-pass-sentinel",
        ),
        pytest.param(
            SubprocessResult(
                returncode=-9,
                stdout=('[SIM_RESULT] FAILED\n[SIM_SUMMARY] {"passed":false,"sva_errors":3}\n'),
                timed_out=True,
                duration_s=600.0,
            ),
            EXIT_FAILURE,
            "timeout",
            3,
            id="timeout-overrides-fail-and-dirty-sva",
        ),
        pytest.param(
            SubprocessResult(
                returncode=1,
                stdout=(
                    "%Error: tb.sv:12: syntax error\n"
                    "ERROR: Verilator elaboration failed (rc=1)\n"
                    "[SIM_RESULT] PASSED\n"
                    '[SIM_SUMMARY] {"passed":false,"sva_errors":4}\n'
                ),
                duration_s=0.1,
            ),
            EXIT_FAILURE,
            "elab_error",
            0,
            id="elaboration-overrides-sentinel-and-dirty-sva",
        ),
        pytest.param(
            SubprocessResult(
                returncode=127,
                stdout=(
                    "/bin/sh: 1: verilator: not found\n"
                    "ERROR: Verilator elaboration failed (rc=127)\n"
                    "[SIM_RESULT] PASSED\n"
                    '[SIM_SUMMARY] {"passed":false,"sva_errors":5}\n'
                ),
            ),
            EXIT_ERROR,
            None,
            None,
            id="infrastructure-overrides-sentinel-and-dirty-sva",
        ),
    ],
)
def test_public_simulation_precedence_table(
    tmp_path: Path,
    _legacy_build_transport: None,
    process: SubprocessResult,
    exit_code: int,
    verdict: str | None,
    sva_errors: int | None,
) -> None:
    """Conflicting execution evidence reaches the established public exit."""
    flow = _make_flow(tmp_path, config="lite")
    with (
        patch("booley.flows.sim.flow._get_test_names", return_value={}),
        patch.object(SimulateFlow, "_flow_enabled", return_value=True),
        patch.object(flow, "_execute", return_value=process),
        flow.context.publication_resources,
    ):
        outcome = flow._run()

    assert outcome.exit_code == exit_code
    report_path = tmp_path / "reports/sim/1/targets/lite/simulation.json"
    if verdict is None:
        assert not report_path.exists()
        assert outcome.detail["eda_tool_error"] == "missing_executable"
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["tests"][0]["verdict"] == verdict
    assert report["tests"][0]["sva_errors"] == sva_errors


def test_compatibility_projection_and_endpoint_detail_shape(
    tmp_path: Path,
    _legacy_build_transport: None,
) -> None:
    """Freeze the established projection and endpoint values, not only keys."""
    flow = _make_flow(tmp_path, config="lite")
    process = SubprocessResult(
        returncode=0,
        stdout=(
            '[SIM_RESULT] PASSED\n[SIM_CYCLES] 21\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        ),
        duration_s=0.25,
    )
    with (
        patch("booley.flows.sim.flow._get_test_names", return_value={}),
        patch.object(SimulateFlow, "_flow_enabled", return_value=True),
        patch.object(flow, "_execute", return_value=process),
        patch.object(flow, "_compile_command_str", return_value=None),
        patch.object(flow, "_fileset_for_report", return_value=None),
        flow.context.publication_resources,
    ):
        outcome = flow._run()

    report = json.loads(
        (tmp_path / "reports/sim/1/targets/lite/simulation.json").read_text(encoding="utf-8")
    )
    _assert_projection_shapes(report, outcome)
    _assert_projection_values(report, outcome)


def _assert_projection_shapes(report, outcome) -> None:
    assert set(report) == {
        "artifacts",
        "build_stage",
        "complete",
        "eda_tool",
        "elapsed_s",
        "flow",
        "mode",
        "passed",
        "phase_timings_s",
        "target",
        "target_identity",
        "tb_top",
        "tests",
        "timestamp",
    }
    assert set(report["tests"][0]) == {
        "artifacts",
        "build_s",
        "cycle_observation",
        "cycles",
        "elapsed_s",
        "error_tail",
        "failure_kind",
        "name",
        "passed",
        "phase_timings_s",
        "resources",
        "sva_errors",
        "test_validated",
        "timed_out",
        "termination",
        "verdict",
        "workload_fingerprint",
    }
    _assert_endpoint_detail_shape(outcome)


def _assert_endpoint_detail_shape(outcome) -> None:
    assert set(outcome.detail) == {
        "artifacts",
        "cycle_counts",
        "elaboration",
        "elapsed_s",
        "mode",
        "phase_timings_s",
        "resolution_s",
        "pre_sim_runs",
        "pre_sim_lines",
        "targets",
        "targets_passed",
    }
    assert outcome.detail["pre_sim_runs"] == []
    assert outcome.detail["pre_sim_lines"] == []


def _assert_projection_values(report, outcome) -> None:
    test = report["tests"][0]
    assert report["flow"] == "sim"
    assert report["mode"] == "simulate"
    assert report["target"] == "lite"
    assert report["target_identity"] == "::lite:0#lite"
    assert report["complete"] is True
    assert report["passed"] is True
    assert test["name"] == "lite"
    assert test["passed"] is True
    assert test["verdict"] == "pass"
    assert test["cycles"] == 21
    assert test["cycle_observation"] == "legacy"
    assert outcome.exit_code == EXIT_SUCCESS
    assert outcome.criterion_key == "sim_pass_lite"
    assert outcome.criterion_met is True
    assert outcome.display_label == "target lite · test lite"
    assert outcome.detail["mode"] == "simulate"
    assert outcome.detail["targets"] == 1
    assert outcome.detail["targets_passed"] == 1
    assert outcome.detail["cycle_counts"] == [
        {
            "target": "lite",
            "target_identity": "::lite:0#lite",
            "test": "lite",
            "verdict": "pass",
            "cycles": 21,
            "observation": "legacy",
        }
    ]


class _BuildSessionBoundary(SimulationBuildSession):
    """Deterministic cache behind the production session orchestration."""

    def __init__(self, handle: TargetHandle, _variant: str) -> None:
        super().__init__(handle, _variant)
        self.cached: PreparedSimulationBuild | None = None
        self.cache_decision = "miss"

    def reusable_key(
        self,
        _prepared: PreparedSimulationBuild,
        _inputs: dict[str, str],
        *,
        hooks: bool,
    ) -> str:
        assert hooks is False
        return "stable-build-key"

    def try_reuse(
        self,
        _prepared: PreparedSimulationBuild,
        _key: str | None,
    ) -> PreparedSimulationBuild | None:
        self.cache_decision = "hit" if self.cached is not None else "miss"
        return self.cached

    def authorize_fresh_image(
        self,
        prepared: PreparedSimulationBuild,
        _inputs: dict[str, str],
        _key: str | None,
    ) -> None:
        self.cached = prepared


class _SessionBoundaryExecution(SimulationExecution):
    def __init__(self, *, eda_tool: str, cocotb: bool) -> None:
        self._eda_tool = eda_tool
        self._cocotb = cocotb
        self.identities = []
        super().__init__(invoke=self._invoke_process, options=SimulationOptions())

    def _prepare_build(self, handle: TargetHandle) -> tuple[PreparedSimulationBuild, TraceMode]:
        assert self._build_session is not None
        work_root = self._build_session.new_generation()
        build_root = work_root / "build"
        build_root.mkdir()
        resolved = ResolvedTarget(
            name=handle.selector,
            vlnv=handle.vlnv,
            toplevel="tb_demo",
            eda_tool=self._eda_tool,
            files=(),
            parameters={},
            build_root=build_root,
            edam_path=build_root / "demo.eda.yml",
            flow_options={"cocotb_module": "test_demo"} if self._cocotb else {},
            cocotb_module="test_demo" if self._cocotb else None,
        )
        prepared = PreparedSimulationBuild(
            handle.selector,
            handle.identity,
            resolved,
            work_root,
            build_root,
            self._eda_tool,
            "tb_demo",
            ("make", "-C", str(build_root)),
        )
        self._fresh_generation = work_root
        return prepared, TraceMode.VCD_FIFO

    def _prepare_attempt(self, *args: Any, **kwargs: Any):
        attempt = super()._prepare_attempt(*args, **kwargs)
        self.identities.append(attempt.identity)
        return attempt

    def _invoke_process(self, command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        identity = self.identities[-1]
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            return SubprocessResult(
                returncode=0,
                stdout=f"BOOLEY_BUILD_STAGE token={identity.attempt_token} rc=0\n",
            )
        write_adapter_result(
            identity,
            AdapterResult(
                True,
                False,
                0,
                identity.selected_tests,
                test_results=tuple(
                    AdapterTestResult(name, "pass") for name in identity.selected_tests
                ),
            ),
        )
        return SubprocessResult(returncode=0, stdout="adapter completed\n")


def _handle(root: Path) -> TargetHandle:
    return cast(
        TargetHandle,
        SimpleNamespace(
            project_root=root.resolve(),
            selector="sim",
            identity="acme:lib:demo:1#sim",
            vlnv="acme:lib:demo:1",
        ),
    )


@pytest.mark.parametrize(
    ("eda_tool", "cocotb", "expected_groups", "expected_builds", "expected_runs"),
    [
        pytest.param("icarus", False, 3, 1, 3, id="icarus-native-reuse"),
        pytest.param("verilator", False, 3, 1, 3, id="verilator-native-reuse"),
        pytest.param("icarus", True, 1, 1, 1, id="cocotb-batch"),
    ],
)
def test_leased_session_build_and_run_counts(
    tmp_path: Path,
    eda_tool: str,
    cocotb: bool,
    expected_groups: int,
    expected_builds: int,
    expected_runs: int,
) -> None:
    """Production grouping reuses one leased native build and batches Cocotb."""
    handle = _handle(tmp_path)
    execution = _SessionBoundaryExecution(eda_tool=eda_tool, cocotb=cocotb)
    commands: list[list[str]] = []
    invoke = execution._invoke

    def record(command: list[str], *, timeout: int) -> SubprocessResult:
        commands.append(command)
        return invoke(command, timeout=timeout)

    execution._invoke = record
    inspection = SimpleNamespace(
        toplevel="tb_demo",
        eda_tool=eda_tool,
        flow_options={"cocotb_module": "test_demo"} if cocotb else {},
    )
    inspection.inspect = lambda _handle: inspection
    with (
        patch(
            "booley.flows.sim.execution.engine.SimulationBuildSession",
            _BuildSessionBoundary,
        ),
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=inspection,
        ),
    ):
        outcome = execution.run(handle, NamedTests(("smoke", "stress", "edge")))

    build_commands = [command for command in commands if "BOOLEY_BUILD_STAGE" in command[-1]]
    run_commands = [command for command in commands if "BOOLEY_BUILD_STAGE" not in command[-1]]
    assert outcome.passed, outcome.infrastructure_failure
    assert len(outcome.builds) == expected_groups
    assert [build.ran for build in outcome.builds] == [True] + [False] * (expected_groups - 1)
    assert len(build_commands) == expected_builds
    assert len(run_commands) == expected_runs


class _AdapterAbortExecution(_SessionBoundaryExecution):
    termination = "fatal_init"
    failure_kind = "missing_input"
    first_pass = False
    first_fail = False
    cocotb_path: Path | None = None

    def _prepare_build(self, handle):
        prepared, trace = super()._prepare_build(handle)
        executable = prepared.build_root / "Vtop"
        executable.write_bytes(b"authenticated simulator image")
        executable.chmod(0o755)
        return prepared, trace

    def _invoke_process(self, command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            identity = self.identities[-1]
            return SubprocessResult(
                returncode=0,
                stdout=f"BOOLEY_BUILD_STAGE token={identity.attempt_token} rc=0\n",
            )
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
        detail = "$readmemh: Cannot open memory.hex"
        self.cocotb_path = identity.result_path.parent / "cocotb_results.json"
        self.cocotb_path.write_text(
            json.dumps(
                {
                    "state": "ok",
                    "detail": "",
                    "tests": [
                        {
                            "name": name,
                            "module": "test_counter",
                            "status": "pass"
                            if self.first_pass and name == selected[0]
                            else "fail",
                            "failure": ""
                            if self.first_pass and name == selected[0]
                            else "earlier design failure"
                            if self.first_fail and name == selected[0]
                            else f"{detail} (rc=7)",
                            "elapsed_s": 0.01,
                        }
                        for name in selected
                    ],
                    "skipped_unselected": 0,
                }
            ),
            encoding="utf-8",
        )
        write_adapter_result(
            identity,
            AdapterResult(
                False,
                False,
                0,
                selected,
                simulator_returncode=7,
                termination=self.termination,
                missing_input_path="memory.hex" if self.termination == "fatal_init" else "",
                failure_kind=self.failure_kind,
                detail=detail,
                test_results=tuple(
                    AdapterTestResult(
                        name,
                        "pass" if self.first_pass and name == selected[0] else "fail",
                        detail=""
                        if self.first_pass and name == selected[0]
                        else "earlier design failure"
                        if self.first_fail and name == selected[0]
                        else detail,
                        termination="completed"
                        if (self.first_pass or self.first_fail) and name == selected[0]
                        else self.termination,
                        failure_kind=""
                        if (self.first_pass or self.first_fail) and name == selected[0]
                        else self.failure_kind,
                    )
                    for name in selected
                ),
            ),
        )
        return SubprocessResult(returncode=0, stdout="adapter wrapper exited cleanly\n")

    def _completed_group(self, *args, **kwargs):
        outcome = super()._completed_group(*args, **kwargs)
        assert self.cocotb_path is not None
        artifact = SimulationArtifactEvidence(
            "cocotb_results_json",
            str(self.cocotb_path),
            self.cocotb_path.stat().st_size,
            tuple(test.name for test in outcome.tests),
        )
        return replace(outcome, artifacts=(*outcome.artifacts, artifact))


def test_adapter_termination_reaches_normalized_campaign_result(tmp_path: Path) -> None:
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    execution = _AdapterAbortExecution(eda_tool="verilator", cocotb=True)
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: execution,  # type: ignore[arg-type,return-value]
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    outcome = SimulationCampaign(executor).run(
        NewCampaignRunRequest(
            plan, tmp_path, invocation.parent, CampaignPolicy(), invocation, _unmanaged()
        )
    )

    assert outcome.complete is True
    store = CampaignStore(invocation / "targets/sim/campaign")
    result = store.scan().items[0].result
    assert result is not None
    observation = result.document["observations"][0]
    assert result.document["state"] == "aborted"
    assert observation["execution"] == "aborted"
    assert observation["failure_class"] == "design"
    assert observation["functional"] == "fail"
    assert observation["detail"] == {"reason": "$readmemh: Cannot open memory.hex (rc=7)"}


@pytest.mark.parametrize(
    "termination", ["disk_budget", "sim_time_stall", "trace_stall", "fatal_init"]
)
@pytest.mark.parametrize("first_pass", [False, True, "fail"])
def test_infrastructure_guard_publishes_terminal_campaign(
    tmp_path: Path, termination: str, first_pass: bool | str
) -> None:
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    execution = _AdapterAbortExecution(eda_tool="verilator", cocotb=True)
    execution.termination = termination
    execution.failure_kind = "infrastructure"
    execution.first_pass = first_pass is True
    execution.first_fail = first_pass == "fail"
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,
        execution_factory=lambda _options: execution,
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    outcome = SimulationCampaign(executor).run(
        NewCampaignRunRequest(
            plan, tmp_path, invocation.parent, CampaignPolicy(), invocation, _unmanaged()
        )
    )
    assert outcome.complete
    result = CampaignStore(invocation / "targets/sim/campaign").scan().items[0].result
    assert result is not None
    assert result.document["state"] == "aborted"
    assert result.document["grade"] == "error"
    for index, observation in enumerate(result.document["observations"]):
        if first_pass and index == 0:
            assert observation["execution"] == "completed"
            assert observation["functional"] == ("fail" if first_pass == "fail" else "pass")
            continue
        assert observation["execution"] == "aborted"
        assert observation["failure_class"] == "infrastructure"
        assert observation["functional"] == observation["assertions"] == "not_observed"
        assert observation["cycle_count"] is None
        assert observation["detail"]["termination"] == termination


class _NoVerdictExecution(_AdapterAbortExecution):
    def _completed_group(self, *args, **kwargs):
        outcome = super()._completed_group(*args, **kwargs)
        return replace(
            outcome,
            tests=(),
            infrastructure_failure=SimulationInfrastructureFailure(
                "adapter_protocol", "simulator returned no verdict", detail="界" * 4000
            ),
            artifacts=(),
        )


@pytest.mark.parametrize("count", [2, 1000])
def test_infrastructure_without_tests_retains_selected_identities(
    tmp_path: Path, count: int
) -> None:
    names = ("count", "reset") if count == 2 else tuple(f"test_{index}" for index in range(count))
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True, names=names)
    execution = _NoVerdictExecution(eda_tool="verilator", cocotb=True)
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None, execution_factory=lambda _options: execution
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    outcome = SimulationCampaign(executor).run(
        NewCampaignRunRequest(
            plan, tmp_path, invocation.parent, CampaignPolicy(), invocation, _unmanaged()
        )
    )
    assert outcome.complete
    result = CampaignStore(invocation / "targets/sim/campaign").scan().items[0].result
    assert result is not None
    assert result.document["grade"] == "error"
    assert [item["test"] for item in result.document["observations"]] == list(names)
    assert all(item["functional"] == "not_observed" for item in result.document["observations"])


def test_cleanup_failure_preserves_completed_tests_and_guard(tmp_path, monkeypatch) -> None:
    unlink = Path.unlink

    def denied_partial(path, *args, **kwargs):
        if path.name.endswith(".partial"):
            raise OSError("partial cleanup denied")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_partial)
    test_infrastructure_guard_publishes_terminal_campaign(tmp_path, "disk_budget", True)
    store = CampaignStore(tmp_path / "reports/1/targets/sim/campaign")
    result = store.scan().items[0].result
    assert result is not None
    assert any(
        item["code"] == "artifact_persistence" and "cleanup denied" in item["message"]
        for item in result.document["diagnostics"]
    )


def test_unfiltered_guard_without_discovered_tests_is_scannable(tmp_path) -> None:
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True, names=())
    execution = _AdapterAbortExecution(eda_tool="verilator", cocotb=True)
    execution.termination = "sim_time_stall"
    execution.failure_kind = "infrastructure"
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None, execution_factory=lambda _options: execution
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    outcome = SimulationCampaign(executor).run(
        NewCampaignRunRequest(
            plan, tmp_path, invocation.parent, CampaignPolicy(), invocation, _unmanaged()
        )
    )
    assert outcome.complete
    result = CampaignStore(invocation / "targets/sim/campaign").scan().items[0].result
    assert result is not None
    assert result.document["grade"] == "error"
    assert result.document["observations"][0]["test"] is None
    assert result.document["observations"][0]["detail"]["termination"] == "sim_time_stall"


def test_campaign_scan_rejects_adapter_guard_for_foreign_target(tmp_path) -> None:
    import hashlib

    from booley.flows.sim.campaign.codec import (
        SimulationCampaignIntegrityError,
        canonical_json_bytes,
    )

    test_infrastructure_guard_publishes_terminal_campaign(tmp_path, "disk_budget", False)
    store = CampaignStore(tmp_path / "reports/1/targets/sim/campaign")
    (result_path,) = store.root.glob("work-items/*/result.json")
    document = json.loads(result_path.read_bytes())
    (attempt,) = result_path.parent.glob("attempts/*")
    reference = next(
        item for item in document["evidence"] if item["kind"] == "simulation_adapter_result"
    )
    source_path = attempt / reference["path"]
    source = json.loads(source_path.read_bytes())
    source["target_identity"] = "foreign#sim"
    raw = json.dumps(source, separators=(",", ":")).encode()
    source_path.write_bytes(raw)
    reference["bytes"] = len(raw)
    reference["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    result_path.write_bytes(canonical_json_bytes(document))
    with pytest.raises(SimulationCampaignIntegrityError, match="Target disagrees"):
        store.scan()


@pytest.mark.parametrize("met", [True, False])
def test_standalone_legacy_simulation_final_report_is_run_local(
    tmp_path, _legacy_build_transport, met
):
    flow = _make_flow(tmp_path, config="lite")
    flow.args.state_file = None
    flow.read_state()
    process = SubprocessResult(
        returncode=0, stdout=f"[SIM_RESULT] {'PASSED' if met else 'FAILED'}\n"
    )
    with (
        patch("booley.flows.sim.flow._get_test_names", return_value={}),
        patch.object(SimulateFlow, "_flow_enabled", return_value=True),
        patch.object(flow, "_execute", return_value=process),
        flow.context.publication_resources,
    ):
        outcome = flow._run()
        from booley.flows.endpoint_report_criteria import freeze

        freeze(flow.context)
        path = flow.context.write_report(outcome)
    from booley.flows.endpoint_report_criteria import freeze

    freeze(flow.context)
    report = json.loads(path.read_text())
    assert (report["criterion_key"], report["criterion_met"]) == ("sim_pass_lite", met)
    assert "elab_pass_lite" not in flow.state.criteria
    assert flow.state._file_path is None
