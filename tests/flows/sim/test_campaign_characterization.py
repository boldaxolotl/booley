"""Compatibility guardrails for the simulation execution refactor."""

from __future__ import annotations

import json
from collections.abc import Callable
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
    work_transport_identity,
    write_adapter_result,
)
from booley.flows.sim.backends import cocotb_results
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.build_session import SimulationBuildSession
from booley.flows.sim.campaign.codec import decode_simulation_result, encode_simulation_result
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.flow_planning import plan_ordinary_hdl_campaign
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
from booley.flows.sim.execution.contract import (
    SimulationArtifactEvidence,
    SimulationInfrastructureFailure,
)
from booley.flows.sim.trace_recipe import TraceMode
from booley.flows.test_runs import build_test_run
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.targets.domain import TargetHandle
from tests.flows.report_headline import final_report_omits_headline
from tests.flows.sim.campaign_test_support import _facts, _plan, _unmanaged
from tests.flows.sim.launched_work import launched_work
from tests.flows.sim.leased_session import canned_build_session
from tests.flows.sim.test_flow import SimulateFlow, _make_flow


@pytest.fixture()
def _leased_build_session():
    """Run canned-process compatibility tests against a leased build session."""
    with canned_build_session():
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
    _leased_build_session: None,
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
        flow.publication_resources,
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
    _leased_build_session: None,
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
        flow.publication_resources,
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
        "test_runs",
        "tests",
        "timestamp",
    }
    assert set(report["test_runs"][0]) == {
        "args",
        "env",
        "fingerprint",
        "grade",
        "name",
        "seed",
        "test",
        "variant",
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
        "fingerprint",
        "resources",
        "seed",
        "sva_errors",
        "test_run",
        "test_validated",
        "timed_out",
        "termination",
        "variant",
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
    first_timeout = False
    detail = "$readmemh: Cannot open memory.hex"
    cocotb_path: Path | None = None

    def _prepare_build(self, handle):
        prepared, trace = super()._prepare_build(handle)
        executable = prepared.build_root / "Vtop"
        executable.write_bytes(b"authenticated simulator image")
        executable.chmod(0o755)
        if self._cocotb:
            # The planned cocotb core copies its test module into the build.
            (prepared.build_root / "test_counter.py").write_text("# cocotb\n", encoding="utf-8")
        return prepared, trace

    def _invoke_process(self, command: list[str], *, timeout: int) -> SubprocessResult:
        del timeout
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            identity = self.identities[-1]
            return SubprocessResult(
                returncode=0,
                stdout=f"BOOLEY_BUILD_STAGE token={identity.attempt_token} rc=0\n",
            )
        identity = work_transport_identity(launched_work(command))
        assert identity is not None
        selected = identity.selected_tests
        detail = self.detail
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
                        "timeout"
                        if self.first_timeout and name == selected[0]
                        else "pass"
                        if self.first_pass and name == selected[0]
                        else "timeout"
                        if self.termination == "timeout"
                        else "fail",
                        detail=""
                        if self.first_pass and name == selected[0]
                        else "simulation timed out"
                        if self.first_timeout and name == selected[0]
                        else "earlier design failure"
                        if self.first_fail and name == selected[0]
                        else detail,
                        termination="completed"
                        if (self.first_pass or self.first_fail or self.first_timeout)
                        and name == selected[0]
                        else self.termination,
                        failure_kind=""
                        if (self.first_pass or self.first_fail or self.first_timeout)
                        and name == selected[0]
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


class _NativeAdapterExecution(_AdapterAbortExecution):
    """Native Target with no named Test: the adapter writes no per-test entry."""

    adapter_termination = "timeout"
    adapter_detail = "simulation exceeded the supervision timeout"
    process_result = SubprocessResult(returncode=-9, stdout="")

    def _prepare_build(self, handle):
        prepared, trace = super()._prepare_build(handle)
        # A native Verilator image is named after the toplevel, not `Vtop`.
        (prepared.build_root / "Vtop").rename(prepared.build_root / f"V{prepared.toplevel}")
        return prepared, trace

    def _invoke_process(self, command: list[str], *, timeout: int) -> SubprocessResult:
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            return super()._invoke_process(command, timeout=timeout)
        identity = work_transport_identity(launched_work(command))
        assert identity is not None
        if self.adapter_termination:
            write_adapter_result(
                identity,
                AdapterResult(
                    False,
                    False,
                    0,
                    (),
                    simulator_returncode=-9,
                    termination=self.adapter_termination,  # type: ignore[arg-type]
                    failure_kind=self.adapter_termination,  # type: ignore[arg-type]
                    detail=self.adapter_detail,
                ),
            )
        return self.process_result

    def _completed_group(self, *args, **kwargs):
        # Skip the cocotb artifact the parent harness attaches.
        return SimulationExecution._completed_group(self, *args, **kwargs)


def _run_campaign_result(tmp_path: Path, plan: Any, execution: Any) -> Any:
    """Run ``plan`` through the ordinary executor and return the scanned result."""
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
    # Publish encodes and scan decodes: the record round-trips by construction.
    assert decode_simulation_result(encode_simulation_result(result)) == result
    return result


def _native_execution(**fields: Any) -> _NativeAdapterExecution:
    execution = _NativeAdapterExecution(eda_tool="verilator", cocotb=False)
    for name, value in fields.items():
        setattr(execution, name, value)
    return execution


def _cocotb_execution(**fields: Any) -> _AdapterAbortExecution:
    execution = _AdapterAbortExecution(eda_tool="verilator", cocotb=True)
    for name, value in fields.items():
        setattr(execution, name, value)
    return execution


def _native_plan(tmp_path: Path) -> Any:
    """Plan a native Target that names no Test (the default selection)."""
    handle, inspection, preview = _facts(tmp_path, cocotb=False, preview_names=())
    _write_native_core(tmp_path)
    run_groups = ((build_test_run(None),),)
    preview = replace(preview, groups=((),), run_groups=run_groups, commands=(("run",),))
    return plan_ordinary_hdl_campaign(
        handle=handle,
        inspection=inspection,
        preview=preview,
        groups=((),),
        required_suite=(),
        revision="abc123",
        invocation_id=7,
        execution_id="7" * 32,
        trace=False,
    )


def _write_native_core(root: Path) -> None:
    """Author a native Verilator Target that names no Test."""
    (root / "dut.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:dut:1\n"
        "filesets:\n"
        "  tb:\n"
        "    files:\n"
        "      - tb.sv: {file_type: systemVerilogSource}\n"
        "targets:\n"
        "  sim:\n"
        "    filesets: [tb]\n"
        "    toplevel: tb\n"
        "    flow: sim\n"
        "    flow_options:\n"
        "      tool: verilator\n",
        encoding="utf-8",
    )


def _cocotb_plan(tmp_path: Path) -> Any:
    return _plan(tmp_path, kind="cocotb_batch", cocotb=True)


@pytest.mark.parametrize(
    ("make_plan", "execution", "state", "executions", "grade", "hint"),
    [
        pytest.param(
            _cocotb_plan,
            _cocotb_execution(termination="timeout", failure_kind="timeout"),
            "timeout",
            ["timeout", "timeout"],
            "fail",
            True,
            id="adapter-timeout-per-test",
        ),
        pytest.param(
            _native_plan,
            _native_execution(),
            "timeout",
            ["timeout"],
            "fail",
            True,
            id="adapter-timeout-native-default-selection",
        ),
        pytest.param(
            _native_plan,
            _native_execution(
                adapter_termination="",
                process_result=SubprocessResult(returncode=-9, stdout="", timed_out=True),
            ),
            "timeout",
            ["timeout"],
            "fail",
            True,
            id="wrapper-timeout",
        ),
        pytest.param(
            _native_plan,
            _native_execution(
                adapter_termination="", process_result=SubprocessResult(returncode=-11, stdout="")
            ),
            "crash",
            ["crash"],
            "fail",
            False,
            id="crash",
        ),
        pytest.param(
            _cocotb_plan,
            _cocotb_execution(
                termination="fatal_init", failure_kind="missing_input", first_timeout=True
            ),
            "aborted",
            ["aborted", "aborted"],
            "fail",
            True,
            id="timeout-with-non-infrastructure-abort",
        ),
    ],
)
def test_result_state_follows_observations(
    tmp_path: Path,
    make_plan: Callable[[Path], Any],
    execution: Any,
    state: str,
    executions: list[str],
    grade: str,
    hint: bool,
) -> None:
    result = _run_campaign_result(tmp_path, make_plan(tmp_path), execution)
    observations = result.document["observations"]
    assert result.document["state"] == state
    assert [item["execution"] for item in observations] == executions
    assert result.document["grade"] == grade
    assert all(item["failure_class"] == "design" for item in observations)
    assert ("--timeout-ms" in observations[0]["detail"]["reason"]) is hint


def test_timeout_next_to_infrastructure_abort_publishes_aborted_error(tmp_path: Path) -> None:
    """A timed-out test plus an infrastructure-aborted one is a Target-level failure.

    This is both the "mixed" and the "Target-level infrastructure failure with a
    timed-out test" shape: the engine sets the Target failure from the
    infrastructure-aborted test, so the result goes through the Target-level
    observation path. Booley's own cocotb adapter folds this shape; the case is
    characterization of the codec's abort-dominates rule.
    """
    execution = _cocotb_execution(
        termination="disk_budget", failure_kind="infrastructure", first_timeout=True
    )
    result = _run_campaign_result(tmp_path, _cocotb_plan(tmp_path), execution)
    first, second = result.document["observations"]
    assert result.document["state"] == "aborted"
    assert result.document["grade"] == "error"
    assert (first["execution"], first["failure_class"], first["functional"]) == (
        "aborted",
        "design",
        "fail",
    )
    assert first["assertions"] == "not_observed"
    assert first["cycle_count"] is None
    assert (second["execution"], second["failure_class"]) == ("aborted", "infrastructure")
    assert second["detail"]["termination"] == "disk_budget"


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
    assert decode_simulation_result(encode_simulation_result(result)) == result
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


@pytest.mark.parametrize("met", [True, False])
def test_standalone_legacy_simulation_final_report_is_run_local(
    tmp_path, _leased_build_session, met
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
        flow.publication_resources,
    ):
        outcome = flow._run()
        final_report_omits_headline(flow, outcome)
    assert flow.state.criteria["sim_pass_lite"].met is met
    assert "elab_pass_lite" not in flow.state.criteria
    assert flow.state._file_path is None


def test_campaign_scan_takes_a_published_infrastructure_verdict_as_published(tmp_path) -> None:
    test_infrastructure_guard_publishes_terminal_campaign(tmp_path, "disk_budget", False)
    store = CampaignStore(tmp_path / "reports/1/targets/sim/campaign")
    published = store.scan().items[0].result
    assert published is not None
    for reference in published.document["evidence"]:
        if reference["kind"] == "simulation_adapter_result":
            (attempt,) = (store.root / "work-items").glob("*/attempts/*")
            (attempt / reference["path"]).unlink()

    assert store.scan().items[0].result == published


@pytest.mark.parametrize(
    "backend_detail",
    [
        "Verilator simulation timed out (5s)",
        "Icarus simulation timed out (5s)",
        cocotb_results.TIMEOUT_ACTIVE_DETAIL,
    ],
)
def test_backend_timeout_on_campaign_path_preserves_detail_and_adds_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_detail: str
) -> None:
    """An adapter-reported timeout fails the run (exit 1) with its detail and the hint.

    Only the build and the process boundary are faked; planning, the Simulation
    Campaign, its store, acceptance and reporting are real. Verilator and Icarus
    timeouts use the native default-selection adapter shape (no per-test entry);
    the cocotb detail names its tests, so the adapter writes a per-test entry.
    """
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    cocotb = backend_detail == cocotb_results.TIMEOUT_ACTIVE_DETAIL
    if cocotb:
        _facts(tmp_path, cocotb=True)
        execution: Any = _cocotb_execution(
            termination="timeout", failure_kind="timeout", detail=backend_detail
        )
        tests = ("count", "reset")
    else:
        _write_native_core(tmp_path)
        (tmp_path / "tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")
        execution = _native_execution(adapter_detail=backend_detail)
        tests = ()
    monkeypatch.setattr(
        "booley.flows.sim.flow.OrdinaryHdlSerialExecutor",
        lambda **kwargs: OrdinaryHdlSerialExecutor(
            **kwargs, execution_factory=lambda _options: execution
        ),
    )
    argv = ["--work-dir", str(tmp_path), "--report-dir", str(tmp_path / "reports")]
    argv += ["--target", "sim"]
    for name in tests:
        argv += ["--test", name]
    flow = SimulateFlow()
    # Planning and execution share the fake build, so the planned source
    # disclosure matches what execution later prepares.
    flow._simulation_execution_override = execution
    result = flow.execute_cli(argv)

    assert result.exit_code == EXIT_FAILURE, result.outcome.report_text
    hint = "raise --timeout-ms or [flows.sim].timeout_ms if the test legitimately needs longer"
    timeout_line = next(
        line for line in result.outcome.report_text.splitlines() if backend_detail in line
    )
    assert f"{backend_detail} (rc=" in timeout_line
    assert hint in timeout_line
    campaign = CampaignStore(tmp_path / "reports/sim/1/targets/sim/campaign").scan()
    published = campaign.items[0].result
    assert published is not None
    assert published.document["state"] == "timeout"
    for observation in published.document["observations"]:
        assert observation["execution"] == "timeout"
        assert backend_detail in observation["detail"]["reason"]
        assert hint in observation["detail"]["reason"]
