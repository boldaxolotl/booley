"""Compatibility guardrails for the simulation execution refactor."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.build_session import SimulationBuildSession
from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
from booley.flows.sim.trace_recipe import TraceMode
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.targets.domain import TargetHandle
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
                stdout=(
                    "[SIM_RESULT] FAILED\n"
                    '[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
                ),
            ),
            EXIT_SUCCESS,
            "pass",
            0,
            id="authenticated-summary-overrides-fail-sentinel",
        ),
        pytest.param(
            SubprocessResult(
                returncode=0,
                stdout=(
                    "[SIM_RESULT] PASSED\n"
                    '[SIM_SUMMARY] {"passed":false,"sva_errors":2}\n'
                ),
            ),
            EXIT_FAILURE,
            "fail",
            2,
            id="dirty-sva-overrides-pass-sentinel",
        ),
        pytest.param(
            SubprocessResult(
                returncode=-9,
                stdout=(
                    "[SIM_RESULT] FAILED\n"
                    '[SIM_SUMMARY] {"passed":false,"sva_errors":3}\n'
                ),
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
            "[SIM_RESULT] PASSED\n"
            "[SIM_CYCLES] 21\n"
            '[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        ),
        duration_s=0.25,
    )
    with (
        patch("booley.flows.sim.flow._get_test_names", return_value={}),
        patch.object(SimulateFlow, "_flow_enabled", return_value=True),
        patch.object(flow, "_execute", return_value=process),
        patch.object(flow, "_compile_command_str", return_value=None),
        patch.object(flow, "_fileset_for_report", return_value=None),
    ):
        outcome = flow._run()

    report = json.loads(
        (tmp_path / "reports/sim/1/targets/lite/simulation.json").read_text(
            encoding="utf-8"
        )
    )
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
        "name",
        "passed",
        "phase_timings_s",
        "resources",
        "sva_errors",
        "test_validated",
        "timed_out",
        "verdict",
        "workload_fingerprint",
    }
    assert set(outcome.detail) == {
        "artifacts",
        "cycle_counts",
        "elaboration",
        "elapsed_s",
        "mode",
        "phase_timings_s",
        "resolution_s",
        "targets",
        "targets_passed",
    }
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
