"""Unit tests for sim_result.py — shared simulation output parsing."""

import json

import pytest

import booley.flows.sim.result as sr

# ---------------------------------------------------------------------------
# parse_sim_verdict
# ---------------------------------------------------------------------------


class TestParseSimVerdict:
    def test_sentinel_passed(self):
        assert sr.parse_sim_verdict("[SIM_RESULT] PASSED") is True

    def test_sentinel_failed(self):
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
# format_summary
# ---------------------------------------------------------------------------


def _summary_payload(line: str) -> dict:
    """The JSON object ``format_summary`` put after the ``[SIM_SUMMARY]`` prefix."""
    assert line.startswith(sr.SIM_SUMMARY_PREFIX)
    return json.loads(line[len(sr.SIM_SUMMARY_PREFIX) :])


class TestFormatSummary:
    def test_passed_basic(self):
        payload = _summary_payload(sr.format_summary(True))
        assert payload["passed"] is True
        assert payload["sva_errors"] == 0

    def test_failed_with_sva(self):
        payload = _summary_payload(sr.format_summary(False, sva_errors=3))
        assert payload["passed"] is False
        assert payload["sva_errors"] == 3

    def test_with_vrfc_warnings(self):
        warnings = ["WARNING: [VRFC 10-3380] issue A"]
        payload = _summary_payload(sr.format_summary(True, vrfc_warnings=warnings))
        assert payload["vrfc_warnings"] == warnings

    def test_no_vrfc_key_when_empty(self):
        assert "vrfc_warnings" not in _summary_payload(sr.format_summary(True))

    def test_inconclusive_flag(self):
        payload = _summary_payload(sr.format_summary(False, inconclusive=True))
        assert payload["passed"] is False
        assert payload["inconclusive"] is True

    def test_no_inconclusive_key_when_false(self):
        assert "inconclusive" not in _summary_payload(sr.format_summary(True))


class TestHarnessInfraLines:
    """F-30: Booley's own ERROR: markers are not DUT assertion failures."""

    @pytest.mark.parametrize(
        "line",
        [
            "ERROR: Verilator elaboration failed (rc=2)",
            "ERROR: iverilog compilation failed (rc=1)",
            "ERROR: Verilator executable Vfpu not found in build/",
            "ERROR: Verilator simulation timed out (900s)",
            "ERROR: missing $readmemh memory-init file — foo",
            "simulation killed: run directory /work grew by 1 bytes",
            "simulation aborted: run directory /work grew by 1 bytes",
            "TRACE_OK: /work/trace.fst",
        ],
    )
    def test_infra_markers_are_not_counted(self, line):
        assert sr.is_harness_infra_line(line) is True
        assert sr.count_sva_errors(line + "\n") == 0

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
            "ERROR: simulation aborted: watchdog expired",
            "ERROR: simulation aborted: run directory /work grew by 1 bytes",
        ],
    )
    def test_testbench_prose_is_not_mistaken_for_infra(self, line):
        assert sr.is_harness_infra_line(line) is False
        assert sr.count_sva_errors(line + "\n") == 1

    @pytest.mark.parametrize(
        "line",
        [
            # ...while every spelling the harness itself emits still is infra
            # (the harness's elaboration-failure markers + the run-halves' own messages).
            "ERROR: Verilator elaboration timed out (600s)",
            "ERROR: iverilog compilation timed out (600s)",
            "ERROR: cocotb simulation timed out (600s)",
            "ERROR: iverilog simulation timed out (900s) — last output 12s ago",
        ],
    )
    def test_every_marker_the_harness_emits_is_still_infra(self, line):
        assert sr.is_harness_infra_line(line) is True
        assert sr.count_sva_errors(line + "\n") == 0

    @pytest.mark.parametrize(
        "line",
        [
            "ERROR: $readmemh: Cannot open memory.hex",
            "ERROR: $readmemb: unable to open image.bin",
            "ERROR: $readmemh: can't open vectors.hex",
            "ERROR: $readmemb: could not open boot.bin",
        ],
    )
    def test_supported_raw_readmem_open_failures_are_not_sva(self, line):
        assert sr.is_harness_infra_line(line) is True
        assert sr.count_sva_errors(line + "\n") == 0

    @pytest.mark.parametrize(
        "line",
        [
            "ERROR: memory controller can't open grant window",
            "ERROR: DUT could not open transaction after reset",
        ],
    )
    def test_unrelated_dut_open_errors_remain_sva(self, line):
        assert sr.is_harness_infra_line(line) is False
        assert sr.count_sva_errors(line + "\n") == 1

    @pytest.mark.parametrize("tool", ["Verilator", "iverilog", "cocotb"])
    def test_has_simulation_timeout_finds_the_run_halfs_own_line(self, tool):
        output = f"[sim] start\nERROR: {tool} simulation timed out (25s)\n"
        assert sr.has_simulation_timeout(output) is True

    @pytest.mark.parametrize(
        "output",
        [
            "ERROR: fetch simulation timed out waiting for grant\n",
            "$display: ERROR: Verilator simulation timed out (the TB says so inline)",
            "ERROR: Verilator elaboration timed out (600s)\n",
            "",
        ],
    )
    def test_has_simulation_timeout_ignores_testbench_prose(self, output):
        assert sr.has_simulation_timeout(output) is False
