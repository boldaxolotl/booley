"""Focused contracts for Simulation's elaboration-only mode."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.sim.build import (
    BuildOutcome,
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    build_stage_script,
    classify_build_outcome,
)
from booley.flows.sim.flow import ElabOnlyTargetResult, SimulateFlow, TargetResult
from booley.flows.sim.flow import TestResult as SimTestResult
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.mcp.schema_extractor import extract_schema


def _result(output: str, *, rc: int = 0, **kwargs: object) -> SubprocessResult:
    return SubprocessResult(returncode=rc, stdout=output, **kwargs)


def _flow_with_state(tmp_path: Path, targets: list[str]) -> SimulateFlow:
    state_file = tmp_path / "state.json"
    state = DevelopmentState.load(state_file)
    state.init_criteria({f"elab_pass_{target}": True for target in targets})
    state.save()
    env = os.environ.copy()
    env.update(
        BOOLEY_SLUG="elab-only-test",
        BOOLEY_STATE_FILE=str(state_file),
    )
    flow = SimulateFlow()
    with patch.dict(os.environ, env):
        flow.parse_args(
            [
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(tmp_path / "reports"),
                "--target",
                ",".join(targets),
                "--elab-only",
            ]
        )
    flow.read_state()
    return flow


def test_elab_only_and_build_only_share_one_destination(tmp_path: Path) -> None:
    canonical = SimulateFlow()
    canonical.parse_args(["--work-dir", str(tmp_path), "--target", "sim_dut", "--elab-only"])
    alias = SimulateFlow()
    alias.parse_args(["--work-dir", str(tmp_path), "--target", "sim_dut", "--build-only"])

    assert canonical.args.elab_only is True
    assert alias.args.elab_only is True
    assert "--elab-only, --build-only" in canonical._parser.format_help()


def test_mcp_schema_exposes_only_canonical_property_and_description() -> None:
    flow = SimulateFlow()
    schema = extract_schema(flow._parser)

    assert schema["properties"]["elab_only"]["type"] == "boolean"
    assert "build_only" not in schema["properties"]
    assert "elab_only=true" in flow.description
    assert "--elab-only" in flow.description
    assert "--build-only" in flow.description


@pytest.mark.parametrize(
    ("extra", "argument"),
    [
        (["--test", "smoke"], "--test"),
        (["--skip", "slow"], "--skip"),
        (["--trace"], "--trace"),
        (["--result-verbosity", "full"], "--result-verbosity full"),
        (["--no-kill"], "--no-kill"),
    ],
)
def test_elab_only_rejects_run_stage_arguments(
    tmp_path: Path, extra: list[str], argument: str
) -> None:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim_dut", "--elab-only", *extra])

    result = flow._validate_mode_args()

    assert result is not None
    assert result.exit_code == EXIT_ERROR
    assert argument in result.report_text
    assert "omit --elab-only" in result.report_text


def test_standalone_requires_elab_only(tmp_path: Path) -> None:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim_dut", "--standalone"])

    result = flow._validate_mode_args()

    assert result is not None
    assert result.exit_code == EXIT_ERROR
    assert "requires --elab-only" in result.report_text


def test_build_stage_script_emits_record_before_run_half() -> None:
    script = build_stage_script(["make", "-C", "build"], "abc123", run_line="run-sim")

    assert script.index("make -C build") < script.index("token=abc123")
    assert script.index("token=abc123") < script.index("run-sim")
    assert "ERROR: Verilator elaboration failed" not in script


def test_build_stage_script_measures_both_halves_and_preserves_run_exit() -> None:
    script = build_stage_script(["true"], "abc123", run_line="sh -c 'exit 7'")

    completed = subprocess.run(
        ["sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 7
    assert re.search(
        r"^BOOLEY_BUILD_STAGE token=abc123 rc=0 duration_ms=\d+$",
        completed.stdout,
        re.MULTILINE,
    )
    assert re.search(
        r"^BOOLEY_RUN_STAGE token=abc123 rc=7 duration_ms=\d+$",
        completed.stdout,
        re.MULTILINE,
    )


def test_classifier_accepts_one_matching_success_record() -> None:
    outcome = classify_build_outcome(
        _result("compiler output\nBOOLEY_BUILD_STAGE token=abc123 rc=0\nruntime failed", rc=1),
        "abc123",
    )

    assert outcome.passed
    assert outcome.failure_kind is None


def test_classifier_uses_authenticated_build_duration() -> None:
    outcome = classify_build_outcome(
        _result(
            "compiler output\nBOOLEY_BUILD_STAGE token=abc123 rc=0 duration_ms=17\nruntime output",
            rc=0,
            duration_s=4.0,
        ),
        "abc123",
    )

    assert outcome.elapsed_s == 0.017


def test_classifier_rejects_spoofed_or_duplicate_records() -> None:
    spoofed = classify_build_outcome(
        _result("BOOLEY_BUILD_STAGE token=wrong rc=0\n", rc=0),
        "abc123",
    )
    duplicate = classify_build_outcome(
        _result(
            "BOOLEY_BUILD_STAGE token=abc123 rc=0\nBOOLEY_BUILD_STAGE token=abc123 rc=0\n",
            rc=0,
        ),
        "abc123",
    )

    assert spoofed.verdict is None
    assert duplicate.verdict is None
    assert spoofed.failure_kind == duplicate.failure_kind == "infrastructure"


def test_classifier_distinguishes_design_rejection_from_ambiguous_exit() -> None:
    design = classify_build_outcome(
        _result("%Error: rtl/top.sv:4: syntax error\nBOOLEY_BUILD_STAGE token=abc123 rc=1", rc=1),
        "abc123",
    )
    ambiguous = classify_build_outcome(
        _result("make stopped\nBOOLEY_BUILD_STAGE token=abc123 rc=1", rc=1),
        "abc123",
    )

    assert design.design_failed
    assert ambiguous.verdict is None
    assert ambiguous.failure_kind == "infrastructure"


def test_classifier_reads_design_diagnostics_from_stderr() -> None:
    outcome = classify_build_outcome(
        _result(
            "BOOLEY_BUILD_STAGE token=abc123 rc=1\n",
            rc=1,
            stderr="%Error: rtl/top.sv:4: syntax error\n",
        ),
        "abc123",
    )

    assert outcome.design_failed
    assert outcome.failure_kind == "design"


def test_timeout_before_terminal_record_has_no_verdict() -> None:
    outcome = classify_build_outcome(
        _result("still compiling", rc=-1, timed_out=True),
        "abc123",
    )

    assert outcome.verdict is None
    assert outcome.failure_kind == "infrastructure"


def test_signal_style_build_exit_has_no_design_verdict() -> None:
    outcome = classify_build_outcome(
        _result(
            "%Error: rtl/top.sv:4: syntax error\nBOOLEY_BUILD_STAGE token=abc123 rc=139",
            rc=139,
        ),
        "abc123",
    )

    assert outcome.verdict is None
    assert outcome.failure_kind == "infrastructure"


def test_timeout_after_success_record_preserves_build_pass() -> None:
    outcome = classify_build_outcome(
        _result(
            "BOOLEY_BUILD_STAGE token=abc123 rc=0\nruntime still active",
            rc=-1,
            timed_out=True,
        ),
        "abc123",
    )

    assert outcome.passed


def test_campaign_continues_and_applies_error_fail_pass_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = ["clean", "broken", "infra"]
    flow = _flow_with_state(tmp_path, targets)
    previous_infra_state = flow.state.criteria["elab_pass_infra"].met
    calls: list[str] = []
    outcomes = {
        "clean": BuildOutcome(True, "pass", None, output="ok"),
        "broken": BuildOutcome(
            True,
            "fail",
            "design",
            output="%Error: rtl/top.sv:1: syntax error",
            reason="compiler rejected design",
        ),
        "infra": BuildOutcome(
            False,
            None,
            "infrastructure",
            output="spawn failed",
            reason="setup failed",
        ),
    }
    monkeypatch.setattr(flow, "_resolve_requested_targets", lambda: targets)
    monkeypatch.setattr(flow, "_validate_interactive_args", lambda selected: None)
    monkeypatch.setattr(flow, "_standalone_requested", lambda: False)

    def run_one(target: str) -> ElabOnlyTargetResult:
        calls.append(target)
        return ElabOnlyTargetResult(target=target, outcome=outcomes[target])

    monkeypatch.setattr(flow, "_run_one_elab_only", run_one)

    result = flow._run_elab_only()

    assert result.exit_code == EXIT_ERROR
    assert calls == targets
    assert flow.state.criteria["elab_pass_clean"].met is True
    assert flow.state.criteria["elab_pass_broken"].met is False
    assert flow.state.criteria["elab_pass_infra"].met is previous_infra_state
    assert result.detail["mode"] == "elab_only"
    assert [entry["failure_class"] for entry in result.detail["targets"]] == [
        None,
        "design",
        "infrastructure",
    ]
    progress = next((tmp_path / "reports" / "sim").glob("*/progress.json"))
    assert '"mode": "elab_only"' in progress.read_text(encoding="utf-8")
    assert '"complete": true' in progress.read_text(encoding="utf-8")
    for target in targets:
        report = tmp_path / "reports" / f"sim_{target}.json"
        assert '"mode": "elab_only"' in report.read_text(encoding="utf-8")


def test_missing_executable_is_typed_in_elab_only_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _flow_with_state(tmp_path, ["sim_dut"])
    monkeypatch.setattr(flow, "_resolve_requested_targets", lambda: ["sim_dut"])
    monkeypatch.setattr(flow, "_validate_interactive_args", lambda selected: None)
    monkeypatch.setattr(flow, "_standalone_requested", lambda: False)
    monkeypatch.setattr(
        flow,
        "_run_one_elab_only",
        lambda target: ElabOnlyTargetResult(
            target=target,
            outcome=BuildOutcome(
                False,
                None,
                "infrastructure",
                output=(
                    "/bin/sh: 1: verilator: not found\nmake: *** [Makefile:8: Vtop.mk] Error 127\n"
                ),
                reason="nonzero build exit without a recognized design diagnostic",
            ),
        ),
    )

    result = flow._run_elab_only()

    assert result.exit_code == EXIT_ERROR
    assert result.detail["mode"] == "elab_only"
    assert result.detail["eda_tool_error"] == "missing_executable"
    assert result.detail["missing_executable"] == "verilator"
    assert "required executable 'verilator'" in result.report_text


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([BuildOutcome(True, "pass", None)], EXIT_SUCCESS),
        ([BuildOutcome(True, "fail", "design")], EXIT_FAILURE),
        (
            [
                BuildOutcome(True, "fail", "design"),
                BuildOutcome(False, None, "infrastructure"),
            ],
            EXIT_ERROR,
        ),
    ],
)
def test_campaign_exit_precedence(
    outcomes: list[BuildOutcome],
    expected: int,
) -> None:
    results = [
        ElabOnlyTargetResult(target=str(index), outcome=outcome)
        for index, outcome in enumerate(outcomes)
    ]

    assert SimulateFlow._elab_only_exit_code(results) == expected


def test_one_target_executes_only_authenticated_make_and_archives_complete_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _flow_with_state(tmp_path, ["sim_dut"])
    prepared = PreparedSimulationBuild(
        target="sim_dut",
        target_identity="::dut:0#sim_dut",
        resolved=MagicMock(),
        work_root=tmp_path / ".booley_project/.runtime/edalize/sim/sim_dut",
        build_root=tmp_path / ".booley_project/.runtime/edalize/sim/sim_dut/inner",
        eda_tool="verilator",
        toplevel="tb_dut",
        make_argv=("make", "-C", "cache"),
        environment={"SIM_MODE": "build"},
        fileset={"rtl": ("rtl/dut.sv",), "tb": ("tb/tb_dut.sv",)},
    )
    monkeypatch.setattr("booley.flows.sim.flow.prepare_simulation_build", lambda *a, **k: prepared)
    monkeypatch.setattr(flow, "_target_handle", lambda _target: MagicMock())
    monkeypatch.setattr(flow, "_target_sim_env", lambda target: {})
    monkeypatch.setattr(flow, "_effective_timeout_ms", lambda: 7000)
    captured: list[list[str]] = []

    def execute(command: list[str], *, timeout: int) -> SubprocessResult:
        captured.append(command)
        assert timeout == 7
        token = re.search(r"token=([0-9a-f]+)", command[2])
        assert token is not None
        output = f"complete compiler output\nBOOLEY_BUILD_STAGE token={token.group(1)} rc=0\n"
        return SubprocessResult(returncode=0, stdout=output)

    monkeypatch.setattr(flow, "_execute_boundary", execute)

    result = flow._run_one_elab_only("sim_dut")

    assert result.outcome.passed
    assert captured[0][:2] == ["sh", "-c"]
    script = captured[0][2]
    assert "make -C cache" in script
    assert "SIM_MODE" in script
    assert "cocotb" not in script.lower()
    assert "pre_run" not in script.lower()
    assert result.log_path
    log = tmp_path / result.log_path
    assert log.read_text(encoding="utf-8") == result.outcome.output


def test_setup_failure_archives_current_error_without_reusing_old_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _flow_with_state(tmp_path, ["sim_dut"])
    old = tmp_path / "reports/sim/1/artifacts/sim_sim_dut/run.log"
    old.parent.mkdir(parents=True)
    old.write_text("old passing output", encoding="utf-8")
    monkeypatch.setattr(flow, "_target_sim_env", lambda target: {})

    def fail_setup(*args: object, **kwargs: object) -> PreparedSimulationBuild:
        raise SimulationBuildPreparationError("current setup exploded")

    monkeypatch.setattr("booley.flows.sim.flow.prepare_simulation_build", fail_setup)
    monkeypatch.setattr(flow, "_target_handle", lambda _target: MagicMock())

    result = flow._run_one_elab_only("sim_dut")

    assert result.outcome.verdict is None
    assert result.outcome.failure_kind == "infrastructure"
    assert result.log_path
    current = (tmp_path / result.log_path).read_text(encoding="utf-8")
    assert "current setup exploded" in current
    assert "old passing output" not in current


def test_elab_only_branch_skips_test_and_cocotb_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _flow_with_state(tmp_path, ["sim_dut"])
    monkeypatch.setattr(flow, "_resolve_requested_targets", lambda: ["sim_dut"])
    monkeypatch.setattr(flow, "_validate_interactive_args", lambda selected: None)
    monkeypatch.setattr(flow, "_standalone_requested", lambda: False)
    monkeypatch.setattr(
        flow,
        "_run_one_elab_only",
        lambda target: ElabOnlyTargetResult(
            target=target,
            outcome=BuildOutcome(True, "pass", None),
        ),
    )
    monkeypatch.setattr(
        flow,
        "_resolve_run_targets",
        lambda: pytest.fail("test discovery ran in elab-only mode"),
    )
    monkeypatch.setattr(
        flow,
        "_validate_cocotb_targets",
        lambda targets: pytest.fail("Cocotb validation ran in elab-only mode"),
    )

    result = flow._run()

    assert result.exit_code == EXIT_SUCCESS


@pytest.mark.parametrize(
    ("outcomes", "prior", "expected"),
    [
        ([BuildOutcome(True, "pass", None)], False, True),
        ([BuildOutcome(True, "fail", "design")], True, False),
        (
            [
                BuildOutcome(False, None, "infrastructure"),
                BuildOutcome(True, "fail", "design"),
            ],
            True,
            False,
        ),
        ([BuildOutcome(False, None, "infrastructure")], True, True),
        ([], True, True),
    ],
)
def test_full_sim_records_only_authenticated_build_verdicts(
    tmp_path: Path,
    outcomes: list[BuildOutcome],
    prior: bool,
    expected: bool,
) -> None:
    flow = _flow_with_state(tmp_path, ["sim_dut"])
    flow.state.criteria["elab_pass_sim_dut"].met = prior
    tests = [
        SimTestResult(
            name=f"attempt-{index}",
            passed=False,
            build_outcome=outcome,
        )
        for index, outcome in enumerate(outcomes)
    ]

    flow._record_elab_criterion(TargetResult(target="sim_dut", tests=tests))

    assert flow.state.criteria["elab_pass_sim_dut"].met is expected
