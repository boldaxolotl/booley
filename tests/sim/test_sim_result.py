"""Unit tests for sim_result.py — shared simulation output parsing."""

import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import booley.sim.sim_result as sr

# ---------------------------------------------------------------------------
# parse_sim_verdict
# ---------------------------------------------------------------------------


class TestParseSimVerdict:
    def test_sentinel_passed(self):
        assert sr.parse_sim_verdict("[SIM_RESULT] PASSED") is True

    def test_sentinel_failed(self):
        # Smoke-test: just ensure no exception; real assertion is on next line
        sr.parse_sim_verdict("[SIM_RESULT] FAILED")
        assert sr.parse_sim_verdict("[SIM_RESULT] FAILED") is False

    def test_failed_wins_over_passed(self):
        assert sr.parse_sim_verdict("[SIM_RESULT] FAILED\n[SIM_RESULT] PASSED") is False

    def test_fatal(self):
        # Fatal: no longer detected by parse_sim_verdict (handled by count_sva_errors)
        assert sr.parse_sim_verdict("Fatal: some error") is None

    def test_legacy_passed(self):
        # Legacy sentinels removed — returns None (indeterminate)
        assert sr.parse_sim_verdict("Success! PASSED Success!") is None

    def test_legacy_failed(self):
        assert sr.parse_sim_verdict("Error! FAILED Error!") is None

    def test_bare_failed_with_tests(self):
        assert sr.parse_sim_verdict("FAILED 2/5 tests") is None

    def test_clean_output(self):
        assert sr.parse_sim_verdict("simulation completed successfully") is None

    def test_empty(self):
        assert sr.parse_sim_verdict("") is None

    def test_passed_wins_over_legacy_failed_text(self):
        """[SIM_RESULT] PASSED takes priority over bare FAILED text."""
        assert sr.parse_sim_verdict("[SIM_RESULT] PASSED\nFAILED 1/3 tests") is True

    def test_custom_pass_sentinel(self):
        """A project's own PASS wording is honored when configured."""
        out = "running...\nALL TESTS PASSED.\n"
        # Default markers absent -> None; configured sentinel -> True.
        assert sr.parse_sim_verdict(out) is None
        assert sr.parse_sim_verdict(out, pass_sentinels=["ALL TESTS PASSED."]) is True

    def test_custom_fail_sentinel_wins(self):
        """Fail sentinel beats pass sentinel on the same output (fail-safe)."""
        out = "ALL TESTS PASSED.\nERROR!\n"
        assert (
            sr.parse_sim_verdict(
                out,
                pass_sentinels=["ALL TESTS PASSED."],
                fail_sentinels=["ERROR!"],
            )
            is False
        )

    def test_sentinel_survives_long_trailing_noise(self):
        # Regression (22b4dcc, 2fe716c): the verdict must not be lost when the
        # sim prints far more than the old 200-line tail cap after the sentinel
        # (e.g. a Verilator rebuild dumping lint warnings onto stderr).
        out = "[SIM_RESULT] PASSED\n" + "\n".join(f"warning {i}" for i in range(5000))
        assert sr.parse_sim_verdict(out) is True

    def test_fail_survives_long_trailing_noise(self):
        out = "[SIM_RESULT] FAILED\n" + "\n".join(f"info {i}" for i in range(5000))
        assert sr.parse_sim_verdict(out) is False

    def test_explicit_tail_still_caps_scan(self):
        # Opt-in tail cap is preserved for callers that genuinely want it.
        out = "[SIM_RESULT] PASSED\n" + "\n".join(f"line {i}" for i in range(300))
        assert sr.parse_sim_verdict(out, tail_lines=200) is None
        assert sr.parse_sim_verdict(out) is True

    def test_custom_sentinels_disable_builtin_markers(self):
        """When custom PASS sentinels are set, the built-in marker no longer counts."""
        assert (
            sr.parse_sim_verdict("[SIM_RESULT] PASSED", pass_sentinels=["ALL TESTS PASSED."])
            is None
        )


# ---------------------------------------------------------------------------
# count_sva_errors
# ---------------------------------------------------------------------------


class TestCountSvaErrors:
    def test_no_errors(self):
        assert sr.count_sva_errors("All tests passed") == 0

    def test_dollar_error(self):
        assert sr.count_sva_errors("$error at time 100") == 1

    def test_bracket_error(self):
        assert sr.count_sva_errors("] Error: mismatch") == 1

    def test_fatal(self):
        assert sr.count_sva_errors("$fatal triggered") == 1

    def test_fatal_colon(self):
        assert sr.count_sva_errors("Fatal: simulation aborted") == 1

    def test_multiple_mixed(self):
        output = "$error at 100\n] Error: x\nFatal: abort\n$fatal end"
        assert sr.count_sva_errors(output) == 4

    def test_icarus_error_line_start(self):
        assert sr.count_sva_errors("ERROR: assertion failed at t=100") == 1

    def test_icarus_error_not_at_start(self):
        # "ERROR:" not at line start should not be counted by the Icarus pattern
        assert sr.count_sva_errors("some ERROR: not at start") == 0


# ---------------------------------------------------------------------------
# extract_vrfc_warnings
# ---------------------------------------------------------------------------


class TestExtractVrfcWarnings:
    def test_no_warnings(self):
        assert sr.extract_vrfc_warnings("clean output\nno issues") == []

    def test_single_warning(self):
        line = "WARNING: [VRFC 10-3380] some forward ref issue"
        result = sr.extract_vrfc_warnings(f"before\n{line}\nafter")
        assert result == [line]

    def test_multiple_warnings(self):
        lines = [
            "WARNING: [VRFC 10-3380] issue A",
            "WARNING: [VRFC 10-3380] issue B",
        ]
        output = "header\n" + "\n".join(lines) + "\nfooter"
        assert sr.extract_vrfc_warnings(output) == lines


# ---------------------------------------------------------------------------
# format_summary / parse_summary_line
# ---------------------------------------------------------------------------


class TestSummaryRoundTrip:
    def test_passed_basic(self):
        line = sr.format_summary(True)
        parsed = sr.parse_summary_line(line)
        assert parsed is not None
        assert parsed["passed"] is True
        assert parsed["sva_errors"] == 0

    def test_failed_with_sva(self):
        line = sr.format_summary(False, sva_errors=3)
        parsed = sr.parse_summary_line(line)
        assert parsed["passed"] is False
        assert parsed["sva_errors"] == 3

    def test_with_vrfc_warnings(self):
        warnings = ["WARNING: [VRFC 10-3380] issue A"]
        line = sr.format_summary(True, vrfc_warnings=warnings)
        parsed = sr.parse_summary_line(line)
        assert parsed["vrfc_warnings"] == warnings

    def test_no_vrfc_key_when_empty(self):
        line = sr.format_summary(True)
        parsed = sr.parse_summary_line(line)
        assert "vrfc_warnings" not in parsed

    def test_embedded_in_output(self):
        output = (
            "lots of sim output\nmore output\n"
            + sr.format_summary(True)
            + "\n>>> Simulation PASSED"
        )
        parsed = sr.parse_summary_line(output)
        assert parsed is not None
        assert parsed["passed"] is True

    def test_missing_returns_none(self):
        assert sr.parse_summary_line("no summary here") is None

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError, match="Malformed SIM_SUMMARY"):
            sr.parse_summary_line("[SIM_SUMMARY] {bad json}")

    def test_inconclusive_roundtrip(self):
        line = sr.format_summary(False, inconclusive=True)
        parsed = sr.parse_summary_line(line)
        assert parsed is not None
        assert parsed["passed"] is False
        assert parsed["inconclusive"] is True

    def test_no_inconclusive_key_when_false(self):
        line = sr.format_summary(True)
        parsed = sr.parse_summary_line(line)
        assert "inconclusive" not in parsed

    def test_non_object_json_raises(self):
        # Valid JSON but a list/scalar, not the expected object shape.
        for bad in ("[1, 2, 3]", "42", '"passed"', "null"):
            with pytest.raises(ValueError, match="Malformed SIM_SUMMARY shape"):
                sr.parse_summary_line("[SIM_SUMMARY] " + bad)

    def test_object_missing_passed_key_raises(self):
        # Well-formed object but missing the mandatory `passed` key the
        # caller indexes directly.
        with pytest.raises(ValueError, match="Malformed SIM_SUMMARY shape"):
            sr.parse_summary_line('[SIM_SUMMARY] {"sva_errors": 0}')

    @pytest.mark.parametrize(
        "payload",
        [
            '{"passed": "yes"}',
            '{"passed": true, "sva_errors": false}',
            '{"passed": true, "sva_errors": "3"}',
            '{"passed": true, "sva_errors": 3.0}',
            '{"passed": true, "vrfc_warnings": ["ok", 7]}',
            '{"passed": true, "inconclusive": 1}',
        ],
    )
    def test_invalid_field_types_raise(self, payload):
        with pytest.raises(ValueError, match="Malformed SIM_SUMMARY shape"):
            sr.parse_summary_line(sr.SIM_SUMMARY_PREFIX + payload)


# ---------------------------------------------------------------------------
# write_run_log
# ---------------------------------------------------------------------------


class TestWriteRunLog:
    """run.log — the durable raw-output copy that survives stdout truncation."""

    def test_filename_is_exactly_run_log(self, tmp_path):
        # CONTRACT: the simulate summary prints <work_dir>/run.log — the
        # filename must not drift.
        path = sr.write_run_log(tmp_path, "hello\nworld\n")
        assert path == tmp_path / "run.log"
        assert sr.RUN_LOG_NAME == "run.log"
        assert path.read_text(encoding="utf-8") == "hello\nworld\n"

    def test_under_cap_written_verbatim(self, tmp_path):
        text = "line\n" * 100
        sr.write_run_log(tmp_path, text, max_bytes=10_000)
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert content == text
        assert "TRUNCATED" not in content

    def test_over_cap_keeps_tail_with_marker(self, tmp_path):
        lines = [f"line {i:06d}" for i in range(500)]
        text = "\n".join(lines) + "\n"
        cap = 2_000
        sr.write_run_log(tmp_path, text, max_bytes=cap)
        raw = (tmp_path / "run.log").read_bytes()
        assert len(raw) <= cap  # the written file never exceeds the cap
        content = raw.decode("utf-8")
        marker, body = content.split("\n", 1)
        # One-line marker records the original (pre-cap) size.
        assert marker.startswith("[RUN_LOG TRUNCATED]")
        assert str(len(text.encode("utf-8"))) in marker
        # The TAIL survives (verdict/sentinel wording lives there) ...
        assert body.endswith("line 000499\n")
        # ... and starts on a clean line boundary, not mid-line.
        assert body.splitlines()[0].startswith("line ")

    def test_none_cap_preserves_unabridged_output(self, tmp_path):
        text = "complete simulator evidence\n"
        sr.write_run_log(tmp_path, text, max_bytes=None)
        assert (tmp_path / "run.log").read_text(encoding="utf-8") == text

    def test_overwrites_previous_log(self, tmp_path):
        sr.write_run_log(tmp_path, "first run")
        sr.write_run_log(tmp_path, "second run")
        assert (tmp_path / "run.log").read_text(encoding="utf-8") == "second run"

    def test_atomic_leaves_no_tmp_file(self, tmp_path):
        # Atomic tmp+rename: the PID-suffixed staging file must be gone.
        sr.write_run_log(tmp_path, "output")
        assert (tmp_path / "run.log").exists()
        assert list(tmp_path.glob("run.log.*.tmp")) == []

    def test_undecodable_text_never_raises(self, tmp_path):
        # A lone surrogate (undecoded byte smuggled through errors="replace"
        # upstream) must not lose the whole log to an encode error.
        sr.write_run_log(tmp_path, "before \udce9 after\n")
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert "before" in content and "after" in content


# ---------------------------------------------------------------------------
# begin_run_log / run_log_is_current — the F-26 staleness guard
# ---------------------------------------------------------------------------


class TestRunLogHeader:
    """A run.log must never read as live progress while holding old bytes."""

    def test_begin_erases_the_previous_runs_output(self, tmp_path):
        sr.write_run_log(tmp_path, "TEST PASSED\nold verdict\n")
        sr.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert "TEST PASSED" not in content
        assert content.startswith(f"{sr.RUN_LOG_HEADER_PREFIX} ")
        assert "run=run-2 flow=sim target=sim_fifo started=" in content
        assert sr.RUN_LOG_PENDING in content

    def test_header_fields_parse_back(self, tmp_path):
        sr.begin_run_log(tmp_path, flow="lint", target="lint_top", run="lint-7")
        header = sr.read_run_log_header(tmp_path)
        assert header == {
            "run": "lint-7",
            "flow": "lint",
            "target": "lint_top",
            "started": header["started"],
        }
        assert header["started"].endswith("Z")

    def test_write_preserves_the_header(self, tmp_path):
        sr.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        sr.write_run_log(tmp_path, "[SIM_RESULT] PASSED\n")
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert content.splitlines()[0].startswith(sr.RUN_LOG_HEADER_PREFIX)
        assert sr.RUN_LOG_PENDING not in content
        assert content.endswith("[SIM_RESULT] PASSED\n")
        assert sr.read_run_log_header(tmp_path)["run"] == "run-2"

    def test_cap_accounts_for_the_preserved_header(self, tmp_path):
        sr.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        cap = 500
        sr.write_run_log(tmp_path, "line\n" * 500, max_bytes=cap)
        raw = (tmp_path / "run.log").read_bytes()
        assert len(raw) <= cap
        assert raw.startswith(sr.RUN_LOG_HEADER_PREFIX.encode())

    def test_headerless_log_is_written_verbatim(self, tmp_path):
        # Paths that never call begin_run_log keep the historical shape.
        sr.write_run_log(tmp_path, "raw output\n")
        assert (tmp_path / "run.log").read_text(encoding="utf-8") == "raw output\n"
        assert sr.read_run_log_header(tmp_path) is None

    def test_is_current_only_after_the_output_lands(self, tmp_path):
        assert sr.run_log_is_current(tmp_path, "run-2") is False  # no log at all
        sr.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-2")
        assert sr.run_log_is_current(tmp_path, "run-2") is False  # still pending
        sr.write_run_log(tmp_path, "[SIM_RESULT] FAILED\n")
        assert sr.run_log_is_current(tmp_path, "run-2") is True

    def test_is_current_rejects_another_run(self, tmp_path):
        sr.begin_run_log(tmp_path, flow="sim", target="sim_fifo", run="run-1")
        sr.write_run_log(tmp_path, "[SIM_RESULT] PASSED\n")
        assert sr.run_log_is_current(tmp_path, "run-2") is False

    def test_is_current_rejects_a_headerless_log(self, tmp_path):
        sr.write_run_log(tmp_path, "TEST PASSED\n")
        assert sr.run_log_is_current(tmp_path, "run-2") is False

    def test_run_token_prefers_the_job_run_id(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_RUN_ID", "simulate-20260726T120000-1")
        assert sr.current_run_token() == "simulate-20260726T120000-1"
        monkeypatch.delenv("BOOLEY_RUN_ID")
        assert sr.current_run_token().startswith("pid")


class TestRunLogProgress:
    """F-18: an in-flight run must be readable, without ever looking finished."""

    def test_progress_shows_a_live_tail_and_keeps_the_header(self, tmp_path):
        sr.begin_run_log(tmp_path, flow="sim", target="sim_float", run="run-9")
        sr.write_run_log_progress(
            tmp_path,
            "cycle 100\ncycle 200\n",
            elapsed_s=42.0,
            line_count=2,
            idle_s=3.0,
        )
        content = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert content.splitlines()[0].startswith(sr.RUN_LOG_HEADER_PREFIX)
        assert "42s elapsed, 2 output line(s), last output 3s ago" in content
        assert "cycle 200" in content

    def test_progress_never_reads_as_a_finished_run(self, tmp_path):
        # The whole point of the marker prefix: a live tail is not a verdict.
        sr.begin_run_log(tmp_path, flow="sim", target="sim_float", run="run-9")
        sr.write_run_log_progress(tmp_path, "TEST SUCCEEDED\n", elapsed_s=5.0, line_count=1)
        assert sr.run_log_is_current(tmp_path, "run-9") is False
        sr.write_run_log(tmp_path, "TEST SUCCEEDED\n[SIM_RESULT] PASSED\n")
        assert sr.run_log_is_current(tmp_path, "run-9") is True

    def test_progress_keeps_the_tail_when_capped(self, tmp_path):
        sr.begin_run_log(tmp_path, flow="sim", target="sim_float", run="run-9")
        sr.write_run_log_progress(
            tmp_path,
            "".join(f"line {i}\n" for i in range(2000)),
            elapsed_s=1.0,
            line_count=2000,
            max_bytes=400,
        )
        raw = (tmp_path / "run.log").read_bytes()
        assert len(raw) <= 400 + len(sr.RUN_LOG_HEADER_PREFIX) + 200
        assert b"line 1999" in raw


class TestHarnessInfraLines:
    """F-30: Booley's own ERROR: markers are not DUT assertion failures."""

    @pytest.mark.parametrize(
        "line",
        [
            "ERROR: Verilator elaboration failed (rc=2)",
            "ERROR: iverilog compilation failed (rc=1)",
            "ERROR: vcs elaboration failed (rc=2)",
            "ERROR: Verilator executable Vfpu not found in build/",
            "ERROR: Verilator simulation timed out (900s)",
            "ERROR: missing $readmemh memory-init file — foo",
            "simulation killed: run directory /work grew by 1 bytes",
            "TRACE_OK: /work/trace.fst",
        ],
    )
    def test_infra_markers_are_not_counted(self, line):
        assert sr.is_harness_infra_line(line) is True
        assert sr.count_sva_errors(line + "\n") == 0
        assert sr.count_sva_errors_xcelium(line + "\n") == 0
        assert sr.count_sva_errors_vcs(line + "\n") == 0

    def test_missing_eda_toolchain_run_reports_zero_sva_errors(self):
        # The exact fpu F-30 shape: no verilator on PATH, so no simulator ever
        # ran — the JSON must not claim the design failed an assertion.
        log = "make: verilator: No such file or directory\nERROR: Verilator elaboration failed (rc=2)\n"
        assert sr.count_sva_errors(log) == 0

    def test_a_real_testbench_error_line_still_counts(self):
        assert sr.count_sva_errors("ERROR: reference mismatch at vector 12\n") == 1

    @pytest.mark.parametrize(
        "line",
        [
            # A TB reporting a DUT-internal error in harness-shaped prose. The
            # `\w+` spelling of the EDA-tool name swallowed all of these, dropping
            # them from the SVA count: a TB that then exits 0 goes from FAIL to
            # INCONCLUSIVE — false-PASS-adjacent.
            "ERROR: DUT compilation failed at time 100",
            "ERROR: memory elaboration failed in bank 3",
            "ERROR: fetch simulation timed out waiting for grant",
            "ERROR: alu executable path not found",
        ],
    )
    def test_testbench_prose_is_not_mistaken_for_infra(self, line):
        assert sr.is_harness_infra_line(line) is False
        assert sr.count_sva_errors(line + "\n") == 1

    @pytest.mark.parametrize(
        "line",
        [
            # ...while every spelling the harness itself emits still is infra
            # (sim_edam._ELAB_FAIL_MARKERS + the run-halves' own messages).
            "ERROR: Verilator elaboration timed out (600s)",
            "ERROR: iverilog compilation timed out (600s)",
            "ERROR: xcelium elaboration failed (rc=1)",
            "ERROR: cocotb simulation timed out (600s)",
            "ERROR: iverilog simulation timed out (900s) — last output 12s ago",
        ],
    )
    def test_every_marker_the_harness_emits_is_still_infra(self, line):
        assert sr.is_harness_infra_line(line) is True
        assert sr.count_sva_errors(line + "\n") == 0


# ---------------------------------------------------------------------------
# count_sva_errors_xcelium
# ---------------------------------------------------------------------------


class TestCountSvaErrorsXcelium:
    """Patterns frozen against real xrun 21.03-s001 logs (Phase B, ADR 0025)."""

    def test_no_errors(self):
        assert sr.count_sva_errors_xcelium("xcelium> run\n[SIM_RESULT] PASSED\n") == 0

    def test_front_end_eda_tool_errors(self):
        # Real compile/elab wording: per-EDA-tool *E lines plus xrun's roll-up.
        out = (
            "xmvlog: *E,SVILTY (src/lite_0/rtl/lite_add.sv,383|8): Referring to"
            " a datatype as super.<type_name> is not legal SystemVerilog..\n"
            "xmelab: *E,NOUNIT: Unable to find a unit named 'tb_liteX' in the"
            " libraries.\n"
            "xrun: *E,ELBERR: Error during elaboration (status 1), exiting.\n"
        )
        assert sr.count_sva_errors_xcelium(out) == 3

    def test_fatal_lines_count(self):
        # Real $fatal wording — message text arrives on the following line.
        out = "xmsim: *F,FATSEV (./src/lite_0/tb/tb_lite.sv,607): (time 120 US).\n"
        assert sr.count_sva_errors_xcelium(out) == 1

    def test_assertion_failure_counts_once(self):
        # Real SVA wording: the *E,ASRTST line carries the generic
        # "Assertion … has failed" marker too — no double counting.
        out = (
            "xmsim: *E,ASRTST (./src/lite_0/tb/tb_lite.sv,614): "
            "(time 100005 NS) Assertion tb_lite.chk_sva has failed\n"
        )
        assert sr.count_sva_errors_xcelium(out) == 1

    def test_tb_emitted_generic_markers_still_count(self):
        # A simulator-agnostic TB's own failure wording keeps its weight.
        out = "ERROR: scoreboard mismatch at vector 3\n"
        assert sr.count_sva_errors_xcelium(out) == 1

    def test_warnings_do_not_count(self):
        # Includes the real SIGTERM-kill line: warning-level, exit code (124)
        # carries the verdict instead.
        out = (
            "xmvlog: *W,UEXPSC (src/lite_0/rtl/lite_rom.sv,67|71): Ignored"
            " unexpected semicolon following SystemVerilog description"
            " keyword.\n"
            "xmsim: *W,NCTERM: Simulation received SIGTERM signal from"
            " process 1785010, user id 11427 (timeout).\n"
        )
        assert sr.count_sva_errors_xcelium(out) == 0


# ---------------------------------------------------------------------------
# count_sva_errors_vcs
# ---------------------------------------------------------------------------


class TestCountSvaErrorsVcs:
    """The VCS mirror of the xcelium tests. Build-half patterns
    (Error-/Warning-[TAG]) frozen against real vcs X-2025.06-1 logs (Phase D,
    ADR 0025); runtime Error:/Fatal: patterns PROVISIONAL — the EDA host had
    no reachable Synopsys license, so simv never ran."""

    def test_no_errors(self):
        assert (
            sr.count_sva_errors_vcs(
                "Chronologic VCS simulator\n[SIM_RESULT] PASSED\n"
                "$finish at simulation time 100 ns\n"
            )
            == 0
        )

    def test_front_end_eda_tool_error_headers(self):
        # vlogan/vcs diagnostics arrive as Error-[TAG] block headers; the
        # indented block body lines are not double-counted.
        out = (
            "Error-[SE] Syntax error\n"
            "  Following verilog source has syntax error :\n"
            "  \"src/lite_0/rtl/lite_add.sv\", 383: token is ';'\n"
            "Error-[URMI] Unresolved modules\n"
            "  Unresolved module tb_liteX in the design.\n"
        )
        assert sr.count_sva_errors_vcs(out) == 2

    def test_runtime_severity_lines_count(self):
        # simv severity tasks ($error/$fatal, default SVA action) prefix
        # Error:/Fatal: — the "failed at" wording is not double-counted.
        out = (
            'Error: "src/lite_0/tb/tb_lite.sv", 614: tb_lite.chk_sva: '
            "started at 100005ns failed at 100005ns\n"
            'Fatal: "src/lite_0/tb/tb_lite.sv", 607: tb_lite: at time 120000 ns\n'
        )
        assert sr.count_sva_errors_vcs(out) == 2

    def test_tb_emitted_generic_markers_still_count(self):
        # A simulator-agnostic TB's own failure wording keeps its weight.
        out = "ERROR: scoreboard mismatch at vector 3\n"
        assert sr.count_sva_errors_vcs(out) == 1

    def test_warnings_and_lint_do_not_count(self):
        # Warning-[RVOSFD] is real frozen vlogan X-2025.06-1 wording.
        out = (
            "Warning-[RVOSFD] Return value discarded\n"
            "Lint-[TFIPC] Too few instance port connections\n"
            "CPU time: .057 seconds to compile\n"
        )
        assert sr.count_sva_errors_vcs(out) == 0
