"""Pre-Run Commands tests (ADR 0039 — [flows.sim].pre_run_commands).

Covers the env contract, firing order (per-test on the HDL loop vs once per
cocotb batch), failure isolation (a failed pre-run is a failed TestResult and
the loop continues — never exit 2), marker attribution, dry-run parity, and
the shared run-budget timeout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.flows.test_simulate import (
    _make_flow,
    _mock_execute_pass,
)
from tests.flows.test_simulate_cocotb import (
    _cocotb_output,
    _fake_resolved,
    _make_cocotb_flow,
)

from booley.flows.base import SubprocessResult
from booley.flows.execution import ExecutionSelection
from booley.flows.simulate import SimulateFlow
from booley.mcp.base import EXIT_FAILURE, EXIT_SUCCESS
from booley.runtime.project_dir import reset_cache

_BUILTIN_SANDBOX = ExecutionSelection()

_PRE_RUN = ["make -C tests prep CASE=$BOOLEY_TEST_NAME"]


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["bash"], returncode=rc, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Env contract
# ---------------------------------------------------------------------------


class TestPreRunEnvContract:
    def test_single_test_run_env(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        flow._record_eda_tool("lite", "verilator")
        env = flow._pre_run_env(
            "lite",
            "smoke",
            {"lite": ["smoke", "stress"]},
            tmp_path / "build" / "lite",
        )
        assert env["BOOLEY_TARGET"] == "lite"
        # A single-test run: BOOLEY_TEST_NAME set, TEST_NAMES carries the run's
        # (one-element) list — not the Target's whole declared list.
        assert env["BOOLEY_TEST_NAME"] == "smoke"
        assert env["BOOLEY_TEST_NAMES"] == "smoke"
        assert env["BOOLEY_BUILD_ROOT"] == str(tmp_path / "build" / "lite")
        assert env["BOOLEY_PROJECT_ROOT"] == str(tmp_path)
        # run_cwd knob unset → the sandbox default is the project root.
        assert env["BOOLEY_RUN_CWD"] == str(tmp_path)
        assert env["BOOLEY_SIM_EDA_TOOL"] == "verilator"

    def test_batch_run_env_has_no_test_name(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        env = flow._pre_run_env(
            "lite",
            None,
            {"lite": ["smoke", "stress"]},
            None,
        )
        assert "BOOLEY_TEST_NAME" not in env
        assert env["BOOLEY_TEST_NAMES"] == "smoke stress"
        assert "BOOLEY_BUILD_ROOT" not in env

    @patch("booley.flows.simulate._resolve_run_cwd", return_value="util/sim")
    def test_run_cwd_knob_wins(self, _mock_cwd, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        env = flow._pre_run_env("lite", None, {}, None)
        assert env["BOOLEY_RUN_CWD"] == str(tmp_path / "util" / "sim")

    def test_default_run_dir_used_when_knob_unset(self, tmp_path: Path):
        # The boundary path passes the work root — where the host EDA tools run.
        flow = _make_flow(tmp_path, config="lite")
        work_root = tmp_path / "wr"
        env = flow._pre_run_env("lite", None, {}, None, default_run_dir=work_root)
        assert env["BOOLEY_RUN_CWD"] == str(work_root)

    def test_project_dir_exported_when_present(self, tmp_path: Path):
        import os

        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        flow = _make_flow(tmp_path, config="lite")
        reset_cache()
        try:
            with patch.dict(os.environ, {"BOOLEY_PROJECT_DIR": str(project_dir)}):
                env = flow._pre_run_env("lite", None, {}, None)
            assert env["BOOLEY_PROJECT_DIR"] == str(project_dir)
        finally:
            reset_cache()


# ---------------------------------------------------------------------------
# HDL loop: fires per test, under the run's env
# ---------------------------------------------------------------------------


class TestHdlLoopFiring:
    @patch("booley.flows.simulate._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_resolve_execution", return_value=_BUILTIN_SANDBOX)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    def test_fires_once_per_test(
        self, _mock_pre, _mock_prep, _mock_exec_sel, _mock_tests, tmp_path: Path
    ):
        run_mock = MagicMock(return_value=_completed())
        with patch("booley.flows.simulate.subprocess.run", run_mock):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert run_mock.call_count == 2  # one per test
        for call, test in zip(run_mock.call_args_list, ["smoke", "stress"], strict=True):
            argv = call.args[0]
            assert argv[1:] == ["-c", "set -e\n" + _PRE_RUN[0]]
            env = call.kwargs["env"]
            assert env["BOOLEY_TEST_NAME"] == test
            assert env["BOOLEY_TARGET"] == "lite"
            assert call.kwargs["cwd"] == flow.args.work_dir
            # The commands share the per-test run budget.
            assert call.kwargs["timeout"] == flow._get_timeout()

    @patch("booley.flows.simulate._get_test_names", return_value={"lite": ["smoke"]})
    @patch.object(SimulateFlow, "_resolve_execution", return_value=_BUILTIN_SANDBOX)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_unset_knob_spawns_nothing(
        self, _mock_prep, _mock_exec_sel, _mock_tests, tmp_path: Path
    ):
        run_mock = MagicMock(return_value=_completed())
        with patch("booley.flows.simulate.subprocess.run", run_mock):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Failure isolation: failed TestResult, loop continues, never exit 2
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    @patch("booley.flows.simulate._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_resolve_execution", return_value=_BUILTIN_SANDBOX)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    def test_failure_is_isolated_and_loop_continues(
        self, _mock_pre, _mock_prep, _mock_exec_sel, _mock_tests, tmp_path: Path
    ):
        # First test's pre-run fails (rc=2), second succeeds → the batch is a
        # graded FAIL (exit 1) with 1/2 passed, never a Flow crash (exit 2).
        run_mock = MagicMock(
            side_effect=[
                _completed(rc=2, stderr="fw build broke\nmissing objcopy"),
                _completed(),
            ]
        )
        with patch("booley.flows.simulate.subprocess.run", run_mock):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert run_mock.call_count == 2  # the loop went on to the second test
        assert "pre-run commands failed (rc=2)" in result.report_text
        assert "missing objcopy" in result.report_text
        assert "1/2 tests" in result.report_text

    @patch("booley.flows.simulate._get_test_names", return_value={"lite": ["smoke"]})
    @patch.object(SimulateFlow, "_resolve_execution", return_value=_BUILTIN_SANDBOX)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    def test_timeout_shares_run_budget(
        self, _mock_pre, _mock_prep, _mock_exec_sel, _mock_tests, tmp_path: Path
    ):
        run_mock = MagicMock(side_effect=subprocess.TimeoutExpired(cmd="bash", timeout=5))
        with patch("booley.flows.simulate.subprocess.run", run_mock):
            # --timeout 5000 ms → a 5 s budget shared with the pre-run step.
            flow = _make_flow(tmp_path, config="lite", extra_args=["--timeout", "5000"])
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert run_mock.call_args.kwargs["timeout"] == 5
        assert "pre-run commands failed (timeout after 5s)" in result.report_text


# ---------------------------------------------------------------------------
# Cocotb batch: fires once, before the batched build+run
# ---------------------------------------------------------------------------


class TestCocotbBatchFiring:
    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    def test_fires_once_per_batch(self, _mock_pre, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output([("test_reset", "pass", ""), ("test_count", "pass", "")])
        run_mock = MagicMock(return_value=_completed())
        with (
            patch(
                "booley.config.project_config.TEST_NAMES", {"ccfg": ["test_reset", "test_count"]}
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=_fake_resolved(tmp_path),
            ),
            patch.object(
                SimulateFlow,
                "_execute",
                lambda self, cmd: SubprocessResult(
                    returncode=0, stdout=out, stderr="", duration_s=1.0
                ),
            ),
            patch("booley.flows.simulate.subprocess.run", run_mock),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert run_mock.call_count == 1  # once per batch, not per test
        env = run_mock.call_args.kwargs["env"]
        assert "BOOLEY_TEST_NAME" not in env  # a 2-test batch is not a single-test run
        assert env["BOOLEY_TEST_NAMES"] == "test_reset test_count"

    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    def test_single_test_batch_sets_test_name(self, _mock_pre, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path, extra_args=["--test", "test_reset"])
        out = _cocotb_output([("test_reset", "pass", "")])
        run_mock = MagicMock(return_value=_completed())
        with (
            patch(
                "booley.config.project_config.TEST_NAMES", {"ccfg": ["test_reset", "test_count"]}
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=_fake_resolved(tmp_path),
            ),
            patch.object(
                SimulateFlow,
                "_execute",
                lambda self, cmd: SubprocessResult(
                    returncode=0, stdout=out, stderr="", duration_s=1.0
                ),
            ),
            patch("booley.flows.simulate.subprocess.run", run_mock),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        env = run_mock.call_args.kwargs["env"]
        assert env["BOOLEY_TEST_NAME"] == "test_reset"
        assert env["BOOLEY_TEST_NAMES"] == "test_reset"

    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    def test_batch_failure_short_circuits_before_build(self, _mock_pre, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        run_mock = MagicMock(return_value=_completed(rc=3, stderr="no cross gcc"))
        with (
            patch(
                "booley.config.project_config.TEST_NAMES", {"ccfg": ["test_reset", "test_count"]}
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=_fake_resolved(tmp_path),
            ),
            patch.object(
                SimulateFlow,
                "_execute",
                MagicMock(side_effect=AssertionError("build+run must not fire")),
            ),
            patch("booley.flows.simulate.subprocess.run", run_mock),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE  # graded failure, not exit 2
        assert "pre-run commands failed (rc=3)" in result.report_text
        assert "no cross gcc" in result.report_text


class TestMarkerAttribution:
    def test_interpret_attributes_prerun_failure(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        combined = (
            "module load ok\n"
            "fw build broke\n"
            "[BOOLEY_PRERUN_FAIL rc=3]\n"
            "make: *** [booley-sim] Error 1\n"
        )
        proc = SubprocessResult(returncode=2, stdout=combined, stderr="", duration_s=1.0)
        tr = flow._interpret_sim_result(combined, proc, "lite", "smoke")
        assert tr.passed is False
        assert tr.inconclusive is False
        assert tr.elab_failed is True  # short-circuit shape: the sim never ran
        assert "pre-run commands failed (rc=3)" in tr.error_tail


# ---------------------------------------------------------------------------
# Dry-run parity
# ---------------------------------------------------------------------------


class TestDryRunParity:
    @patch(
        "booley.flows.simulate._resolve_pre_run_commands",
        return_value=["bash tests/prep.sh $BOOLEY_TEST_NAME"],
    )
    def test_sandbox_preview_surfaces_commands_and_env(self, _mock_pre, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        cmd = flow._dry_run_command("lite", "smoke", {"lite": ["smoke"]})
        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        pre_cmd = "bash tests/prep.sh $BOOLEY_TEST_NAME"
        assert pre_cmd in script
        assert "export BOOLEY_TARGET=lite" in script
        assert "export BOOLEY_TEST_NAME=smoke" in script
        # Real position: after the fusesoc setup half, before the build.
        assert script.index("--setup") < script.index(pre_cmd)
        assert script.index(pre_cmd) < script.index("make -C")

    def test_sandbox_preview_clean_when_unset(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        script = flow._dry_run_command("lite", "smoke", {"lite": ["smoke"]})[2]
        assert "export BOOLEY_" not in script


class TestTargetSimEnvVisibleToPreRun:
    """F-5: the Target's tests.toml `env` is visible to pre-run commands too.

    On the boundary path those exports head the same makefile recipe, so a
    flavour-aware firmware build must not see a different world in-sandbox.
    """

    @patch("booley.flows.simulate._get_test_names", return_value={"lite": ["smoke"]})
    @patch.object(SimulateFlow, "_resolve_execution", return_value=_BUILTIN_SANDBOX)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    @patch(
        "booley.flows.simulate._get_test_envs",
        return_value={"lite": {"FLAVOR": "vanilla", "BOOLEY_TARGET": "hijack"}},
    )
    def test_env_reaches_pre_run_and_booley_vars_win(
        self, _mock_env, _mock_pre, _mock_prep, _mock_sel, _mock_tests, tmp_path: Path
    ):
        run_mock = MagicMock(return_value=_completed())
        with patch("booley.flows.simulate.subprocess.run", run_mock):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        env = run_mock.call_args_list[0].kwargs["env"]
        assert env["FLAVOR"] == "vanilla"
        # The BOOLEY_* contract is applied last and is not overridable.
        assert env["BOOLEY_TARGET"] == "lite"


# ---------------------------------------------------------------------------
# Observability: the hook's firing is recorded (fpu F-27)
# ---------------------------------------------------------------------------


class TestFiringIsVisible:
    """A hook quietly doing the wrong thing must be diagnosable.

    Before this, nothing in the console, run.log or artifacts said
    ``pre_run_commands`` had fired at all — the fpu port could only prove it by
    breaking one on purpose and reading the failure.
    """

    @patch("booley.flows.simulate._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_resolve_execution", return_value=_BUILTIN_SANDBOX)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    def test_passing_run_records_each_firing(
        self, _mock_pre, _mock_prep, _mock_exec_sel, _mock_tests, tmp_path: Path
    ):
        with patch("booley.flows.simulate.subprocess.run", MagicMock(return_value=_completed())):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        report = result.report_text
        assert report.count("pre_run_commands (1 line(s)) for smoke: rc=0") == 1
        assert report.count("pre_run_commands (1 line(s)) for stress: rc=0") == 1

    @patch("booley.flows.simulate._get_test_names", return_value={"lite": ["smoke"]})
    @patch.object(SimulateFlow, "_resolve_execution", return_value=_BUILTIN_SANDBOX)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    @patch("booley.flows.simulate._resolve_pre_run_commands", return_value=list(_PRE_RUN))
    def test_failing_hook_records_its_exit_status(
        self, _mock_pre, _mock_prep, _mock_exec_sel, _mock_tests, tmp_path: Path
    ):
        failed = _completed()
        failed.returncode = 3
        failed.stderr = "make: *** no rule to make target"
        with patch("booley.flows.simulate.subprocess.run", MagicMock(return_value=failed)):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert "pre_run_commands (1 line(s)) for smoke: rc=3" in result.report_text

    @patch("booley.flows.simulate._get_test_names", return_value={"lite": ["smoke"]})
    @patch.object(SimulateFlow, "_resolve_execution", return_value=_BUILTIN_SANDBOX)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_unset_knob_records_nothing(
        self, _mock_prep, _mock_exec_sel, _mock_tests, tmp_path: Path
    ):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert "pre_run_commands" not in result.report_text
