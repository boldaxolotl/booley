"""SimulateFlow Cocotb Target tests (ADR 0034 — G3, G4, B2, B3, B4, G6).

Host-runnable: `fusesoc run --setup` is stubbed with a fake ResolvedTarget
carrying ``cocotb_module`` and the batched execution is fed canned run-half
output ([COCOTB_RESULTS] + [SIM_SUMMARY] lines built with the real
formatters). The real icarus/verilator cocotb runs are the Sandbox e2e's job
(G8-G15).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from booley.flows.base import SubprocessResult
from booley.flows.sim.flow import SimulateFlow
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.sim import cocotb_results as cr
from booley.sim.sim_result import format_summary
from tests.flows.sim.test_flow import _make_flow

# A .core declaring a Cocotb Target (flow_options.cocotb_module — decision 2).
_COCOTB_CORE_TEXT = """\
CAPI=2:
name: ::ccfg:0
description: cocotb simulate fixture
filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
  tb:
    files:
      - tb/test_counter.py: {file_type: user, copyto: test_counter.py}
    tags: [tb]
targets:
  ccfg:
    filesets: [rtl, tb]
    toplevel: counter
    flow: sim
    flow_options:
      tool: icarus
      cocotb_module: test_counter
      iverilog_options: [-g2012]
"""


def _make_cocotb_flow(tmp_path: Path, extra_args: list[str] | None = None):
    (tmp_path / "ccfg.core").write_text(_COCOTB_CORE_TEXT, encoding="utf-8")
    return _make_flow(
        tmp_path,
        config="ccfg",
        extra_args=extra_args,
        seed_core=False,
    )


def _fake_resolved(tmp_path: Path, eda_tool: str = "icarus") -> ResolvedTarget:
    build_root = tmp_path / "build" / "ccfg"
    build_root.mkdir(parents=True, exist_ok=True)
    return ResolvedTarget(
        name="ccfg",
        vlnv="::ccfg:0",
        toplevel="counter",
        eda_tool=eda_tool,
        files=(),
        parameters={},
        build_root=build_root,
        edam_path=build_root / "ccfg.eda.yml",
        cocotb_module="test_counter",
    )


def _cocotb_output(
    entries: list[tuple[str, str, str]],
    *,
    state: str = cr.STATE_OK,
    detail: str = "",
    sva_lines: str = "",
    sva_errors: int = 0,
    passed: bool | None = None,
) -> str:
    """Canned run-half stdout: sim noise + [COCOTB_RESULTS] + [SIM_SUMMARY].

    *sva_errors* rides the [SIM_SUMMARY] line — the run-half computes it over
    the RUN output only, and simulate takes it from the summary (recounting
    over `combined` would miscount build warnings echoing $error source text).
    """
    results = cr.CocotbResults(
        state=state,
        detail=detail,
        tests=tuple(
            cr.CocotbTest(name=n, module="test_counter", status=s, failure_text=f)
            for n, s, f in entries
        ),
    )
    if passed is None:
        passed = bool(entries) and all(s == "pass" for _, s, _ in entries)
    return (
        f"sim noise\n{sva_lines}"
        f"{cr.format_results_line(results)}\n"
        f"{format_summary(passed, sva_errors)}\n"
    )


def _execute_returning(stdout: str, returncode: int = 0, timed_out: bool = False):
    def _exec(self, cmd):
        return SubprocessResult(
            returncode=returncode,
            stdout=stdout,
            stderr="",
            timed_out=timed_out,
            duration_s=2.0,
        )

    return _exec


def _run_cocotb(tmp_path, flow, stdout, returncode=0, timed_out=False, resolved_eda_tool="icarus"):
    token = "abc123"
    if "BOOLEY_BUILD_STAGE" not in stdout:
        build_failed = any(
            marker in stdout
            for marker in ("compilation failed", "elaboration failed", "not found")
        )
        if build_failed:
            stdout = f"{stdout}BOOLEY_BUILD_STAGE token={token} rc=1\n"
        else:
            stdout = f"BOOLEY_BUILD_STAGE token={token} rc=0\n{stdout}"
    with (
        patch(
            "booley.fusesoc.fusesoc_registry.resolve_target",
            return_value=_fake_resolved(tmp_path, resolved_eda_tool),
        ),
        patch.object(
            SimulateFlow,
            "_execute",
            _execute_returning(stdout, returncode, timed_out),
        ),
        patch("booley.flows.sim.flow.new_attempt_token", return_value=token),
    ):
        return flow._run()


_TESTS = {"ccfg": ["test_reset", "test_count", "test_fail_assert"]}


# ---------------------------------------------------------------------------
# G3 — verdict matrix (the two cocotb traps front and center)
# ---------------------------------------------------------------------------


class TestCocotbVerdictMatrix:
    def test_all_pass_rc0_passes_and_sets_criterion(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output(
            [
                ("test_reset", "pass", ""),
                ("test_count", "pass", ""),
                ("test_fail_assert", "pass", ""),
            ]
        )
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out)
        assert result.exit_code == EXIT_SUCCESS
        assert result.criterion_key == "sim_pass_ccfg"
        assert result.criterion_met is True

    def test_named_cycle_records_are_attributed_within_batch(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        out = (
            _cocotb_output(
                [
                    ("test_reset", "pass", ""),
                    ("test_count", "pass", ""),
                    ("test_fail_assert", "pass", ""),
                ]
            )
            + "CYCLES test_count 17\n"
            + "CYCLES test_reset 3\n"
        )
        with (
            patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)),
            patch(
                "booley.flows.sim.flow._resolve_cycle_sentinels",
                return_value=["CYCLES"],
            ),
        ):
            result = _run_cocotb(tmp_path, flow, out)

        assert "test_count           PASS         17 cycles" in result.report_text
        report = json.loads((tmp_path / "reports" / "sim_ccfg.json").read_text())
        by_name = {test["name"]: test for test in report["tests"]}
        assert by_name["test_reset"]["cycles"] == 3
        assert by_name["test_count"]["cycles"] == 17
        assert by_name["test_fail_assert"]["cycles"] is None

    def test_trap_rc0_with_failing_test_in_xml_is_fail(self, tmp_path: Path):
        """Regression (G3): exit code 0 with a failing test in XML is FAIL."""
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output(
            [
                ("test_reset", "pass", ""),
                ("test_count", "pass", ""),
                (
                    "test_fail_assert",
                    "fail",
                    "AssertionError: deliberate failure: count should be 0",
                ),
            ],
            passed=False,
        )
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out, returncode=0)
        assert result.exit_code == EXIT_FAILURE
        # The cocotb failure text is surfaced; siblings stay pass (G9 shape).
        assert "deliberate failure" in result.report_text
        report = json.loads(
            (tmp_path / "reports" / "sim_ccfg.json").read_text(),
        )
        by_name = {t["name"]: t for t in report["tests"]}
        assert by_name["test_fail_assert"]["verdict"] == "fail"
        assert by_name["test_reset"]["verdict"] == "pass"
        assert by_name["test_count"]["verdict"] == "pass"

    def test_trap_rc0_with_no_xml_is_inconclusive(self, tmp_path: Path):
        """Regression (G3): exit code 0 with no results.xml is INCONCLUSIVE."""
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output(
            [],
            state=cr.STATE_MISSING,
            detail="results.xml not found at /b/results.xml",
            passed=False,
        )
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out, returncode=0)
        assert result.exit_code == EXIT_FAILURE
        assert "INCONCLUSIVE" in result.report_text
        # Inconclusive skips the criterion write entirely.
        assert not flow.state.has_criterion("sim_pass_ccfg")

    def test_absent_expected_test_is_inconclusive_with_actionable_message(
        self,
        tmp_path: Path,
    ):
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output(
            [
                ("test_reset", "pass", ""),
                ("test_count", "pass", ""),
            ],
            passed=False,
        )  # test_fail_assert absent from the XML
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out)
        assert result.exit_code == EXIT_FAILURE
        assert "no matching @cocotb.test" in result.report_text
        report = json.loads(
            (tmp_path / "reports" / "sim_ccfg.json").read_text(),
        )
        by_name = {t["name"]: t for t in report["tests"]}
        assert by_name["test_fail_assert"]["verdict"] == "inconclusive"

    def test_extra_unexpected_xml_test_is_logged_not_verdict_bearing(
        self,
        tmp_path: Path,
    ):
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output(
            [
                ("test_reset", "pass", ""),
                ("test_count", "pass", ""),
                ("test_fail_assert", "pass", ""),
                ("test_surprise", "pass", ""),
            ]
        )
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out)
        assert result.exit_code == EXIT_SUCCESS  # extras don't flip verdicts
        assert "test_surprise" in result.report_text
        report = json.loads(
            (tmp_path / "reports" / "sim_ccfg.json").read_text(),
        )
        assert len(report["tests"]) == 3  # selected set only

    def test_xml_truncated_is_inconclusive(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output(
            [],
            state=cr.STATE_UNPARSEABLE,
            detail="results.xml is truncated or malformed: no element found",
            passed=False,
        )
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out)
        assert result.exit_code == EXIT_FAILURE
        assert "INCONCLUSIVE" in result.report_text

    def test_sva_errors_fail_an_all_pass_batch(self, tmp_path: Path):
        """C3: output scanning retained — RTL $fatal under a green XML fails.

        The count arrives via the run-half's [SIM_SUMMARY] (run output only);
        simulate never recounts over `combined`, whose build half echoes the
        DUT's $error/$fatal source text in iverilog warnings (e2e regression).
        """
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output(
            [
                ("test_reset", "pass", ""),
                ("test_count", "pass", ""),
                ("test_fail_assert", "pass", ""),
            ],
            sva_lines="ERROR: rtl/counter.sv:18: counter reached error trap\n",
            sva_errors=1,
            passed=False,
        )
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out)
        assert result.exit_code == EXIT_FAILURE
        assert "sva_errors=1" in result.report_text

    def test_build_warnings_echoing_error_text_do_not_fail_the_batch(
        self,
        tmp_path: Path,
    ):
        """e2e G8 regression: iverilog's 'System task ($error) cannot be
        synthesized' build warnings must not count as DUT assertions."""
        flow = _make_cocotb_flow(tmp_path)
        out = (
            "rtl/counter.sv:18: warning: System task ($error) cannot be "
            "synthesized in an always_ff process.\n"
            "rtl/counter.sv:19: warning: System task ($fatal) cannot be "
            "synthesized in an always_ff process.\n"
            + _cocotb_output(
                [
                    ("test_reset", "pass", ""),
                    ("test_count", "pass", ""),
                    ("test_fail_assert", "pass", ""),
                ]
            )
        )
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out)
        assert result.exit_code == EXIT_SUCCESS
        assert result.criterion_met is True

    def test_nonzero_rc_with_all_pass_xml_is_not_pass(self, tmp_path: Path):
        """G3 matrix: an all-pass XML after a failed-verdict run stays FAIL.

        The run-half folds the sim exit code into its [SIM_SUMMARY] batch
        verdict (rc!=0 → passed=false even when every XML entry passed —
        e.g. a $fatal after the last test completed); simulate honors that
        batch authority while per-test verdicts stay reconciliation-driven.
        """
        flow = _make_cocotb_flow(tmp_path)
        out = _cocotb_output(
            [
                ("test_reset", "pass", ""),
                ("test_count", "pass", ""),
                ("test_fail_assert", "pass", ""),
            ],
            passed=False,
        )  # the run-half saw rc!=0 and said FAIL
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out, returncode=1)
        assert result.exit_code == EXIT_FAILURE
        # Per-test verdicts still come from the reconciled XML.
        report = json.loads(
            (tmp_path / "reports" / "sim_ccfg.json").read_text(),
        )
        assert all(t["verdict"] == "pass" for t in report["tests"])

    def test_timeout_marks_unfinished_tests_timed_out(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        out = "sim noise\nERROR: cocotb simulation timed out (5s)\n" + _cocotb_output(
            [
                ("test_reset", "pass", ""),
                ("test_count", "fail", "SimFailure: Simulator shut down prematurely"),
            ],
            passed=False,
        )
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, out, returncode=1)
        assert result.exit_code == EXIT_FAILURE
        report = json.loads(
            (tmp_path / "reports" / "sim_ccfg.json").read_text(),
        )
        by_name = {t["name"]: t for t in report["tests"]}
        # Finished-and-passed stays pass, the active test reads timeout, and a
        # later selected test is explicitly distinguished as never run.
        assert by_name["test_reset"]["verdict"] == "pass"
        assert by_name["test_count"]["verdict"] == "timeout"
        assert by_name["test_fail_assert"]["verdict"] == "inconclusive"
        assert cr.TIMEOUT_NOT_RUN_DETAIL in by_name["test_fail_assert"]["error_tail"]

    def test_missing_results_line_is_inconclusive_never_pass(self, tmp_path: Path):
        # The run died before the run-half's post-processing (e.g. an outer
        # kill): no [COCOTB_RESULTS], no [SIM_SUMMARY] → inconclusive.
        flow = _make_cocotb_flow(tmp_path)
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(tmp_path, flow, "bare sim noise, rc 0\n")
        assert result.exit_code == EXIT_FAILURE
        assert "INCONCLUSIVE" in result.report_text


# ---------------------------------------------------------------------------
# B2 — batching: one prepared command, --test set filtering
# ---------------------------------------------------------------------------


class TestCocotbBatching:
    def test_one_execute_for_the_whole_set(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        calls: list[list[str]] = []

        def _capture(self, cmd):
            calls.append(cmd)
            return SubprocessResult(
                returncode=0,
                stdout=_cocotb_output(
                    [
                        ("test_reset", "pass", ""),
                        ("test_count", "pass", ""),
                        ("test_fail_assert", "pass", ""),
                    ]
                ),
                stderr="",
                duration_s=1.0,
            )

        with (
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=_fake_resolved(tmp_path),
            ),
            patch.object(SimulateFlow, "_execute", _capture),
            patch(
                "booley.config.project_config.TEST_NAMES",
                dict(_TESTS),
            ),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert len(calls) == 1  # one build + one sim process for 3 tests
        script = calls[0][-1]
        assert "booley.sim.cocotb_run" in script
        assert script.count("--test=") == 3
        assert "--cocotb-module test_counter" in script
        assert "--result-verbosity compact" in script

    def test_full_result_verbosity_reaches_the_run_half(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path, extra_args=["--result-verbosity", "full"])
        cmd = flow._cocotb_run_cmd("build/ccfg", "icarus", "test_counter", ["test_reset"])
        index = cmd.index("--result-verbosity")
        assert cmd[index + 1] == "full"

    def test_trace_scope_reaches_the_run_half(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path, extra_args=["--trace"])
        cmd = flow._cocotb_run_cmd(
            "build/ccfg",
            "icarus",
            "test_counter",
            ["test_reset"],
            trace_scope="counter",
        )
        index = cmd.index("--expected-trace-scope")
        assert cmd[index + 1] == "counter"

    def test_substr_filter_prunes_the_selected_set(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path, extra_args=["--test", "count"])
        calls: list[list[str]] = []

        def _capture(self, cmd):
            calls.append(cmd)
            return SubprocessResult(
                returncode=0,
                stdout=_cocotb_output([("test_count", "pass", "")]),
                stderr="",
                duration_s=1.0,
            )

        with (
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=_fake_resolved(tmp_path),
            ),
            patch.object(SimulateFlow, "_execute", _capture),
            patch(
                "booley.config.project_config.TEST_NAMES",
                dict(_TESTS),
            ),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        script = calls[0][-1]
        assert "--test=test_count" in script
        assert "test_reset" not in script

    def test_skip_prunes_but_never_empties(self, tmp_path: Path):
        """G4: `skip` works unchanged on a Cocotb Target."""
        flow = _make_cocotb_flow(tmp_path, extra_args=["--skip", "test_fail_assert"])
        calls: list[list[str]] = []

        def _capture(self, cmd):
            calls.append(cmd)
            return SubprocessResult(
                returncode=0,
                stdout=_cocotb_output(
                    [
                        ("test_reset", "pass", ""),
                        ("test_count", "pass", ""),
                    ]
                ),
                stderr="",
                duration_s=1.0,
            )

        with (
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=_fake_resolved(tmp_path),
            ),
            patch.object(SimulateFlow, "_execute", _capture),
            patch(
                "booley.config.project_config.TEST_NAMES",
                dict(_TESTS),
            ),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        script = calls[0][-1]
        assert "test_fail_assert" not in script
        assert "--test=test_reset" in script


# ---------------------------------------------------------------------------
# G4 — tests.toml validation: `select` is a setup-time error on cocotb
# ---------------------------------------------------------------------------


class TestCocotbSelectRejection:
    def test_select_template_on_cocotb_target_is_setup_error(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        with (
            patch(
                "booley.config.project_config.TEST_NAMES",
                dict(_TESTS),
            ),
            patch(
                "booley.config.project_config.TEST_SELECT",
                {"ccfg": "+test_id={index}"},
            ),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert "COCOTB_TEST_FILTER" in result.report_text
        assert "remove the `select` key" in result.report_text

    def test_select_on_sv_target_still_fine(self, tmp_path: Path):
        # The rejection keys off cocotb-ness, not the mere presence of select.
        flow = _make_flow(tmp_path, config="lite")
        with (
            patch(
                "booley.config.project_config.TEST_NAMES",
                {"lite": ["t0"]},
            ),
            patch(
                "booley.config.project_config.TEST_SELECT",
                {"lite": "+test_id={index}"},
            ),
            patch.object(
                SimulateFlow,
                "_prepare_sim_command",
                return_value=["sh", "-c", ":"],
            ),
            patch.object(
                SimulateFlow,
                "_execute",
                _execute_returning(
                    '[SIM_RESULT] PASSED\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n',
                ),
            ),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Public simulator eligibility
# ---------------------------------------------------------------------------


class TestCocotbSimulatorEligibility:
    def test_commercial_resolved_eda_tool_fails_fast(self, tmp_path: Path):
        # A Cocotb Target resolving to xcelium/vcs raises in prepare — the
        # message names the v1 boundary, never a silent non-cocotb run.
        flow = _make_cocotb_flow(tmp_path)
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(
                tmp_path,
                flow,
                "unused",
                resolved_eda_tool="xcelium",
            )
        assert result.exit_code == EXIT_FAILURE
        assert "select a Verilator or Icarus Target" in result.report_text


# ---------------------------------------------------------------------------
# B4 — dry-run parity
# ---------------------------------------------------------------------------


class TestCocotbDryRun:
    def test_dry_run_emits_one_batched_command(self, tmp_path: Path, capsys):
        flow = _make_cocotb_flow(tmp_path, extra_args=["--dry-run"])
        with (
            patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)),
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                side_effect=AssertionError("dry-run must not resolve"),
            ),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        commands = result.detail["commands"]
        assert len(commands) == 1  # batched: one command for three tests
        script = commands[0][-1]
        assert "fusesoc" in script and "--setup" in script
        assert "booley.sim.cocotb_run" in script
        assert script.count("--test=") == 3
        assert "--eda-tool icarus" in script
        assert "--cocotb-module test_counter" in script

    def test_dry_run_sv_target_unchanged(self, tmp_path: Path):
        """G6 guard: a non-cocotb Target's dry-run command carries no cocotb."""
        flow = _make_flow(tmp_path, config="lite", extra_args=["--dry-run"])
        with patch("booley.config.project_config.TEST_NAMES", {}):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        script = result.detail["commands"][0][-1]
        assert "cocotb" not in script
        assert "booley.sim.verilator_run" in script


class TestCocotbBuildFailureShape:
    """B4: when the BUILD half breaks, no simulator ever starts and not one of
    the selected tests runs. Fanning the compile error out into N per-test
    "FAIL 0.0s" rows invented verdicts for tests that never executed and buried
    the compile error in the first row's tail (taxi: an icarus Target whose SV
    interface ports don't parse in iverilog gave 14 phantom FAILs)."""

    # An iverilog compile error, exactly as the run-half echoes it — matched by
    # simulate's _ELAB_FAIL_RE. No [COCOTB_RESULTS] line: the sim never ran.
    _BUILD_FAIL_OUTPUT = (
        "[cocotb simulation: test_counter on icarus]\n"
        "rtl/counter.sv:12: syntax error\n"
        "rtl/counter.sv:12: error: Invalid module instantiation\n"
        "ERROR: iverilog compilation failed\n"
    )

    def _report(self, tmp_path: Path) -> dict:
        return json.loads(
            (tmp_path / "reports" / "sim_ccfg.json").read_text(),
        )

    def test_build_failure_yields_one_build_entry_not_a_verdict_per_test(
        self,
        tmp_path: Path,
    ):
        flow = _make_cocotb_flow(tmp_path)
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(
                tmp_path,
                flow,
                self._BUILD_FAIL_OUTPUT,
                returncode=1,
            )
        assert result.exit_code == EXIT_FAILURE
        tests = self._report(tmp_path)["tests"]
        # One entry for the build — not three phantom per-test FAILs.
        assert len(tests) == 1
        assert tests[0]["name"] == "ccfg"
        assert tests[0]["verdict"] == "elab_error"
        assert tests[0]["passed"] is False

    def test_the_compile_error_rides_the_build_entry(self, tmp_path: Path):
        flow = _make_cocotb_flow(tmp_path)
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(
                tmp_path,
                flow,
                self._BUILD_FAIL_OUTPUT,
                returncode=1,
            )
        tail = self._report(tmp_path)["tests"][0]["error_tail"]
        assert "iverilog compilation failed" in tail
        assert "syntax error" in tail
        assert "did not compile" in tail
        # The summary names the tests that never ran, instead of grading them.
        assert "never ran" in result.report_text

    def test_a_build_failure_never_reads_as_a_pass(self, tmp_path: Path):
        # ADR 0034 dec 6 holds regardless of the reshaping: no results.xml, no
        # pass — even if the build-failed process somehow exits 0.
        flow = _make_cocotb_flow(tmp_path)
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            result = _run_cocotb(
                tmp_path,
                flow,
                self._BUILD_FAIL_OUTPUT,
                returncode=0,
            )
        assert result.exit_code == EXIT_FAILURE
        assert result.criterion_met is False

    def test_a_run_death_without_a_build_error_still_fans_out(self, tmp_path: Path):
        # The collapse is gated on an actual build failure. A run that dies
        # AFTER the simulator started (no compile error in the output) keeps the
        # per-test inconclusive shape — those tests really were dispatched.
        flow = _make_cocotb_flow(tmp_path)
        with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
            _run_cocotb(tmp_path, flow, "bare sim noise, rc 0\n")
        tests = self._report(tmp_path)["tests"]
        assert len(tests) == len(_TESTS["ccfg"])
        assert all(t["verdict"] == "inconclusive" for t in tests)


# ---------------------------------------------------------------------------
# ravenoc F-25 / F-32 — cocotb batch: watchdog wiring + missing-binary grading
# ---------------------------------------------------------------------------


def test_run_cmd_forwards_the_sim_time_grace(tmp_path: Path):
    """F-25: the frozen-clock watchdog knob crosses into the sandbox."""
    flow = _make_cocotb_flow(tmp_path)
    with patch("booley.flows.sim.flow._resolve_sim_time_grace_s", return_value=42.0):
        cmd = flow._cocotb_run_cmd("build/ccfg", "icarus", "test_counter", ["a"])
    assert "--sim-time-grace" in cmd
    assert cmd[cmd.index("--sim-time-grace") + 1] == "42.0"


def test_missing_verilator_on_a_cocotb_target_is_exit_2(tmp_path: Path):
    """F-32: the gauntlet's `run_test_001 FAIL 0.0s` was a missing binary."""
    flow = _make_cocotb_flow(tmp_path)
    stdout = "/bin/sh: 1: verilator: not found\nERROR: Verilator elaboration failed (rc=2)\n"
    with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
        result = _run_cocotb(tmp_path, flow, stdout, returncode=2)
    assert result.exit_code == EXIT_ERROR
    assert result.detail["eda_tool_error"] == "missing_executable"
    assert result.detail["missing_executable"] == "verilator"
    # None of the three selected tests may appear as a graded row.
    assert "test_reset" not in result.report_text


def test_missing_fusesoc_on_a_cocotb_target_is_exit_2(tmp_path: Path):
    flow = _make_cocotb_flow(tmp_path)
    boom = RuntimeError("could not invoke fusesoc (fusesoc): No such file or directory")
    with (
        patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)),
        patch.object(SimulateFlow, "_prepare_cocotb_sim_command", side_effect=boom),
    ):
        result = flow._run()
    assert result.exit_code == EXIT_ERROR
    assert result.detail["missing_executable"] == "fusesoc"


def test_real_cocotb_build_failure_still_grades_as_exit_1(tmp_path: Path):
    """A compiler that ran and rejected the design keeps its design verdict."""
    flow = _make_cocotb_flow(tmp_path)
    stdout = "tb.sv:12: syntax error\nERROR: iverilog compilation failed (rc=1)\n"
    with patch("booley.config.project_config.TEST_NAMES", dict(_TESTS)):
        result = _run_cocotb(tmp_path, flow, stdout, returncode=1)
    assert result.exit_code == EXIT_FAILURE
