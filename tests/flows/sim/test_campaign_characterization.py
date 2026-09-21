"""Compatibility guardrails for the Simulation Campaign refactor.

These tests intentionally describe the pre-campaign implementation.  In
particular, the build-count assertions freeze known inefficiencies which later
campaign phases are expected to remove.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.execution import SimulationExecution
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from tests.flows.sim.test_cocotb import (
    _TESTS,
    _cocotb_output,
    _fake_resolved,
    _make_cocotb_flow,
)
from tests.flows.sim.test_flow import SimulateFlow, _make_flow


@pytest.fixture(autouse=True)
def _use_legacy_build_transport_for_characterization():
    """Canned builds in this module predate the leased generation protocol."""
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
                stdout="partial output\n",
                timed_out=True,
                duration_s=600.0,
            ),
            EXIT_FAILURE,
            "timeout",
            0,
            id="timeout",
        ),
        pytest.param(
            SubprocessResult(
                returncode=1,
                stdout=(
                    "%Error: tb.sv:12: syntax error\n"
                    "ERROR: Verilator elaboration failed (rc=1)\n"
                ),
                duration_s=0.1,
            ),
            EXIT_FAILURE,
            "elab_error",
            0,
            id="elaboration-rejection",
        ),
        pytest.param(
            SubprocessResult(
                returncode=127,
                stdout=(
                    "/bin/sh: 1: verilator: not found\n"
                    "ERROR: Verilator elaboration failed (rc=127)\n"
                ),
            ),
            EXIT_ERROR,
            None,
            None,
            id="missing-eda-infrastructure",
        ),
    ],
)
def test_public_simulation_precedence_table(
    tmp_path: Path,
    process: SubprocessResult,
    exit_code: int,
    verdict: str | None,
    sva_errors: int | None,
) -> None:
    """Sentinel, SVA, timeout, and elaboration precedence reaches public exits."""
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


def test_compatibility_projection_and_endpoint_detail_shape(tmp_path: Path) -> None:
    """Freeze the established simulation.json and endpoint-detail contracts."""
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


@pytest.mark.parametrize("eda_tool", ["icarus", "verilator"])
def test_ordinary_hdl_currently_builds_once_per_test(
    tmp_path: Path,
    eda_tool: str,
) -> None:
    """Known Phase-0 inefficiency: native tests each execute their own build."""
    flow = _make_flow(tmp_path, config="lite")
    flow._boundary_eda_tool = eda_tool
    calls: list[list[str]] = []

    def execute(command: list[str]) -> SubprocessResult:
        calls.append(command)
        return SubprocessResult(returncode=0, stdout="[SIM_RESULT] PASSED\n")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"lite": ["smoke", "stress", "edge"]},
        ),
        patch.object(SimulateFlow, "_flow_enabled", return_value=True),
        patch.object(flow, "_execute", side_effect=execute),
    ):
        outcome = flow._run()

    assert outcome.exit_code == EXIT_SUCCESS
    assert len(calls) == 3


def test_cocotb_currently_builds_once_for_the_batch(tmp_path: Path) -> None:
    """Cocotb's existing batch has one composite build/run invocation."""
    flow = _make_cocotb_flow(tmp_path)
    calls: list[list[str]] = []
    output = _cocotb_output([(name, "pass", "") for name in _TESTS["ccfg"]])

    def execute(command: list[str]) -> SubprocessResult:
        calls.append(command)
        return SubprocessResult(returncode=0, stdout=output)

    with (
        patch("booley.flows.sim.flow._get_test_names", return_value=dict(_TESTS)),
        patch(
            "booley.fusesoc.fusesoc_registry._resolve_target",
            return_value=_fake_resolved(tmp_path),
        ),
        patch.object(SimulateFlow, "_execute", side_effect=execute),
    ):
        outcome = flow._run()

    assert outcome.exit_code == EXIT_SUCCESS
    assert len(calls) == 1
