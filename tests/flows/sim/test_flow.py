"""Tests for SimulateFlow — multi-config sim, cycle parsing, dry-run, reports."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.sim.flow import (
    _INCONCLUSIVE_NO_SENTINEL,
    _INCONCLUSIVE_NO_WAVEFORM,
    SimulateFlow,
    TargetResult,
    _append_batch_output_lines,
    _append_error_excerpt,
    _artifact_path_component,
    _build_display_lines,
    _build_run_script,
    _filter_tests,
    _resolve_sim_campaign_work_units,
    _test_status_line,
    parse_build_seconds,
    parse_cycles,
    parse_sva_errors,
)
from booley.flows.sim.flow import (
    TestResult as SimTestResult,  # aliased: a Test* name would be pytest-collected
)
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.runtime.project_dir import reset_cache
from booley.sim.sim_result import write_run_log

# Built-in Flow execution inside the Session Runtime.
_FLOW_ENABLED = True


def test_qualified_target_uses_declared_icarus_tool(tmp_path: Path) -> None:
    flow = object.__new__(SimulateFlow)
    flow._args = MagicMock(work_dir=tmp_path)
    with patch(
        "booley.flows.sim.flow.fusesoc_registry.resolve_ref",
        return_value=MagicMock(eda_tool="icarus"),
    ) as resolve:
        assert flow._eda_tool_for_target("::fifo:0#sim") == "icarus"
    resolve.assert_called_once_with(tmp_path, "::fifo:0#sim")


def test_human_display_caps_targets_at_three():
    results = [
        TargetResult(target=f"config_{index}", passed=True, elapsed_s=1.0) for index in range(10)
    ]

    lines = _build_display_lines(results, total_elapsed=10.0)

    assert lines == [
        "10/10 targets passed, 10.0s",
        "✓ config_0  1.0s",
        "✓ config_1  1.0s",
        "✓ config_2  1.0s",
        "... and 7 more targets",
    ]


def test_campaign_work_units_count_native_tests_and_cocotb_batches(tmp_path: Path):
    with (
        patch(
            "booley.flows.sim.flow.fusesoc_registry.target_cocotb_modules",
            return_value={"native": None, "cocotb": "test_demo"},
        ),
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={
                "native": ["smoke", "stress", "known_hang"],
                "cocotb": ["one", "two", "three"],
            },
        ),
        patch(
            "booley.flows.sim.flow._get_test_skips",
            return_value={"native": ["known_hang"]},
        ),
    ):
        units = _resolve_sim_campaign_work_units(
            tmp_path,
            "native,cocotb",
        )

    assert units == 3  # two native processes plus one cocotb batch


def test_artifact_path_component_never_embeds_unsafe_test_names():
    assert _artifact_path_component("sign_case_65") == "sign_case_65"
    encoded = _artifact_path_component("../../outside/reports")
    assert encoded.startswith("~sha256-")
    assert "/" not in encoded
    assert encoded != _artifact_path_component("../../different")


# A minimal sim `.core` for the real-fusesoc resolution e2e: the custom
# Verilator main + dump SV are compiled *sources* (decision 4), so they are
# fileset members (the cppSource main wired via --exe), and the --timing option
# set lives in flow_options.verilator_options (the wrapper spike).
_SIM_CORE_TEXT = """\
CAPI=2:
name: ::sim_demo:0
description: simulate slice fixture
filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
    file_type: systemVerilogSource
  tb:
    files:
      - tb/tb_counter.sv: {file_type: systemVerilogSource}
      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}
    tags: [tb]
  tb_cpp:
    files:
      - sim/tb_counter__main.cpp: {file_type: cppSource}
    tags: [tb]
targets:
  default:
    filesets: [rtl]
  sim:
    default_tool: verilator
    flow: sim
    flow_options:
      tool: verilator
      verilator_options: [--timing, --timescale, 1ns/1ns, "+1800-2009ext+sv", --trace, -Wno-fatal]
    filesets: [rtl, tb, tb_cpp]
    toplevel: tb_counter
"""

# Icarus counterpart: runtime trace (booley_vcd_dump $dumpvars on +trace), so no
# compile-time --trace and no Verilator --exe C++ main — just the RTL + TB.
_SIM_CORE_ICARUS_TEXT = """\
CAPI=2:
name: ::sim_demo:0
description: simulate slice fixture (icarus)
filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
    file_type: systemVerilogSource
  tb:
    files:
      - tb/tb_counter.sv: {file_type: systemVerilogSource}
      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}
    tags: [tb]
targets:
  default:
    filesets: [rtl]
  sim:
    default_tool: icarus
    flow: sim
    flow_options:
      tool: icarus
    filesets: [rtl, tb]
    toplevel: tb_counter
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_with_state(state_file: Path, slug: str = "test-ticket") -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_STATE_FILE"] = str(state_file)
    return env


def _make_state(tmp_path: Path) -> Path:
    """Create a fresh state file and return its path."""
    state_file = tmp_path / "state.json"
    DevelopmentState.load(state_file).save()
    return state_file


def _make_flow(
    tmp_path: Path,
    *,
    config: str = "lite",
    tb_top: str = "alu_tb",
    extra_args: list[str] | None = None,
    seed_core: bool = True,
) -> SimulateFlow:
    """Build a SimulateFlow with parsed args and loaded state.

    tb_top left the CLI surface (ADR 0021); a sim Target's ``toplevel`` IS its
    TB top, so it comes from the resolved Target. ADR 0022 made the ``.core`` the
    sole design-description home (the configs.toml ``tb_top`` source was removed),
    so the helper seeds it via a co-located ``.core`` whose Target ``toplevel`` is
    *tb_top* — the cheap, no-subprocess read ``tb_top_for_target(resolved=None)``
    performs (``fusesoc_registry.core_target_toplevel``). Only written for a
    single valid Target name (a comma-list / empty ``config`` is not a Target).
    """
    state_file = _make_state(tmp_path)
    report_dir = tmp_path / "reports"
    # Seed tb_top via a co-located .core the no-resolve tb_top_for_target reads
    # (BOOLEY_PROJECT_DIR=tmp_path). Skip for multi/empty config (not a Target)
    # and when the caller authors its own .core (seed_core=False). Two distinct
    # cores sharing a Target name is legal now (ADR 0030, first-wins view), but
    # this helper still seeds only one to keep the resolved Target unambiguous.
    # ADR 0039: resolve_target_selection validates every token against the
    # .core surface unconditionally, so seed one core per named config
    # (comma-lists included — the old transitional zero-.core skip is gone).
    if seed_core and config and config.strip() == config:
        for name in [c.strip() for c in config.split(",") if c.strip()]:
            core_file = tmp_path / f"{name}.core"
            if not core_file.exists():
                core_file.write_text(
                    "CAPI=2:\n"
                    f"name: ::{name}:0\n"
                    "targets:\n"
                    f"  {name}:\n"
                    "    flow: sim\n"
                    "    flow_options: {tool: verilator}\n"
                    f"    toplevel: {tb_top}\n",
                    encoding="utf-8",
                )
    argv = [
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(report_dir),
        "--target",
        config,
    ]
    if extra_args:
        argv.extend(extra_args)
    env = _env_with_state(state_file)
    project_dir = tmp_path / ".booley_project"
    if project_dir.exists():
        env["BOOLEY_PROJECT_DIR"] = str(project_dir)
    flow = SimulateFlow()
    with patch.dict(os.environ, env):
        flow.parse_args(argv)
    flow.read_state()
    return flow


# ---------------------------------------------------------------------------
# Cycle count parsing
# ---------------------------------------------------------------------------


class TestCycleParsing:
    def test_extracts_cycle_count(self):
        output = "some log\n[SIM_CYCLES] smoke 12345\nmore log"
        assert parse_cycles(output, "smoke") == 12345

    def test_large_cycle_count(self):
        output = "[SIM_CYCLES] stress 9999999"
        assert parse_cycles(output, "stress") == 9999999

    def test_no_cycle_sentinel(self):
        output = "simulation finished without cycle info"
        assert parse_cycles(output, "smoke") is None

    def test_empty_output(self):
        assert parse_cycles("", "smoke") is None

    def test_multiple_cycle_lines_selects_test_name(self):
        output = "[SIM_CYCLES] smoke 100\n[SIM_CYCLES] stress 200"
        assert parse_cycles(output, "stress") == 200

    def test_test_name_may_contain_spaces(self):
        output = "[SIM_CYCLES] pipeline smoke test 314"
        assert parse_cycles(output, "pipeline smoke test") == 314

    def test_cycle_with_extra_whitespace(self):
        output = "[SIM_CYCLES]   smoke   42"
        assert parse_cycles(output, "smoke") == 42

    def test_configured_cycle_sentinel(self):
        output = "CoreMark completed in: coremark 12345"
        assert parse_cycles(output, "coremark", ["CoreMark completed in:"]) == 12345

    def test_configured_cycle_sentinel_is_literal(self):
        output = "cycles.total: smoke 987"
        assert parse_cycles(output, "smoke", ["cycles.total:"]) == 987

    def test_configured_cycle_sentinel_overrides_default(self):
        output = "[SIM_CYCLES] smoke 10\nEXECUTED_CYCLES smoke 20"
        assert parse_cycles(output, "smoke", ["EXECUTED_CYCLES"]) == 20

    def test_overlapping_configured_sentinels_use_the_longest_literal(self):
        output = "CYCLES TOTAL smoke 20"
        assert parse_cycles(output, "smoke", ["CYCLES", "CYCLES TOTAL"]) == 20

    def test_single_legacy_cycle_record_remains_compatible(self):
        assert parse_cycles("[SIM_CYCLES] 42", "smoke") == 42

    def test_multiple_legacy_cycle_records_are_ambiguous(self):
        output = "[SIM_CYCLES] 10\n[SIM_CYCLES] 20"
        assert parse_cycles(output, "smoke") is None

    def test_legacy_cycle_record_can_be_disabled_for_a_batch(self):
        assert parse_cycles("[SIM_CYCLES] 42", "smoke", allow_legacy=False) is None

    def test_named_record_for_another_test_is_not_misattributed(self):
        assert parse_cycles("[SIM_CYCLES] stress 42", "smoke") is None


# ---------------------------------------------------------------------------
# Build-time attribution (first test of a run pays the edalize make)
# ---------------------------------------------------------------------------


class TestBuildTimeAttribution:
    """After an RTL edit the first test absorbs the whole model rebuild; the
    report must say so ("reset PASS 5.0s" with a 0.1s sim misleads timing
    triage)."""

    def test_parse_build_seconds_extracts_marker(self):
        output = "make stuff\nBOOLEY_BUILD_SECONDS: 5\n[SIM_RESULT] PASSED"
        assert parse_build_seconds(output) == 5.0

    def test_parse_build_seconds_absent_marker_is_zero(self):
        assert parse_build_seconds("no marker here") == 0.0
        assert parse_build_seconds("") == 0.0

    def test_parse_build_seconds_ignores_mid_line_mention(self):
        # Only a line-anchored marker counts — a log quoting the string must
        # not be parsed as a measurement.
        assert parse_build_seconds("echo BOOLEY_BUILD_SECONDS: 9") == 0.0

    def test_build_run_script_brackets_the_make_half(self):
        script = _build_run_script(
            ["make", "-C", "bld"], "Verilator elaboration failed", "python3 -m run"
        )
        lines = script.splitlines()
        assert lines[0] == "_booley_build_start=$(date +%s)"
        assert lines[1].startswith("make -C bld || ")
        assert 'echo "BOOLEY_BUILD_SECONDS:' in lines[2]
        assert lines[3] == "python3 -m run"
        # A broken build exits before the echo: no marker, no misattribution.
        assert " || " in lines[1] and "exit 1" in lines[1]

    def test_status_line_annotates_a_real_build(self):
        tr = SimTestResult(name="reset", passed=True, elapsed_s=5.0, build_s=5.0)
        assert "(incl. 5s build)" in _test_status_line(tr)

    def test_status_line_is_silent_on_warm_cache(self):
        tr = SimTestResult(name="reset", passed=True, elapsed_s=0.1, build_s=0.0)
        assert "build" not in _test_status_line(tr)


class TestBatchOutputLines:
    """F-6: a cocotb batch runs every test in ONE process, so a process-level
    failure gives them all the same error. Print it once, not once per test."""

    def test_shared_error_is_printed_once_and_counted(self):
        shared = "cocotb could not import the test module 'test_x': No module named 'y'"
        results = [
            SimTestResult(name=f"t{i}", passed=False, inconclusive=True, error_tail=shared)
            for i in range(3)
        ]
        lines: list[str] = []
        _append_batch_output_lines(results, lines)
        text = "\n".join(lines)

        # Every test still gets its own status line...
        for i in range(3):
            assert f"t{i}" in text
        # ...but the error body appears exactly once, and says who it covers.
        assert text.count(shared) == 1
        assert "same error for all 3 selected tests" in text

    def test_distinct_errors_are_all_printed(self):
        """Genuinely independent failures must not be collapsed into one."""
        results = [
            SimTestResult(name="t0", passed=False, error_tail="assert 1 == 2"),
            SimTestResult(name="t1", passed=False, error_tail="timeout waiting for ready"),
        ]
        lines: list[str] = []
        _append_batch_output_lines(results, lines)
        text = "\n".join(lines)
        assert "assert 1 == 2" in text
        assert "timeout waiting for ready" in text
        assert "same error for all" not in text

    def test_passing_batch_has_no_error_blocks(self):
        results = [SimTestResult(name="t0", passed=True), SimTestResult(name="t1", passed=True)]
        lines: list[str] = []
        _append_batch_output_lines(results, lines)
        text = "\n".join(lines)
        assert "error output" not in text
        assert "same error for all" not in text


# ---------------------------------------------------------------------------
# SVA error parsing (fallback path)
# ---------------------------------------------------------------------------


class TestSvaErrorParsing:
    def test_no_errors(self):
        assert parse_sva_errors("normal log output") == 0

    def test_assertion_failed(self):
        output = "Assertion foo_check FAILED at time 100ns"
        assert parse_sva_errors(output) == 1

    def test_assertion_error(self):
        output = "Assertion bar ERROR\nAssertion baz ERROR"
        assert parse_sva_errors(output) == 2


# ---------------------------------------------------------------------------
# Test name filtering
# ---------------------------------------------------------------------------


class TestFilterTests:
    def test_substring_match(self):
        tests = ["lite_smoke", "lite_stress", "lite_boot"]
        assert _filter_tests(tests, "stress") == ["lite_stress"]

    def test_multiple_matches(self):
        tests = ["lite_smoke", "lite_stress", "lite_boot"]
        assert _filter_tests(tests, "lite_") == tests

    def test_no_match(self):
        tests = ["lite_smoke", "lite_stress"]
        assert _filter_tests(tests, "full") == []

    def test_exact_match(self):
        tests = ["lite_smoke"]
        assert _filter_tests(tests, "lite_smoke") == ["lite_smoke"]


# ---------------------------------------------------------------------------
# Skip-list (tests.toml `skip` + --skip): known-hang exclusion
# ---------------------------------------------------------------------------


class TestSkipList:
    _NAMES: ClassVar[dict[str, list[str]]] = {
        "lite": ["lite_smoke", "lite_stress", "lite_boot"],
    }

    def test_config_skip_excludes_named_tests(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        with patch(
            "booley.flows.sim.flow._get_test_skips", return_value={"lite": ["lite_stress"]}
        ):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == [
                "lite_smoke",
                "lite_boot",
            ]
            assert flow._skipped_tests("lite", self._NAMES) == ["lite_stress"]

    def test_cli_skip_adds_to_config_skip(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--skip", "lite_boot"])
        with patch(
            "booley.flows.sim.flow._get_test_skips", return_value={"lite": ["lite_stress"]}
        ):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == ["lite_smoke"]
            assert sorted(flow._skipped_tests("lite", self._NAMES)) == [
                "lite_boot",
                "lite_stress",
            ]

    def test_cli_skip_comma_separated(self, tmp_path: Path):
        flow = _make_flow(
            tmp_path,
            config="lite",
            extra_args=["--skip", "lite_stress, lite_boot"],
        )
        assert flow._resolve_tests_to_run("lite", self._NAMES) == ["lite_smoke"]

    def test_explicit_test_overrides_skip(self, tmp_path: Path):
        # Naming a skipped test by hand is a clear override of the skip list.
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "lite_stress"])
        with patch(
            "booley.flows.sim.flow._get_test_skips", return_value={"lite": ["lite_stress"]}
        ):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == ["lite_stress"]
            # Nothing reported skipped — the override ran it.
            assert flow._skipped_tests("lite", self._NAMES) == []

    def test_all_skipped_is_rejected(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        with patch(
            "booley.flows.sim.flow._get_test_skips",
            return_value={"lite": list(self._NAMES["lite"])},
        ):
            result = flow._validate_runnable_tests(["lite"], self._NAMES)
            assert result is not None
            assert result.exit_code == EXIT_ERROR
            assert "no runnable tests" in result.report_text
            assert flow._resolve_tests_to_run("lite", self._NAMES) == []
            assert flow._skipped_tests("lite", self._NAMES) == self._NAMES["lite"]

    def test_no_skip_runs_every_test(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        with patch("booley.flows.sim.flow._get_test_skips", return_value={}):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == self._NAMES["lite"]
            assert flow._skipped_tests("lite", self._NAMES) == []


# ---------------------------------------------------------------------------
# --test validation: an unknown name must error, not run the default test
# (false-green footgun — contrast with --target, which is validated).
# ---------------------------------------------------------------------------


class TestUnknownTestSelector:
    _NAMES: ClassVar[dict[str, list[str]]] = {
        "lite": ["lite_smoke", "lite_stress", "lite_boot"],
    }

    def test_unknown_name_returns_error(self, tmp_path: Path):
        # The reported bug: --test no_such_test used to run the TB's default
        # test and report PASS. It must now surface an error instead.
        flow = _make_flow(
            tmp_path,
            config="lite",
            extra_args=["--test", "no_such_test"],
        )
        result = flow._validate_test_selector(["lite"], self._NAMES)
        assert result is not None
        assert result.exit_code == EXIT_ERROR
        assert "no_such_test" in result.report_text
        assert "lite" in result.report_text
        # The declared tests are listed so the fix (a typo) is one hop away.
        assert "lite_smoke" in result.report_text

    def test_valid_substring_passes(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "stress"])
        assert flow._validate_test_selector(["lite"], self._NAMES) is None

    def test_no_selector_is_noop(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        assert flow._validate_test_selector(["lite"], self._NAMES) is None

    def test_no_declared_list_passes_through(self, tmp_path: Path):
        # A target that declares no test list keeps the raw-passthrough contract
        # (the TB owns the plusarg) — matching resolve_target_selection's skip
        # while the Target list is unknown. No false-green here: with no declared
        # list there is no "known-good" name a typo could be measured against.
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "anything"])
        assert flow._validate_test_selector(["lite"], {}) is None

    def test_unknown_for_one_of_many_targets_errors(self, tmp_path: Path):
        # Valid for one target, absent from another → still an error: the target
        # missing the test would otherwise run its default and report a PASS.
        names = {"lite": ["lite_smoke"], "full": ["full_sign"]}
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "lite_smoke"])
        result = flow._validate_test_selector(["lite", "full"], names)
        assert result is not None
        assert result.exit_code == EXIT_ERROR
        assert "full" in result.report_text

    @patch(
        "booley.flows.sim.flow._get_test_names",
        return_value={"lite": ["lite_smoke", "lite_stress"]},
    )
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(
        SimulateFlow,
        "_run_target",
        side_effect=AssertionError("must not run a sim on an unknown --test"),
    )
    def test_run_rejects_unknown_test_without_executing(
        self,
        _no_run,
        _mock_backend,
        _mock_names,
        tmp_path: Path,
    ):
        # End-to-end through _run: an unknown --test short-circuits to EXIT_ERROR
        # and never reaches _run_target — so no default test runs, no false PASS.
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "typo"])
        result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert "typo" in result.report_text


# ---------------------------------------------------------------------------
# Criterion recording gated to Ticket Mode (no noise on standalone runs)
# ---------------------------------------------------------------------------


class TestCriterionGating:
    def _passed_target(self):
        from booley.flows.sim.flow import TargetResult, TestResult

        return TargetResult(
            target="lite",
            passed=True,
            tests=[TestResult(name="lite_smoke", passed=True)],
        )

    def test_no_state_file_skips_criterion(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        flow.args.state_file = None  # standalone / Interactive Mode
        flow.set_criterion = MagicMock()
        flow._record_sim_criterion(self._passed_target())
        flow.set_criterion.assert_not_called()

    def test_state_file_records_criterion(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        assert flow.args.state_file is not None  # Ticket Mode
        flow.set_criterion = MagicMock()
        flow._record_sim_criterion(self._passed_target())
        flow.set_criterion.assert_called_once()


# ---------------------------------------------------------------------------
# Multi-config splitting
# ---------------------------------------------------------------------------


class TestMultiConfig:
    def test_single_config(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        configs = [c.strip() for c in flow.args.target.split(",") if c.strip()]
        assert configs == ["lite"]

    def test_multi_config(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite,full,combo")
        configs = [c.strip() for c in flow.args.target.split(",") if c.strip()]
        assert configs == ["lite", "full", "combo"]

    def test_config_with_spaces(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config=" lite , full ")
        configs = [c.strip() for c in flow.args.target.split(",") if c.strip()]
        assert configs == ["lite", "full"]

    def test_empty_config_fails(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="")
        result = flow._run()
        assert result.exit_code == EXIT_ERROR


class TestExecutionValidation:
    def test_job_class_is_heavy(self, tmp_path: Path):
        from booley.runtime import job_slots

        flow = _make_flow(tmp_path, config="lite")
        assert flow._resolve_job_class() == job_slots.CLASS_HEAVY


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(
        SimulateFlow,
        "_dry_run_command",
        side_effect=lambda config, test, names: (
            ["sh", "-c", ":", "--config", config] + (["--test", test] if test else [])
        ),
    )
    def test_dry_run_prints_json(
        self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path, capsys
    ):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--dry-run"])
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        captured = capsys.readouterr()
        commands = json.loads(captured.out)
        assert isinstance(commands, list)
        assert len(commands) == 1
        assert "--config" in commands[0]
        assert "lite" in commands[0]

    @patch(
        "booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress", "boot"]}
    )
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(
        SimulateFlow,
        "_dry_run_command",
        side_effect=lambda config, test, names: (
            ["sh", "-c", ":", "--config", config] + (["--test", test] if test else [])
        ),
    )
    def test_dry_run_expands_tests(
        self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path, capsys
    ):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--dry-run"])
        flow._run()
        captured = capsys.readouterr()
        commands = json.loads(captured.out)
        assert len(commands) == 3  # one per test

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(
        SimulateFlow,
        "_dry_run_command",
        side_effect=lambda config, test, names: (
            ["sh", "-c", ":", "--config", config] + (["--test", test] if test else [])
        ),
    )
    def test_dry_run_with_test_filter(
        self,
        _mock_edalize,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
        capsys,
    ):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--dry-run", "--test", "smoke"])
        flow._run()
        captured = capsys.readouterr()
        commands = json.loads(captured.out)
        assert len(commands) == 1
        assert "smoke" in commands[0]

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_dry_run_multi_config(
        self,
        _mock_edalize,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
        capsys,
    ):
        flow = _make_flow(
            tmp_path,
            config="lite,lite",
            extra_args=["--dry-run", "--test", "smoke"],
        )
        flow._run()
        captured = capsys.readouterr()
        commands = json.loads(captured.out)
        # 2 configs x 1 test each
        assert len(commands) == 2

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch(
        "booley.fusesoc.fusesoc_registry.resolve_target",
        side_effect=AssertionError("dry-run must not resolve (run fusesoc)"),
    )
    def test_dry_run_edalize_shows_fusesoc_setup_without_resolving(
        self,
        _no_resolve,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
        capsys,
    ):
        # The edalize dry-run path shows the `fusesoc run --setup` command a real
        # run would execute, sourced from a cheap .core YAML read — no fusesoc
        # invocation (patched resolve_target would fail the test if it fired).
        (tmp_path / "sim.core").write_text(
            "CAPI=2:\nname: ::sim_demo:0\ntargets:\n  lite:\n    flow: sim\n"
            "    flow_options:\n      tool: verilator\n",
            encoding="utf-8",
        )
        flow = _make_flow(tmp_path, config="lite", extra_args=["--dry-run"], seed_core=False)
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        commands = json.loads(capsys.readouterr().out)
        assert len(commands) == 2  # one per test
        for cmd in commands:
            assert cmd[:2] == ["sh", "-c"]
            script = cmd[2]
            assert "run --build-root" in script and "--setup" in script
            assert "--target lite" in script
            assert "sim_demo" in script  # the resolved vlnv from the .core
            assert "make -C" in script


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


class TestCommandBuilding:
    # The legacy run_sim_batch command builder (_build_sim_command) was removed
    # with the legacy runners; both simulators now build via _prepare_sim_command
    # (covered by TestEdalizeSimPath and the real-fusesoc setup tests).

    def test_trace_scope_left_the_surface(self, tmp_path: Path):
        """--trace-scope was removed (ADR 0022): the --trace overlay traces full
        hierarchy, so simulate neither accepts nor forwards a scope."""
        import pytest

        # The flag is no longer a recognized argument (argparse exits on it)...
        with pytest.raises(SystemExit):
            _make_flow(tmp_path, extra_args=["--trace", "--trace-scope", "tb.dut"])
        # ...and the Flow exposes no trace_scope.
        flow = _make_flow(tmp_path, extra_args=["--trace"])
        assert not hasattr(flow.args, "trace_scope")


# ---------------------------------------------------------------------------
# Mock execution helpers (must precede classes that reference them in decorators)
# ---------------------------------------------------------------------------


def _mock_execute_pass(self, cmd: list[str]) -> SubprocessResult:
    """Simulate a passing test run with [SIM_SUMMARY] JSON."""
    return SubprocessResult(
        returncode=0,
        stdout='[SIM_RESULT] PASSED\n[SIM_CYCLES] 2561\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n',
        stderr="",
        duration_s=6.1,
    )


def _mock_execute_fail(self, cmd: list[str]) -> SubprocessResult:
    """Simulate a failing test run with [SIM_SUMMARY] JSON."""
    return SubprocessResult(
        returncode=1,
        stdout='[SIM_RESULT] FAILED\n[SIM_CYCLES] 8012\n[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n',
        stderr="Error: assertion failed",
        duration_s=12.3,
    )


def _mock_execute_custom_cycle_pass(self, cmd: list[str]) -> SubprocessResult:
    """Simulate a passing test run with a project-native cycle prefix."""
    return SubprocessResult(
        returncode=0,
        stdout=(
            "[SIM_RESULT] PASSED\n"
            "CoreMark completed in: coremark 31415\n"
            '[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        ),
        stderr="",
        duration_s=6.1,
    )


def _mock_execute_inconclusive(self, cmd: list[str]) -> SubprocessResult:
    """Simulate an inconclusive run — rc=0, no sentinel, no summary."""
    return SubprocessResult(
        returncode=0,
        stdout="simulation finished\nno sentinel here\n",
        stderr="",
        duration_s=3.5,
    )


# ---------------------------------------------------------------------------
# Summary-based pass/fail (replaces sentinel detection)
# ---------------------------------------------------------------------------


class TestSummaryParsing:
    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_summary_pass(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_fail)
    def test_summary_fail(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE


# ---------------------------------------------------------------------------
# Inconclusive detection
# ---------------------------------------------------------------------------


class TestInconclusiveDetection:
    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_inconclusive)
    def test_inconclusive_no_criterion(
        self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path
    ):
        """No summary, rc=0 → inconclusive; criterion NOT set."""
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert "INCONCLUSIVE" in result.report_text
        assert not flow.state.is_met("sim_pass_lite")
        # Criterion should not exist at all (skipped, not set to False)
        assert not flow.state.has_criterion("sim_pass_lite")


class TestFullRun:
    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_single_config_pass(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert result.criterion_met is True

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_fail)
    def test_single_config_fail(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert result.criterion_met is False

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_multi_test_all_pass(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_multi_config_mixed(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        """First config passes, second fails => overall FAIL."""
        call_count = 0

        def _alternating_execute(self_inner, cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SubprocessResult(
                    returncode=0,
                    stdout='[SIM_RESULT] PASSED\n[SIM_CYCLES] 100\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n',
                    duration_s=1.0,
                )
            return SubprocessResult(
                returncode=1,
                stdout='[SIM_RESULT] FAILED\n[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n',
                duration_s=2.0,
            )

        with patch.object(SimulateFlow, "_execute", _alternating_execute):
            flow = _make_flow(tmp_path, config="lite,full")
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert result.detail["targets_passed"] == 1


# ---------------------------------------------------------------------------
# Per-config criterion setting
# ---------------------------------------------------------------------------


class TestCriterionSetting:
    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_sets_sim_pass_lite(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        flow._run()
        assert flow.state.is_met("sim_pass_lite")

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_fail)
    def test_sets_sim_pass_full_false(
        self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path
    ):
        flow = _make_flow(tmp_path, config="full")
        flow._run()
        assert not flow.state.is_met("sim_pass_full")

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_sets_criteria_per_config(
        self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path
    ):
        """Multi-config sets separate criteria."""
        call_count = 0

        def _mixed(self_inner, cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SubprocessResult(
                    returncode=0,
                    stdout='[SIM_RESULT] PASSED\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n',
                    duration_s=1.0,
                )
            return SubprocessResult(
                returncode=1,
                stdout='[SIM_RESULT] FAILED\n[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n',
                duration_s=1.0,
            )

        with patch.object(SimulateFlow, "_execute", _mixed):
            flow = _make_flow(tmp_path, config="lite,full")
            flow._run()
        assert flow.state.is_met("sim_pass_lite")
        assert not flow.state.is_met("sim_pass_full")

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_inconclusive)
    def test_inconclusive_skips_criterion(
        self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path
    ):
        """Inconclusive result must NOT set any criterion."""
        flow = _make_flow(tmp_path, config="lite")
        flow._run()
        assert not flow.state.has_criterion("sim_pass_lite")


# ---------------------------------------------------------------------------
# Structured report generation
# ---------------------------------------------------------------------------


class TestReportGeneration:
    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_writes_config_report(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        flow._run()
        report_path = tmp_path / "reports" / "sim_lite.json"
        assert report_path.exists()
        report = json.loads(report_path.read_text())
        assert report["flow"] == "sim"
        assert report["target"] == "lite"
        assert report["tb_top"] == "alu_tb"
        assert report["passed"] is True
        assert len(report["tests"]) == 2
        assert report["tests"][0]["name"] == "smoke"
        assert report["tests"][0]["cycles"] == 2561

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["coremark"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_custom_cycle_pass)
    def test_configured_cycle_sentinel_reaches_mcp_and_json_reports(
        self,
        _mock_edalize,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text(
            '[flows.sim]\ncycle_sentinels = ["CoreMark completed in:"]\n',
            encoding="utf-8",
        )

        result = _make_flow(tmp_path, config="lite")._run()

        assert "31,415 cycles" in result.report_text
        report = json.loads((tmp_path / "reports" / "sim_lite.json").read_text())
        assert report["tests"][0]["cycles"] == 31415

    def test_grouped_hdl_run_preserves_each_test_log_after_later_failure(
        self,
        tmp_path: Path,
    ):
        """Each HDL process gets durable evidence before the next one starts."""
        flow = _make_flow(tmp_path, config="lite")
        build_root = tmp_path / "build"
        build_root.mkdir()
        flow._record_run_log_dir("lite", build_root)
        first_path = tmp_path / "reports/artifacts/sim_lite/tests/smoke/run.log"
        first_bytes: bytes | None = None
        calls = 0

        def execute(_cmd):
            nonlocal calls, first_bytes
            calls += 1
            if calls == 1:
                return SubprocessResult(
                    returncode=0,
                    stdout="SMOKE_ONLY\n[SIM_RESULT] PASSED\n",
                    duration_s=0.1,
                )
            assert first_path.is_file(), "first artifact must land before test two starts"
            first_bytes = first_path.read_bytes()
            return SubprocessResult(
                returncode=1,
                stdout="STRESS_ONLY\n[SIM_RESULT] FAILED\n",
                duration_s=0.1,
            )

        with (
            patch(
                "booley.flows.sim.flow._get_test_names",
                return_value={"lite": ["smoke", "stress"]},
            ),
            patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED),
            patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"]),
            patch.object(flow, "_execute", side_effect=execute),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        assert calls == 2
        report = json.loads((tmp_path / "reports/sim_lite.json").read_text())
        assert [test["name"] for test in report["tests"]] == ["smoke", "stress"]
        pointers = [test["artifacts"]["run_log"] for test in report["tests"]]
        assert pointers == [
            "reports/artifacts/sim_lite/tests/smoke/run.log",
            "reports/artifacts/sim_lite/tests/stress/run.log",
        ]
        smoke_log, stress_log = (tmp_path / pointer for pointer in pointers)
        assert smoke_log.read_bytes() == first_bytes
        assert "SMOKE_ONLY" in smoke_log.read_text()
        assert "STRESS_ONLY" not in smoke_log.read_text()
        assert "STRESS_ONLY" in stress_log.read_text()
        assert "SMOKE_ONLY" not in stress_log.read_text()
        # Compatibility/live-tail contract: the Target-level file remains and
        # holds the most recently completed test.
        assert "STRESS_ONLY" in (build_root / "run.log").read_text()
        assert "SMOKE_ONLY" not in (build_root / "run.log").read_text()

    def test_single_hdl_test_report_has_the_same_run_log_artifact(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "smoke"])
        with (
            patch(
                "booley.flows.sim.flow._get_test_names",
                return_value={"lite": ["smoke", "stress"]},
            ),
            patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED),
            patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"]),
            patch.object(
                flow,
                "_execute",
                return_value=SubprocessResult(
                    returncode=0,
                    stdout="SINGLE_ONLY\n[SIM_RESULT] PASSED\n",
                    duration_s=0.1,
                ),
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        report = json.loads((tmp_path / "reports/sim_lite.json").read_text())
        assert len(report["tests"]) == 1
        pointer = report["tests"][0]["artifacts"]["run_log"]
        assert pointer == "reports/artifacts/sim_lite/tests/smoke/run.log"
        assert "SINGLE_ONLY" in (tmp_path / pointer).read_text()

    def test_completed_target_survives_later_campaign_crash(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite,full")
        first = TargetResult(
            target="lite",
            tb_top="alu_tb",
            eda_tool="verilator",
            passed=True,
            elapsed_s=1.0,
            tests=[SimTestResult(name="smoke", passed=True, elapsed_s=1.0)],
        )
        calls = 0

        def run_target(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first
            raise RuntimeError("simulated outer interruption")

        with (
            patch.dict(os.environ, {"BOOLEY_RUN_ID": "sim-checkpoint-1"}),
            patch.object(
                flow,
                "_resolve_run_targets",
                return_value=(["lite", "full"], {}),
            ),
            patch.object(flow, "_maybe_dispatch_special_run", return_value=None),
            patch.object(flow, "_tb_top_for_target", return_value="alu_tb"),
            patch.object(flow, "_run_target", side_effect=run_target),
            patch.object(flow, "_compile_command_str", return_value=None),
            patch.object(flow, "_fileset_for_report", return_value=None),
            patch.object(flow, "_artifacts_for", return_value={}),
            pytest.raises(RuntimeError, match="outer interruption"),
        ):
            flow._run()

        report_dir = tmp_path / "reports"
        target_report = json.loads((report_dir / "sim_lite.json").read_text())
        assert target_report["run_id"] == "sim-checkpoint-1"
        invocation_dirs = sorted((report_dir / "sim").iterdir())
        progress = json.loads((invocation_dirs[-1] / "progress.json").read_text())
        assert progress["run_id"] == "sim-checkpoint-1"
        assert progress["completed_targets"] == ["lite"]
        assert progress["pending_targets"] == ["full"]
        assert (invocation_dirs[-1] / "targets" / "sim_lite.json").is_file()

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_no_report_dir_skips(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        """No --report-dir => no crash, no report."""
        state_file = _make_state(tmp_path)
        env = _env_with_state(state_file)
        # ADR 0039: selection validates against a real .core surface.
        # The replacement targets FuseSoC's upstream CAPI2 ``tool`` field.
        (tmp_path / "lite.core").write_text(
            "CAPI=2:\nname: ::lite:0\ntargets:\n  lite:\n    flow: sim\n"
            "    flow_options: {tool: verilator}\n    toplevel: alu_tb\n",
            encoding="utf-8",
        )
        flow = SimulateFlow()
        with patch.dict(os.environ, env):
            flow.parse_args(
                [
                    "--work-dir",
                    str(tmp_path),
                    "--target",
                    "lite",
                ]
            )
        flow.read_state()
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_mcp_default_timeout_allows_trace_retry(self):
        assert SimulateFlow.default_timeout == 1290

    def test_default_timeout(self, tmp_path: Path):
        flow = _make_flow(tmp_path)
        assert flow._get_timeout() == 600  # 600000ms -> 600s

    def test_custom_timeout(self, tmp_path: Path):
        flow = _make_flow(tmp_path, extra_args=["--timeout", "120000"])
        assert flow._get_timeout() == 120

    def test_trace_timeout_has_cleanup_margin(self, tmp_path: Path):
        flow = _make_flow(tmp_path, extra_args=["--trace"])
        assert flow._get_timeout() == 690

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_timeout_results_in_fail(
        self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path
    ):
        def _timeout_execute(self_inner, cmd):
            return SubprocessResult(
                returncode=-1,
                stdout="partial output",
                stderr="",
                timed_out=True,
                duration_s=600.0,
            )

        with patch.object(SimulateFlow, "_execute", _timeout_execute):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        report = json.loads((tmp_path / "reports" / "sim_lite.json").read_text(encoding="utf-8"))
        assert report["tests"][0]["timed_out"] is True
        assert report["tests"][0]["verdict"] == "timeout"
        assert "TIMEOUT: simulation exceeded" in report["tests"][0]["error_tail"]


# ---------------------------------------------------------------------------
# Error excerpt extraction
# ---------------------------------------------------------------------------


class TestAppendErrorExcerpt:
    """_append_error_excerpt must surface failure cause, not noise."""

    def test_markers_found(self):
        tail = "line 1\n%Error: something broke\nline 3"
        lines: list[str] = []
        _append_error_excerpt(tail, lines)
        assert any("%Error" in ln for ln in lines)

    def test_warnings_filtered_in_fallback(self):
        """When no markers match, warnings should be stripped from fallback."""
        tail = (
            "actual useful output\n"
            "/work/rtl/foo.sv:10: warning: @* is sensitive to all 16 words\n"
            "/work/rtl/foo.sv:11: warning: @* is sensitive to all 16 words\n"
            "/work/rtl/foo.sv:12: warning: @* is sensitive to all 16 words\n"
        )
        lines: list[str] = []
        _append_error_excerpt(tail, lines)
        assert len(lines) == 1
        assert "actual useful output" in lines[0]
        assert not any("warning:" in ln for ln in lines)

    def test_fallback_all_noise_shows_raw_tail(self):
        """If every line is noise, fall back to showing raw last 3."""
        tail = "warning: foo\nwarning: bar\nwarning: baz\n"
        lines: list[str] = []
        _append_error_excerpt(tail, lines)
        assert len(lines) == 3

    def test_expanded_markers_catch_fail(self):
        tail = "some log\nFAIL: mismatch at output\nmore log"
        lines: list[str] = []
        _append_error_excerpt(tail, lines)
        assert any("FAIL" in ln for ln in lines)

    def test_expanded_markers_catch_error(self):
        tail = "some log\nERROR: unexpected value\nmore log"
        lines: list[str] = []
        _append_error_excerpt(tail, lines)
        assert any("ERROR" in ln for ln in lines)

    def test_vcd_info_filtered(self):
        """VCD/FST info lines should be treated as noise in fallback."""
        tail = "real failure here\nVCD info: dumpfile dump.vcd opened for output.\n"
        lines: list[str] = []
        _append_error_excerpt(tail, lines)
        assert len(lines) == 1
        assert "real failure" in lines[0]


# ---------------------------------------------------------------------------
# Elaboration failure detection
# ---------------------------------------------------------------------------


def _mock_execute_elab_fail_verilator(self, cmd: list[str]) -> SubprocessResult:
    """Simulate a Verilator elaboration failure — no sim output, just elab error."""
    return SubprocessResult(
        returncode=1,
        stdout="",
        stderr="ERROR: Verilator elaboration failed (rc=1)\n",
        duration_s=4.2,
    )


def _mock_execute_elab_fail_iverilog(self, cmd: list[str]) -> SubprocessResult:
    """Simulate an iverilog compilation failure."""
    return SubprocessResult(
        returncode=1,
        stdout="",
        stderr="ERROR: iverilog compilation failed (rc=1)\n",
        duration_s=2.0,
    )


def _mock_execute_raw_verilator_pass(self, cmd: list[str]) -> SubprocessResult:
    """Raw Verilator run: TB sentinels but NO [SIM_SUMMARY] (runner is bypassed)."""
    return SubprocessResult(
        returncode=0,
        stdout="[SIM_RESULT] PASSED\n[SIM_CYCLES] 4242\n",
        stderr="",
        duration_s=5.0,
    )


class TestEdalizeSimPath:
    """ADR 0019: Verilator/Icarus pass-fail runs go through the Edalize flow,
    and the thin post-processor recovers the [SIM_SUMMARY] the bypassed runner
    used to print."""

    # test_use_edalize_sim_gates_on_backend_and_trace was deleted with
    # `_use_edalize_sim` itself (ADR 0037): every selection rides the Edalize
    # flow.

    def test_icarus_run_cmd_ships_through_iverilog_run(self, tmp_path: Path):
        """Icarus runs are re-homed to booley.sim.iverilog_run (the edalize
        Icarus run-half) — the mirror of the verilator_run wiring, not `make run`.
        --trace rides along; the run-half adds the +trace plusarg itself."""
        traced = _make_flow(tmp_path, extra_args=["--trace"])
        cmd = traced._icarus_run_cmd("build/rel/dir", ["test_id=2"])
        assert cmd[:3] == ["python3", "-m", "booley.sim.iverilog_run"]
        assert cmd[cmd.index("--build-dir") + 1] == "build/rel/dir"
        assert "--top" not in cmd  # vvp image is discovered, no toplevel needed
        assert "--trace" in cmd
        assert "--plusarg=test_id=2" in cmd
        # A non-trace run omits --trace (no waveform lifecycle).
        plain = _make_flow(tmp_path)._icarus_run_cmd("d", [])
        assert "--trace" not in plain

    def test_run_cmds_forward_configured_sentinels(self, tmp_path: Path):
        """Configured verdict sentinels ride the run-half command into the sandbox."""
        with patch(
            "booley.flows.sim.flow._resolve_sim_sentinels",
            return_value=(["ALL TESTS PASSED."], ["ERROR!", "TIMEOUT"]),
        ):
            ic = _make_flow(tmp_path)._icarus_run_cmd("d", [])
            vl = _make_flow(tmp_path)._verilator_run_cmd("d", "tb", [])
        for cmd in (ic, vl):
            # `=` form (F-12): one token per sentinel, same encoding as
            # mutation_tester's forwarding of the same flags.
            passes = [a.split("=", 1)[1] for a in cmd if a.startswith("--pass-sentinel=")]
            fails = [a.split("=", 1)[1] for a in cmd if a.startswith("--fail-sentinel=")]
            assert passes == ["ALL TESTS PASSED."]
            assert fails == ["ERROR!", "TIMEOUT"]

    def test_run_cmds_omit_sentinel_flags_when_unset(self, tmp_path: Path):
        """No config -> no sentinel flags (run-half falls back to built-in markers)."""
        with patch(
            "booley.flows.sim.flow._resolve_sim_sentinels",
            return_value=([], []),
        ):
            cmd = _make_flow(tmp_path)._icarus_run_cmd("d", [])
        assert not any(a.startswith(("--pass-sentinel", "--fail-sentinel")) for a in cmd)

    def test_resolve_sim_sentinels_reads_booley_toml(self, tmp_path: Path):
        """[flows.sim].pass_sentinels/fail_sentinels are read from booley.toml."""
        from booley.flows.sim.flow import _resolve_sim_sentinels

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            "[flows.sim]\n"
            'pass_sentinels = ["ALL TESTS PASSED."]\n'
            'fail_sentinels = ["ERROR!", "TIMEOUT"]\n',
            encoding="utf-8",
        )
        passes, fails = _resolve_sim_sentinels(tmp_path)
        assert passes == ["ALL TESTS PASSED."]
        assert fails == ["ERROR!", "TIMEOUT"]
        # Unconfigured project -> empty lists (built-in markers used downstream).
        assert _resolve_sim_sentinels(tmp_path / "nowhere") == ([], [])

    def test_resolve_cycle_sentinels_reads_booley_toml(self, tmp_path: Path):
        """[flows.sim].cycle_sentinels are read from booley.toml."""
        from booley.flows.sim.flow import _resolve_cycle_sentinels

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            '[flows.sim]\ncycle_sentinels = ["CoreMark completed in:", "EXECUTED_CYCLES"]\n',
            encoding="utf-8",
        )
        assert _resolve_cycle_sentinels(tmp_path) == [
            "CoreMark completed in:",
            "EXECUTED_CYCLES",
        ]
        # Unconfigured project -> built-in marker used by parse_cycles.
        assert _resolve_cycle_sentinels(tmp_path / "nowhere") == []

    def test_run_cmds_forward_configured_trace_args(self, tmp_path: Path):
        """[flows.sim].trace_args rides both run-half commands when tracing.

        F-15: Ibex's VerilatorSimCtrl takes only getopt `--trace=FILE`, so
        Booley's generic plusarg pair produced a header-only FST that still
        reported PASS. The contract has to reach both Runtime run-halves.
        """
        with patch(
            "booley.flows.sim.flow._resolve_trace_args",
            return_value=["--trace={file}"],
        ):
            ic = _make_flow(tmp_path, extra_args=["--trace"])._icarus_run_cmd("d", [])
            vl = _make_flow(tmp_path, extra_args=["--trace"])._verilator_run_cmd("d", "tb", [])
        for cmd in (ic, vl):
            # The `=` form keeps argparse from reading the value as an option.
            assert "--trace-arg=--trace={file}" in cmd

    def test_run_cmds_omit_trace_args_when_not_tracing(self, tmp_path: Path):
        """No --trace -> no trace contract flags, even when configured."""
        with patch(
            "booley.flows.sim.flow._resolve_trace_args",
            return_value=["--trace={file}"],
        ):
            cmd = _make_flow(tmp_path)._verilator_run_cmd("d", "tb", [])
        assert not any(a.startswith("--trace-arg") for a in cmd)

    def test_resolve_trace_args_reads_booley_toml(self, tmp_path: Path):
        """[flows.sim].trace_args is read from booley.toml; unset -> empty."""
        from booley.flows.sim.flow import _resolve_trace_args

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            '[flows.sim]\ntrace_args = ["--trace={file}"]\n',
            encoding="utf-8",
        )
        assert _resolve_trace_args(tmp_path) == ["--trace={file}"]
        # Unconfigured -> empty, so the run-half keeps its own convention.
        assert _resolve_trace_args(tmp_path / "nowhere") == []

    def test_run_cmds_forward_rundir_budget(self, tmp_path: Path):
        """The disk-budget knob (SETUP-25) rides both run-half commands."""
        with patch(
            "booley.flows.sim.flow._resolve_max_rundir_bytes",
            return_value=2048,
        ):
            ic = _make_flow(tmp_path)._icarus_run_cmd("d", [])
            vl = _make_flow(tmp_path)._verilator_run_cmd("d", "tb", [])
        for cmd in (ic, vl):
            assert cmd[cmd.index("--max-rundir-bytes") + 1] == "2048"

    def test_run_cmds_omit_rundir_budget_when_disabled(self, tmp_path: Path):
        """A 0 budget disables the guard -> no flag (run-half default is off)."""
        with patch(
            "booley.flows.sim.flow._resolve_max_rundir_bytes",
            return_value=0,
        ):
            ic = _make_flow(tmp_path)._icarus_run_cmd("d", [])
            vl = _make_flow(tmp_path)._verilator_run_cmd("d", "tb", [])
        assert "--max-rundir-bytes" not in ic
        assert "--max-rundir-bytes" not in vl

    def test_resolve_max_rundir_bytes_reads_booley_toml(self, tmp_path: Path):
        """[flows.sim].max_rundir_bytes overrides the default; unset -> default."""
        from booley.flows.sim.flow import (
            _DEFAULT_MAX_RUNDIR_BYTES,
            _resolve_max_rundir_bytes,
        )

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            "[flows.sim]\nmax_rundir_bytes = 123456\n",
            encoding="utf-8",
        )
        assert _resolve_max_rundir_bytes(tmp_path) == 123456
        # Unconfigured project -> the sane multi-GB default (guard on by default).
        assert _resolve_max_rundir_bytes(tmp_path / "nowhere") == _DEFAULT_MAX_RUNDIR_BYTES

    def test_resolve_sim_timeout_ms_reads_booley_toml(self, tmp_path: Path):
        """[flows.sim].timeout_ms overrides the default; unset -> default (F4)."""
        from booley.flows.sim.flow import (
            _DEFAULT_TIMEOUT_MS,
            _resolve_sim_timeout_ms,
        )

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            "[flows.sim]\ntimeout_ms = 1800000\n",
            encoding="utf-8",
        )
        assert _resolve_sim_timeout_ms(tmp_path) == 1800000
        # Unconfigured project -> the built-in 600s default.
        assert _resolve_sim_timeout_ms(tmp_path / "nowhere") == _DEFAULT_TIMEOUT_MS

    def test_effective_timeout_ms_precedence(self, tmp_path: Path):
        """--timeout arg wins over [flows.sim].timeout_ms wins over default (F4)."""
        from booley.flows.sim.flow import _DEFAULT_TIMEOUT_MS

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            "[flows.sim]\ntimeout_ms = 1800000\n",
            encoding="utf-8",
        )
        # No --timeout arg -> the configured knob is honored.
        flow = _make_flow(tmp_path)
        assert flow.args.timeout is None
        assert flow._effective_timeout_ms() == 1800000
        # Explicit --timeout arg wins over the configured knob.
        flow_arg = _make_flow(tmp_path, extra_args=["--timeout", "42000"])
        assert flow_arg._effective_timeout_ms() == 42000
        # Neither set -> the built-in default.
        bare = tmp_path / "bare"
        bare.mkdir()
        assert _make_flow(bare)._effective_timeout_ms() == _DEFAULT_TIMEOUT_MS

    def test_sim_plusargs_resolves_test_id(self, tmp_path: Path):
        flow = _make_flow(tmp_path)
        names = {"lite": ["smoke", "stress", "boot"]}
        assert flow._sim_plusargs("lite", "stress", names) == ["test_id=1"]
        # Unknown test name -> no selector (binary runs its default test).
        assert flow._sim_plusargs("lite", "adhoc", names) == []
        assert flow._sim_plusargs("lite", None, names) == []

    def test_sim_plusargs_honors_tests_toml_select_template(self, tmp_path: Path, monkeypatch):
        """A Target's tests.toml `select` template overrides the default (dec. 16)."""
        from booley.config import project_config

        flow = _make_flow(tmp_path)
        names = {"lite": ["smoke", "stress", "boot"]}
        # `+test={name}` renders the test name; `+` is stripped (sim_run_command
        # re-adds it). Targets without a template stay on the default +test_id=N.
        monkeypatch.setitem(project_config.TEST_SELECT, "lite", "+test={name}")
        assert flow._sim_plusargs("lite", "stress", names) == ["test=stress"]
        assert flow._sim_plusargs("lite", None, names) == []

    def test_sim_plusargs_forwards_resolved_typed_parameters(self, tmp_path: Path):
        flow = _make_flow(tmp_path)
        parameters = {
            "BAUD": {"datatype": "int", "paramtype": "plusarg", "default": 115200},
            "VERBOSE": {"datatype": "bool", "paramtype": "plusarg", "default": True},
            "LABEL": {"datatype": "str", "paramtype": "plusarg", "default": "fast run"},
            "DECLARE_ONLY": {"datatype": "str", "paramtype": "plusarg"},
            "WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 32},
        }
        assert flow._sim_plusargs("lite", None, {}, parameters) == [
            "BAUD=115200",
            "VERBOSE=1",
            "LABEL=fast run",
        ]

    def test_test_selector_overrides_same_named_target_plusarg(self, tmp_path: Path):
        flow = _make_flow(tmp_path)
        parameters = {
            "test_id": {"datatype": "int", "paramtype": "plusarg", "default": 9},
            "BAUD": {"datatype": "int", "paramtype": "plusarg", "default": 57600},
        }
        assert flow._sim_plusargs("lite", "stress", {"lite": ["smoke", "stress"]}, parameters) == [
            "BAUD=57600",
            "test_id=1",
        ]

    @staticmethod
    def _fake_resolved(tmp_path: Path, *, toplevel: str = "tb_counter", parameters=None):
        """A ResolvedTarget under simulate's work root (FuseSoC's build dir)."""
        from booley.fusesoc import fusesoc_registry

        build_root = (
            tmp_path
            / ".booley_project"
            / ".runtime"
            / "edalize"
            / "sim"
            / "lite"
            / "sim_demo_0"
            / "sim"
        )
        return fusesoc_registry.ResolvedTarget(
            name="lite",
            vlnv="::sim_demo:0",
            toplevel=toplevel,
            eda_tool="verilator",
            files=(),
            parameters=parameters or {},
            build_root=build_root,
            edam_path=build_root / "sim_demo_0.eda.yml",
        )

    def test_prepare_sim_command_resolves_then_builds_and_runs(self, tmp_path: Path):
        """Resolve the sim Target, then `make` (build) + run `V<top>` in one step."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        captured = {}

        def fake_resolve(target, *, project_root, build_root, **kw):
            captured.update(target=target, build_root=build_root)
            return self._fake_resolved(tmp_path)

        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=fake_resolve,
            ),
            patch("booley.flows.sim.flow.validate_top_parameter_intent") as guard,
        ):
            cmd = flow._prepare_sim_command(
                "lite",
                None,
                {},
            )
        guard.assert_called_once()

        # Resolution forwards the Target name and simulate's isolated build root.
        assert captured["target"] == "lite"
        assert captured["build_root"] == (
            tmp_path / ".booley_project" / ".runtime" / "edalize" / "sim" / "lite"
        )
        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        # Build failure must surface the canonical elab marker; the run binary
        # (named from the resolved toplevel, decision 12) only fires on a clean
        # build, over the FuseSoC build dir.
        assert "Verilator elaboration failed" in script
        assert "make" in script
        assert ".booley_project/.runtime/edalize/sim/lite/sim_demo_0/sim" in script
        # Verilator run half ships through booley.sim.verilator_run, which builds
        # V<top> from --top (decision 12) over the FuseSoC build dir.
        assert "booley.sim.verilator_run" in script
        assert "--top tb_counter" in script

    def test_prepare_sim_command_forwards_test_plusarg(self, tmp_path: Path):
        """The run half forwards the resolved `+test_id=N` selector (run kept)."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        names = {"lite": ["smoke", "stress", "boot"]}
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: self._fake_resolved(tmp_path),
        ):
            cmd = flow._prepare_sim_command(
                "lite",
                "stress",
                names,
            )
        # verilator_run re-adds the leading `+`, so the selector ships stripped.
        assert "--plusarg=test_id=1" in cmd[2]

    def test_prepare_sim_command_forwards_target_plusarg_parameters(self, tmp_path: Path):
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        resolved = self._fake_resolved(
            tmp_path,
            parameters={
                "uart_baudrate": {
                    "datatype": "int",
                    "paramtype": "plusarg",
                    "default": 57600,
                }
            },
        )
        with patch.object(fusesoc_registry, "resolve_target", return_value=resolved):
            cmd = flow._prepare_sim_command("lite", None, {})
        assert "--plusarg=uart_baudrate=57600" in cmd[2]

    def test_prepare_sim_command_applies_doctor_bad_overlay(self, tmp_path: Path, monkeypatch):
        from booley.fusesoc import fusesoc_registry, selftest_overlay

        flow = _make_flow(tmp_path)
        project_dir = tmp_path / ".booley_project"
        overlay_file = (
            selftest_overlay.bad_overlay_dir(project_dir, "sim") / "firmware" / "firmware.hex"
        )
        overlay_file.parent.mkdir(parents=True)
        overlay_file.write_text("bad\n", encoding="utf-8")
        expected_root = (
            tmp_path
            / ".booley_project"
            / ".runtime"
            / "edalize"
            / "sim"
            / "lite-doctor-selftest-bad"
        )
        staged = expected_root / "sim_demo_0" / "sim" / "firmware" / "firmware.hex"
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
        monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
        reset_cache()

        def fake_resolve(target, *, project_root, build_root, **kwargs):
            assert build_root == expected_root
            resolved = self._fake_resolved(tmp_path)
            return fusesoc_registry.ResolvedTarget(
                name=resolved.name,
                vlnv=resolved.vlnv,
                toplevel=resolved.toplevel,
                eda_tool=resolved.eda_tool,
                files=resolved.files,
                parameters=resolved.parameters,
                build_root=staged.parents[1],
                edam_path=staged.parents[1] / "sim_demo_0.eda.yml",
            )

        with patch.object(fusesoc_registry, "resolve_target", side_effect=fake_resolve):
            flow._prepare_sim_command("lite", "known_bad", {})

        assert staged.read_text(encoding="utf-8") == "bad\n"

    def test_ordinary_sim_does_not_apply_doctor_bad_overlay(self, tmp_path: Path, monkeypatch):
        from booley.fusesoc import selftest_overlay

        flow = _make_flow(tmp_path)
        project_dir = tmp_path / ".booley_project"
        overlay_file = (
            selftest_overlay.bad_overlay_dir(project_dir, "sim") / "firmware" / "firmware.hex"
        )
        overlay_file.parent.mkdir(parents=True)
        overlay_file.write_text("bad\n", encoding="utf-8")
        build_root = tmp_path / "build"
        staged = build_root / "firmware" / "firmware.hex"
        staged.parent.mkdir(parents=True)
        staged.write_text("good\n", encoding="utf-8")
        monkeypatch.delenv(selftest_overlay.INTERNAL_KIND_ENV, raising=False)

        flow._stage_doctor_selftest_overlay(build_root)

        assert staged.read_text(encoding="utf-8") == "good\n"

    def test_run_cmds_accept_getopt_style_test_selector(self, tmp_path: Path):
        """Option-like selectors stay values of the run-half's --plusarg."""
        selector = "--meminit=ram,firmware.elf"
        flow = _make_flow(tmp_path)
        ic = flow._icarus_run_cmd("d", [selector])
        vl = flow._verilator_run_cmd("d", "tb", [selector])
        for cmd in (ic, vl):
            assert f"--plusarg={selector}" in cmd

    def test_prepare_sim_command_trace_builds_overlay_and_ships_trace_flag(
        self,
        tmp_path: Path,
    ):
        """`--trace` generates a trace overlay Target, resolves *that* under its own
        trace-variant build root, and ships the run half through verilator_run."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, extra_args=["--trace"])
        overlay_file = tmp_path / "demo.booleytrace.core"
        overlay_file.write_text("CAPI=2:\nname: ::sim_demo-booleytrace:0\n")
        overlay = fusesoc_registry.TraceOverlay(
            core_file=overlay_file,
            vlnv="::sim_demo-booleytrace:0",
        )
        trace_build_root = (
            tmp_path / ".booley_project" / ".runtime" / "edalize" / "sim" / "lite-trace"
        )
        trace_build_root.mkdir(parents=True)
        (trace_build_root / "stale-model-and-waveform").write_text("old RTL", encoding="utf-8")
        captured: dict = {}

        def fake_resolve(target, *, project_root, build_root, vlnv=None, **kw):
            assert not build_root.exists(), "trace resolution must not see an earlier staged model"
            captured.update(target=target, build_root=build_root, vlnv=vlnv)
            return self._fake_resolved(tmp_path)

        with (
            patch.object(
                fusesoc_registry,
                "write_trace_overlay",
                return_value=overlay,
            ) as mk_overlay,
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=fake_resolve,
            ),
        ):
            cmd = flow._prepare_sim_command(
                "lite",
                None,
                {},
            )
        # The overlay is generated for the requested Target, resolved by its derived
        # VLNV under the distinct trace-variant build root, then cleaned up.
        mk_overlay.assert_called_once()
        assert captured["vlnv"] == "::sim_demo-booleytrace:0"
        assert captured["build_root"] == (
            tmp_path / ".booley_project" / ".runtime" / "edalize" / "sim" / "lite-trace"
        )
        assert not overlay_file.exists()  # cleanup() removed the transient overlay
        script = cmd[2]
        assert "booley.sim.verilator_run" in script
        assert "--trace" in script
        assert "--trace-scope" not in script  # full-hierarchy trace, no scope knob

    def test_trace_build_root_is_reset_once_for_a_multi_test_run(self, tmp_path: Path):
        """Selected tests share one fresh trace build, never a pre-run stale one."""
        flow = _make_flow(tmp_path, extra_args=["--trace"])
        build_root = tmp_path / "trace-build"
        build_root.mkdir()
        (build_root / "stale.fst").write_text("old trace", encoding="utf-8")

        flow._reset_trace_build_root(build_root)
        assert not build_root.exists()

        build_root.mkdir()
        current_model = build_root / "Vcurrent"
        current_model.write_text("fresh model", encoding="utf-8")
        flow._reset_trace_build_root(build_root)

        assert current_model.exists(), "later tests must reuse this invocation's fresh build"

    def test_trace_missing_waveform_fails_the_test(self, tmp_path: Path):
        """--trace that produces no waveform fails loudly (no silent PASS)."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, extra_args=["--trace"])
        # Sim PASSED, but verilator_run printed no TRACE_OK → trace silently no-op'd.
        no_trace = SubprocessResult(
            returncode=0,
            stdout="[SIM_RESULT] PASSED\nERROR: trace requested but no queryable .fst store or .vcd was produced\n",
            stderr="",
            duration_s=1.0,
        )
        with (
            patch.object(
                fusesoc_registry,
                "write_trace_overlay",
                return_value=fusesoc_registry.TraceOverlay(
                    core_file=tmp_path / "x.booleytrace.core",
                    vlnv="::d-booleytrace:0",
                ),
            ),
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=lambda *a, **k: self._fake_resolved(tmp_path),
            ),
            patch.object(
                SimulateFlow,
                "_execute",
                return_value=no_trace,
            ),
        ):
            result = flow._run_single_test("lite", None, {})
        assert result.passed is False
        assert "no waveform" in result.error_tail

    def test_trace_missing_on_passing_sim_is_inconclusive_not_fail(self, tmp_path: Path):
        """QA_REPORT B5.1: a trace-infra failure on a PASSING sim is a Flow
        error (inconclusive), never a design FAIL, and the trace incident's
        ``ERROR:`` banner must not be miscounted as an SVA assertion."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, extra_args=["--trace"])
        no_trace = SubprocessResult(
            returncode=0,
            stdout=(
                "[SIM_RESULT] PASSED\n"
                "ERROR: trace requested but no queryable .fst store or .vcd was produced\n"
            ),
            stderr="",
            duration_s=1.0,
        )
        with (
            patch.object(
                fusesoc_registry,
                "write_trace_overlay",
                return_value=fusesoc_registry.TraceOverlay(
                    core_file=tmp_path / "x.booleytrace.core",
                    vlnv="::d-booleytrace:0",
                ),
            ),
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=lambda *a, **k: self._fake_resolved(tmp_path),
            ),
            patch.object(
                SimulateFlow,
                "_execute",
                return_value=no_trace,
            ),
        ):
            result = flow._run_single_test("lite", None, {})
        assert result.passed is False
        assert result.inconclusive is True, "trace-infra failure must be inconclusive, not FAIL"
        assert result.sva_errors == 0, "trace ERROR line must not be counted as an SVA error"

    def test_trace_ok_marker_keeps_pass(self, tmp_path: Path):
        """A PASS with a real waveform (TRACE_OK) stays a PASS."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, extra_args=["--trace"])
        with_trace = SubprocessResult(
            returncode=0,
            stdout="[SIM_RESULT] PASSED\nTRACE_OK: /work/trace.vcd\n",
            stderr="",
            duration_s=1.0,
        )
        with (
            patch.object(
                fusesoc_registry,
                "write_trace_overlay",
                return_value=fusesoc_registry.TraceOverlay(
                    core_file=tmp_path / "x.booleytrace.core",
                    vlnv="::d-booleytrace:0",
                ),
            ),
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=lambda *a, **k: self._fake_resolved(tmp_path),
            ),
            patch.object(
                SimulateFlow,
                "_execute",
                return_value=with_trace,
            ),
        ):
            result = flow._run_single_test("lite", None, {})
        assert result.passed is True

    def test_setup_failure_propagates(self, tmp_path: Path):
        """A FuseSoC resolution failure surfaces (caller records it as FAIL)."""
        import pytest

        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=fusesoc_registry.TargetResolutionError("boom"),
            ),
            pytest.raises(fusesoc_registry.TargetResolutionError, match="boom"),
        ):
            flow._prepare_sim_command("lite", None, {})

    def test_real_fusesoc_sim_setup(self, tmp_path: Path):
        """End-to-end: a real `fusesoc run --setup` leaves a makeable build dir.

        Proves the --timing option set and the custom --exe main land in the
        resolved .vc, the dir is relocatable, and simulate's command builds then
        runs `V<top>` over it.
        """
        import pytest

        pytest.importorskip("fusesoc")
        pytest.importorskip("edalize")
        import shutil
        import sys

        from booley.fusesoc import fusesoc_registry

        work_dir = tmp_path / "proj"
        (work_dir / "rtl").mkdir(parents=True)
        (work_dir / "tb").mkdir(parents=True)
        (work_dir / "sim").mkdir(parents=True)
        (work_dir / "rtl" / "counter.sv").write_text(
            "module counter(input logic clk); endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "tb" / "tb_counter.sv").write_text(
            "module tb_counter; counter dut(.clk(1'b0)); initial $finish; endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "sim" / "booley_vcd_dump.sv").write_text(
            "module booley_vcd_dump;\n"
            '  initial if ($test$plusargs("trace")) $dumpvars(0);\n'
            "endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "sim" / "tb_counter__main.cpp").write_text(
            '#include "verilated.h"\n#include "Vtb_counter.h"\n'
            "double sc_time_stamp() { return 0; }\n"
            "int main(int argc, char** argv, char**) { return 0; }\n",
            encoding="utf-8",
        )
        (work_dir / "sim_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")

        flow = _make_flow(work_dir, config="sim", seed_core=False)

        if shutil.which("fusesoc"):
            fusesoc_cmd = list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
        else:
            fusesoc_cmd = [sys.executable, "-c", "from fusesoc.main import main; main()"]

        orig_resolve = fusesoc_registry.resolve_target
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: orig_resolve(
                *a,
                **{**k, "fusesoc_cmd": fusesoc_cmd},
            ),
        ):
            cmd = flow._prepare_sim_command(
                "sim",
                None,
                {},
            )

        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        # Build half is the edalize `make`; the run half is re-homed to the
        # booley.sim.verilator_run wrapper (commit 374c97b) which execs the
        # verilated ./V<top> binary — so assert on the wrapper + top, not the
        # bare Vtb_counter binary name the pre-refactor command referenced.
        assert "make" in script
        assert "booley.sim.verilator_run" in script and "tb_counter" in script
        make_dir = next((work_dir / ".booley_project" / ".runtime").rglob("Makefile")).parent
        vc = next(make_dir.glob("*.vc")).read_text(encoding="utf-8")
        assert "--timing" in vc and "--trace" in vc and "-Wno-fatal" in vc
        # Custom main wired via --exe; relocatable (no absolute project paths).
        assert "--exe" in vc
        assert "tb_counter__main.cpp" in vc
        assert str(work_dir) not in vc

    def test_real_fusesoc_icarus_sim_setup(self, tmp_path: Path):
        """End-to-end (Icarus): a real `fusesoc run --setup` leaves a makeable
        Icarus build dir, and simulate's command builds via `make` then runs the
        vvp image through booley.sim.iverilog_run — NOT `make run`, and NOT the
        Verilator run-half. Icarus trace is runtime (+trace) so no overlay/--exe.
        """
        import pytest

        pytest.importorskip("fusesoc")
        pytest.importorskip("edalize")
        import shutil
        import sys

        from booley.fusesoc import fusesoc_registry

        work_dir = tmp_path / "proj"
        (work_dir / "rtl").mkdir(parents=True)
        (work_dir / "tb").mkdir(parents=True)
        (work_dir / "sim").mkdir(parents=True)
        (work_dir / "rtl" / "counter.sv").write_text(
            "module counter(input logic clk); endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "tb" / "tb_counter.sv").write_text(
            "module tb_counter; counter dut(.clk(1'b0)); initial $finish; endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "sim" / "booley_vcd_dump.sv").write_text(
            "module booley_vcd_dump;\n"
            '  initial if ($test$plusargs("trace")) $dumpvars(0);\n'
            "endmodule\n",
            encoding="utf-8",
        )
        (work_dir / "sim_demo.core").write_text(_SIM_CORE_ICARUS_TEXT, encoding="utf-8")

        flow = _make_flow(work_dir, config="sim", extra_args=["--trace"], seed_core=False)

        if shutil.which("fusesoc"):
            fusesoc_cmd = list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
        else:
            fusesoc_cmd = [sys.executable, "-c", "from fusesoc.main import main; main()"]

        orig_resolve = fusesoc_registry.resolve_target
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: orig_resolve(
                *a,
                **{**k, "fusesoc_cmd": fusesoc_cmd},
            ),
        ):
            cmd = flow._prepare_sim_command("sim", None, {})

        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        # Build via edalize `make`; run via the iverilog run-half (not `make run`,
        # not verilator_run). --trace flows to the run-half; no overlay was made.
        assert "make" in script
        assert "booley.sim.iverilog_run" in script
        assert "booley.sim.verilator_run" not in script
        assert "make run" not in script
        assert "--trace" in script

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_raw_verilator_pass)
    def test_raw_run_verdict_recovered_by_postprocessor(
        self,
        _mock_prep,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        # No [SIM_SUMMARY] in the raw output — reemit_sim_summary derives PASS.
        assert result.exit_code == EXIT_SUCCESS
        assert flow.state.is_met("sim_pass_lite")


class TestErrorTailSource:
    """The error excerpt must surface the DUT's own (stdout) failure signal, not
    the build's stderr lint-warning storm — except for elaboration failures,
    whose only diagnostic lives on stderr with no sim stdout to fall back to."""

    @staticmethod
    def _read_tail(tmp_path: Path) -> str:
        report = json.loads((tmp_path / "reports" / "sim_lite.json").read_text(encoding="utf-8"))
        return report["tests"][0]["error_tail"]

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_sim_failure_tail_from_stdout_not_stderr_noise(
        self,
        _mock_edalize,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        """A real sim FAIL: the build's stderr warning storm must not bury the
        stdout mismatch line in the 50-line excerpt window."""
        # Mismatch on stdout (the DUT's verdict) + a warning storm on stderr
        # (the first test's (re)build). Combined = stdout + "\n" + stderr, so a
        # naive combined tail would be 50 trailing warnings with no signal.
        noise = "\n".join(
            f"/work/rtl/foo.sv:{i}: warning: @* is sensitive to all 16 words" for i in range(60)
        )

        def _execute(self_inner, cmd):
            return SubprocessResult(
                returncode=1,
                stdout="[SIM_RESULT] FAILED\nFAIL: coeff mismatch at index 7\n",
                stderr=noise,
                duration_s=5.0,
            )

        with patch.object(SimulateFlow, "_execute", _execute):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        tail = self._read_tail(tmp_path)
        assert "coeff mismatch at index 7" in tail
        assert "warning:" not in tail

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_elab_failure_tail_falls_back_to_stderr(
        self,
        _mock_edalize,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        """An elaboration failure produces no sim stdout — the excerpt must fall
        back to the combined tail so the stderr diagnostic is not lost."""

        def _execute(self_inner, cmd):
            return SubprocessResult(
                returncode=1,
                stdout="",
                stderr="ERROR: Verilator elaboration failed (rc=1)\n",
                duration_s=4.2,
            )

        with patch.object(SimulateFlow, "_execute", _execute):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        assert "Verilator elaboration failed" in self._read_tail(tmp_path)


class TestElabFailedDetection:
    # The Edalize sim backends route through _prepare_sim_command (FuseSoC
    # resolution + build/run); patch that seam to a dummy command so the
    # _execute mock's elab error reaches _ELAB_FAIL_RE — the detection under
    # test — rather than resolution failing first on the bare tmp_path project.
    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_elab_fail_verilator)
    def test_verilator_elab_fail_sets_flag(
        self,
        _mock_prep,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert result.detail.get("elab_failed") is True

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_elab_fail_iverilog)
    def test_iverilog_elab_fail_sets_flag(
        self,
        _mock_prep,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert result.detail.get("elab_failed") is True

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_fail)
    def test_sim_fail_no_elab_flag(
        self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path
    ):
        """Normal sim failure must NOT set elab_failed."""
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert "elab_failed" not in result.detail

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_pass_no_elab_flag(self, _mock_edalize, _mock_backend, _mock_tests, tmp_path: Path):
        """Passing sim must NOT set elab_failed."""
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert "elab_failed" not in result.detail


# ---------------------------------------------------------------------------
# The Session Runtime boundary path (wrapper-Makefile builds, ADR 0037)
# ---------------------------------------------------------------------------


class TestTruncationResilientReport:
    """The MCP layer tail-truncates Flow stdout (keeps the END), so the compact
    per-target headline block + RESULT verdict must come LAST in report_text,
    failure excerpts must be bounded, and the run.log pointer must be printed."""

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_headline_block_is_last(
        self,
        _mock_edalize,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        """A passing target's verdict + cycle count must appear AFTER the
        failing target's verbose detail, so tail-truncation preserves them."""
        call_count = 0

        def _alternating(self_inner, cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SubprocessResult(
                    returncode=0,
                    stdout="[SIM_RESULT] PASSED\n[SIM_CYCLES] 4242\n"
                    '[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n',
                    duration_s=1.0,
                )
            return SubprocessResult(
                returncode=1,
                stdout=("chatty failure detail\n" * 40) + "[SIM_RESULT] FAILED\n"
                '[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n',
                duration_s=2.0,
            )

        with patch.object(SimulateFlow, "_execute", _alternating):
            flow = _make_flow(tmp_path, config="lite,full")
            result = flow._run()

        lines = result.report_text.splitlines()
        # The RESULT verdict is the very last line of the report.
        assert lines[-1].startswith("RESULT: FAIL")
        # The headline block sits after ALL verbose per-test detail.
        marker = lines.index("--- summary ---")
        assert "--- end error output ---" not in lines[marker:]
        tail = "\n".join(lines[marker:])
        # Both targets' verdicts and the passing target's cycle count survive
        # in the tail-truncation-protected block.
        assert "[sim] lite (session-runtime): PASS (1/1 tests" in tail
        assert "4,242 cycles" in tail
        assert "[sim] full (session-runtime): FAIL (0/1 tests" in tail

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_error_excerpt_capped_with_marker(
        self,
        _mock_edalize,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        """A chatty failure's excerpt is capped (~30 lines) with an omission
        marker pointing at run.log, so it can't monopolize the 12KB window."""
        chatty = "\n".join(f"noise-{i:03d}" for i in range(80))

        def _chatty_fail(self_inner, cmd):
            return SubprocessResult(
                returncode=1,
                stdout=chatty + "\n[SIM_RESULT] FAILED\n"
                '[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n',
                duration_s=1.0,
            )

        with patch.object(SimulateFlow, "_execute", _chatty_fail):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()

        report = result.report_text
        # error_tail keeps the last 50 stdout lines; the report shows only the
        # last 30 of those, with an explicit marker for the 20 omitted ones.
        # _prepare_sim_command is mocked away, so no run.log was written by
        # THIS invocation — the marker must NOT vouch for one (a stale log
        # from an earlier build can say "TEST PASSED" under a failing run).
        assert "... (20 lines omitted)" in report
        assert "see run.log" not in report
        assert "noise-079" in report  # tail of the excerpt kept
        assert "noise-051" not in report  # older lines dropped from the report

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_excerpt_marker_cites_run_log_only_when_fresh(
        self,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ):
        """When THIS invocation's run wrote run.log (the sandbox run-half
        does it from its child process), the omission marker may cite it."""
        chatty = "\n".join(f"noise-{i:03d}" for i in range(80))
        build_root = tmp_path / "build" / "lite"
        build_root.mkdir(parents=True)

        def _prepare(self_inner, target, test_name, test_names_map):
            self_inner._record_run_log_dir(target, build_root)
            return ["sh", "-c", ":"]

        def _fail_and_write_log(self_inner, cmd):
            # Simulate the run-half's child-process write: run.log lands on
            # disk DURING the run, through the shared writer that preserves
            # the run header the prepare half stamped in.
            write_run_log(build_root, chatty)
            return SubprocessResult(
                returncode=1,
                stdout=chatty + "\n[SIM_RESULT] FAILED\n"
                '[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n',
                duration_s=1.0,
            )

        with (
            patch.object(SimulateFlow, "_prepare_sim_command", _prepare),
            patch.object(SimulateFlow, "_execute", _fail_and_write_log),
        ):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()

        assert "... (20 lines omitted, see run.log)" in result.report_text

    def test_excerpt_cap_scales_with_mcp_budget(self, monkeypatch):
        """A raised BOOLEY_MCP_MAX_STDOUT_BYTES budget widens the per-test
        excerpt proportionally (30 lines per 12KB of budget)."""
        from booley.flows.sim.flow import _append_test_output_line

        tr = SimTestResult(
            name="smoke",
            passed=False,
            error_tail="\n".join(f"noise-{i:03d}" for i in range(80)),
        )
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "24000")
        lines: list[str] = []
        _append_test_output_line(tr, lines)
        assert "  --- error output (last 60 lines) ---" in lines
        assert "  ... (20 lines omitted)" in lines
        # Unset env: byte-identical to the historical 30-line cap.
        monkeypatch.delenv("BOOLEY_MCP_MAX_STDOUT_BYTES")
        lines = []
        _append_test_output_line(tr, lines)
        assert "  --- error output (last 30 lines) ---" in lines

    def test_headline_prints_run_log_path_on_failure(self, tmp_path: Path, capsys):
        """The headline block prints the project-relative run.log path recorded
        at prepare time (the resolved edalize build dir) — for a FAILING
        target whose log this invocation actually wrote."""
        from booley.flows.sim.flow import TargetResult, TestResult

        flow = _make_flow(tmp_path, config="lite")
        build_root = (
            tmp_path
            / ".booley_project"
            / ".runtime"
            / "edalize"
            / "sim"
            / "lite"
            / "demo_0"
            / "lite"
        )
        flow._record_run_log_dir("lite", build_root)
        write_run_log(build_root, "[SIM_RESULT] FAILED\n")  # this run's output
        tr = TargetResult(
            target="lite",
            passed=False,
            elapsed_s=1.0,
            tests=[TestResult(name="smoke", passed=False, elapsed_s=1.0)],
        )

        report = flow._format_summary([tr], [], False)

        assert ("log: .booley_project/.runtime/edalize/sim/lite/demo_0/lite/run.log") in report
        # The log pointer belongs to the headline block, i.e. before RESULT only.
        assert report.splitlines()[-1] == "RESULT: FAIL (0/1 targets)"

    def test_headline_omits_run_log_on_pass(self, tmp_path: Path, capsys):
        """A passing target's summary stays clean: no ``log:`` pointer (the
        pointer exists to be acted on, and only failures get acted on)."""
        from booley.flows.sim.flow import TargetResult, TestResult

        flow = _make_flow(tmp_path, config="lite")
        flow._record_run_log_dir("lite", tmp_path / "build" / "lite")
        write_run_log(tmp_path / "build" / "lite", "ok\n")  # fresh, but passing
        tr = TargetResult(
            target="lite",
            passed=True,
            elapsed_s=1.0,
            tests=[TestResult(name="smoke", passed=True, cycles=7, elapsed_s=1.0)],
        )

        report = flow._format_summary([tr], [], True)

        assert "log:" not in report
        assert report.splitlines()[-1] == "RESULT: PASS (1/1 targets)"

    def test_headline_suppresses_stale_run_log_pointer(self, tmp_path: Path, capsys):
        """A run.log left over from an EARLIER build must not be cited: it can
        read "TEST PASSED" while this run reports a failure (benchmark
        finding — ≥6/57 failure cases chased a stale log)."""
        from booley.flows.sim.flow import TargetResult, TestResult

        flow = _make_flow(tmp_path, config="lite")
        build_root = tmp_path / "build" / "lite"
        build_root.mkdir(parents=True)
        (build_root / "run.log").write_text("TEST PASSED\n", encoding="utf-8")
        flow._record_run_log_dir("lite", build_root)  # claims it for this run
        tr = TargetResult(
            target="lite",
            passed=False,
            elapsed_s=1.0,
            tests=[TestResult(name="smoke", passed=False, elapsed_s=1.0)],
        )

        report = flow._format_summary([tr], [], False)

        assert "log:" not in report

    def test_claiming_the_log_erases_the_previous_runs_output(self, tmp_path: Path):
        """F-26: claiming a target's run.log truncates it to a header, so a
        tail during the run can never show the PREVIOUS run's verdict as if it
        were live progress — and the pointer stays suppressed until this run's
        output actually lands."""
        flow = _make_flow(tmp_path, config="lite")
        build_root = tmp_path / "build" / "lite"
        build_root.mkdir(parents=True)
        (build_root / "run.log").write_text("TEST PASSED\n", encoding="utf-8")

        flow._record_run_log_dir("lite", build_root)

        mid_run = (build_root / "run.log").read_text(encoding="utf-8")
        assert "TEST PASSED" not in mid_run
        assert mid_run.startswith("[BOOLEY RUN_LOG] ")
        assert "flow=sim target=lite" in mid_run
        assert flow._run_log_is_fresh("lite") is False
        assert flow._run_log_pointer("lite") is None

    def test_run_header_backs_run_log_pointer(self, tmp_path: Path):
        """Once this run's output lands (the run-halves write it from their
        own child process), the preserved run header vouches for the log —
        the single freshness notion behind every pointer."""
        flow = _make_flow(tmp_path, config="lite")
        build_root = tmp_path / "build" / "lite"
        build_root.mkdir(parents=True)
        flow._record_run_log_dir("lite", build_root)
        write_run_log(build_root, "[SIM_RESULT] FAILED\n")

        assert flow._run_log_is_fresh("lite") is True
        assert flow._run_log_pointer("lite") == "build/lite/run.log"

    def test_another_runs_log_is_never_vouched_for(self, tmp_path: Path):
        """A log whose header names a DIFFERENT run (a concurrent Flow, or a
        run whose header this one never wrote) is not citable."""
        from booley.sim.sim_result import begin_run_log

        flow = _make_flow(tmp_path, config="lite")
        build_root = tmp_path / "build" / "lite"
        build_root.mkdir(parents=True)
        flow._record_run_log_dir("lite", build_root)
        begin_run_log(build_root, flow="sim", target="lite", run="somebody-else")
        write_run_log(build_root, "[SIM_RESULT] PASSED\n")

        assert flow._run_log_is_fresh("lite") is False
        assert flow._run_log_pointer("lite") is None

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    def test_prepare_sim_command_records_run_log_dir(
        self,
        _mock_tests,
        tmp_path: Path,
    ):
        """_prepare_sim_command must record the RESOLVED build dir (where the
        run-half writes run.log), not a guessed path."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path, config="lite")
        build_root = (
            tmp_path
            / ".booley_project"
            / ".runtime"
            / "edalize"
            / "sim"
            / "lite"
            / "demo_0"
            / "lite"
        )
        resolved = fusesoc_registry.ResolvedTarget(
            name="lite",
            vlnv="::demo:0",
            toplevel="tb_lite",
            eda_tool="verilator",
            files=(),
            parameters={},
            build_root=build_root,
            edam_path=build_root / "demo_0.eda.yml",
        )
        with patch.object(fusesoc_registry, "resolve_target", return_value=resolved):
            flow._prepare_sim_command("lite", None, {})

        assert flow._run_log_dirs["lite"] == build_root
        # Recording the dir alone does NOT make the pointer citable — only a
        # log this invocation actually wrote may be pointed at (fresh guard).
        assert flow._run_log_pointer("lite") is None
        write_run_log(build_root, "[SIM_RESULT] FAILED\n")
        assert flow._run_log_pointer("lite") == (
            ".booley_project/.runtime/edalize/sim/lite/demo_0/lite/run.log"
        )


# ---------------------------------------------------------------------------
# Build-context observability (compile command + fileset in report / failure card)
# ---------------------------------------------------------------------------


class TestBuildContextReporting:
    """The generated build config was invisible in reports (benchmark finding:
    agents shelled out to recover the edalize compile line and the fileset).
    The per-target report JSON now carries ``compile_command`` + ``fileset``,
    and a failing target's headline card names both in ≤2 compact lines."""

    def test_report_carries_compile_command_and_fileset(self, tmp_path: Path):
        """The per-target report names the composed build command (the same
        script --dry-run previews) and the rtl/tb-split fileset."""
        from booley.flows.sim.flow import TargetResult

        (tmp_path / "sim_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")
        flow = _make_flow(tmp_path, config="sim", seed_core=False)
        flow._test_names_map = {}
        tr = TargetResult(
            target="sim",
            tb_top="tb_counter",
            passed=False,
            elapsed_s=1.0,
            tests=[SimTestResult(name="smoke", passed=False, elapsed_s=1.0)],
        )

        flow._write_target_report(tr)

        report = json.loads((tmp_path / "reports" / "sim_sim.json").read_text())
        # The composed sh -c script: fusesoc setup chained to the edalize make.
        assert "--setup" in report["compile_command"]
        assert "make -C" in report["compile_command"]
        # The .core's declared partition (pre-resolve read, decision 13).
        assert report["fileset"]["rtl"] == ["rtl/counter.sv"]
        assert "tb/tb_counter.sv" in report["fileset"]["tb"]

    def test_report_carries_artifact_pointers(self, tmp_path: Path):
        """The per-target report names run.log and the trace family.

        These paths used to exist only in the stdout headline, and only on
        failure, so the MCP layer's stdout truncation cut them off on exactly
        the long runs that needed them.
        """
        from booley.flows.sim.flow import TargetResult
        from booley.sim.sim_result import write_run_log

        (tmp_path / "sim_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")
        flow = _make_flow(tmp_path, config="sim", seed_core=False)
        flow._test_names_map = {}
        build_root = tmp_path / "build"
        build_root.mkdir()
        # Real run order: the prepare half claims the log (opening it fresh),
        # then the run-half's output lands in it.
        flow._record_run_log_dir("sim", build_root)
        write_run_log(build_root, "simulator output\n")
        (build_root / "result.json").write_text("{}", encoding="utf-8")
        (build_root / "trace.fst").write_text("fst", encoding="utf-8")

        flow._write_target_report(TargetResult(target="sim", passed=False, elapsed_s=1.0))

        artifacts = json.loads((tmp_path / "reports" / "sim_sim.json").read_text())["artifacts"]
        assert artifacts["log"] == "build/run.log"
        assert artifacts["result"] == "build/result.json"
        assert artifacts["trace"] == "build/trace.fst"
        assert artifacts["report"] == "reports/sim_sim.json"
        # Nothing wrote these, so they are absent rather than dead pointers.
        assert "results_xml" not in artifacts
        assert "trace_incident" not in artifacts

    def test_stale_run_drops_every_pointer_not_just_the_log(self, tmp_path: Path):
        """A build that dies before the run-half starts cites NOTHING.

        ``begin_run_log`` truncates run.log, but nothing clears result.json /
        trace.fst / trace_incident.txt in a reused build root. Gating only the
        ``log`` key would leave the PREVIOUS run's ``passed: true`` result.json
        advertised under this run's failed verdict — F-26, one file over.
        """
        from booley.flows.sim.flow import TargetResult

        (tmp_path / "sim_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")
        flow = _make_flow(tmp_path, config="sim", seed_core=False)
        flow._test_names_map = {}
        build_root = tmp_path / "build"
        build_root.mkdir()
        # Survivors of an earlier, successful run in the same build root.
        (build_root / "result.json").write_text('{"passed": true}', encoding="utf-8")
        (build_root / "trace.fst").write_text("fst", encoding="utf-8")
        (build_root / "trace_incident.txt").write_text("old incident", encoding="utf-8")
        # This run claims the log but never completes it.
        flow._record_run_log_dir("sim", build_root)

        flow._write_target_report(TargetResult(target="sim", passed=False, elapsed_s=1.0))

        artifacts = json.loads((tmp_path / "reports" / "sim_sim.json").read_text())["artifacts"]
        assert set(artifacts) == {"report"}, "only this run's own report may be cited"
        assert artifacts["report"] == "reports/sim_sim.json"

    def test_trace_pointer_follows_a_custom_dump_path(self, tmp_path: Path):
        """A project's own ``trace_files`` dump path (F-22) still gets cited —
        ``trace.fst`` is the conventional name, not the only one."""
        from booley.flows.sim.flow import TargetResult
        from booley.sim.sim_result import write_run_log

        (tmp_path / "sim_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")
        flow = _make_flow(tmp_path, config="sim", seed_core=False)
        flow._test_names_map = {}
        build_root = tmp_path / "build"
        build_root.mkdir()
        flow._record_run_log_dir("sim", build_root)
        write_run_log(build_root, "output\n")
        custom = build_root / "waves" / "dump.fst"
        custom.parent.mkdir()
        custom.write_text("fst", encoding="utf-8")

        flow._write_target_report(
            TargetResult(
                target="sim",
                passed=True,
                elapsed_s=1.0,
                tests=[
                    SimTestResult(
                        name="smoke",
                        passed=True,
                        trace_path="build/waves/dump.fst",
                    )
                ],
            )
        )

        artifacts = json.loads((tmp_path / "reports" / "sim_sim.json").read_text())["artifacts"]
        assert artifacts["trace"] == "build/waves/dump.fst"

    def test_report_omits_context_keys_when_uncomposable(self, tmp_path: Path):
        """Best-effort contract: an unauthored Target (no .core) yields a
        report WITHOUT the context keys — never a failed Flow."""
        from booley.flows.sim.flow import TargetResult

        flow = _make_flow(tmp_path, config="ghost", seed_core=False)
        flow._test_names_map = {}
        tr = TargetResult(target="ghost", passed=False, elapsed_s=0.1)

        flow._write_target_report(tr)

        report = json.loads((tmp_path / "reports" / "sim_ghost.json").read_text())
        assert "compile_command" not in report
        assert "fileset" not in report

    def test_failing_headline_names_build_and_fileset(self, tmp_path: Path, capsys):
        """A failing target's headline card carries the compact build/fileset
        lines; a passing target's card stays clean."""
        from booley.flows.sim.flow import TargetResult

        (tmp_path / "sim_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")
        flow = _make_flow(tmp_path, config="sim", seed_core=False)
        flow._test_names_map = {}
        failing = TargetResult(
            target="sim",
            passed=False,
            elapsed_s=1.0,
            tests=[SimTestResult(name="smoke", passed=False, elapsed_s=1.0)],
        )

        report = flow._format_summary([failing], [], False)

        assert "  build: " in report
        assert "make -C" in report
        # Counts match the .core partition: 1 rtl + the tb-tagged files.
        fileset = flow._fileset_for_report("sim")
        total = len(fileset["rtl"]) + len(fileset["tb"])
        assert f"  fileset: {total} files ({len(fileset['tb'])} tb)" in report

        passing = TargetResult(
            target="sim",
            passed=True,
            elapsed_s=1.0,
            tests=[SimTestResult(name="smoke", passed=True, elapsed_s=1.0)],
        )
        report = flow._format_summary([passing], [], True)
        assert "build: " not in report
        assert "fileset:" not in report


# ---------------------------------------------------------------------------
# ravenoc F-5 — per-Target simulator environment (tests.toml `env`)
# ---------------------------------------------------------------------------


def _fake_sim_resolved(
    tmp_path: Path,
    *,
    eda_tool: str = "verilator",
    cocotb: str | None = None,
    parameters=None,
):
    from booley.fusesoc.fusesoc_registry import ResolvedTarget

    build_root = tmp_path / ".booley_project" / ".runtime" / "edalize" / "sim" / "lite"
    build_root.mkdir(parents=True, exist_ok=True)
    return ResolvedTarget(
        name="lite",
        vlnv="::sim_demo:0",
        toplevel="tb_counter",
        eda_tool=eda_tool,
        files=(),
        parameters=parameters or {},
        build_root=build_root,
        edam_path=build_root / "sim_demo_0.eda.yml",
        cocotb_module=cocotb,
    )


class TestPerTargetSimEnv:
    """F-5: an env-parameterized TB must not force a tracked testbench edit.

    ravenoc's eight cocotb modules all read ``os.getenv("FLAVOR")``. Neither
    pre_run_commands (separate shell) nor passthrough_env (host->container)
    reaches the simulator process, so the port had to rewrite a testbench it
    did not own. tests.toml `env` is the per-Target home for it.
    """

    def test_build_run_script_exports_env_before_both_halves(self):
        script = _build_run_script(
            ["make", "-C", "build"],
            "Verilator elaboration failed",
            "./Vtb",
            {"FLAVOR": "vanilla"},
        )
        assert script.startswith("export FLAVOR=vanilla\n")
        # One shell: the export must reach the build AND the run.
        assert script.index("export FLAVOR") < script.index("make")
        assert script.index("export FLAVOR") < script.index("./Vtb")

    def test_build_run_script_quotes_values(self):
        script = _build_run_script(["make"], "m", "./Vtb", {"OPTS": "a b; rm -rf /"})
        assert "export OPTS='a b; rm -rf /'" in script

    def test_build_run_script_without_env_is_unchanged(self):
        assert _build_run_script(["make"], "m", "./Vtb").startswith("_booley_build_start=")

    def test_sandbox_command_carries_the_targets_env(self, tmp_path: Path):
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        with (
            patch(
                "booley.flows.sim.flow._get_test_envs",
                return_value={"lite": {"FLAVOR": "vanilla"}},
            ),
            patch.object(
                fusesoc_registry,
                "resolve_target",
                return_value=_fake_sim_resolved(tmp_path),
            ),
        ):
            cmd = flow._prepare_sim_command("lite", None, {})
        assert "export FLAVOR=vanilla" in cmd[2]

    def test_cocotb_command_carries_the_targets_env(self, tmp_path: Path):
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        with (
            patch(
                "booley.flows.sim.flow._get_test_envs",
                return_value={"lite": {"FLAVOR": "small"}},
            ),
            patch.object(
                fusesoc_registry,
                "resolve_target",
                return_value=_fake_sim_resolved(tmp_path, eda_tool="icarus", cocotb="test_noc"),
            ),
            patch("booley.flows.sim.flow.validate_top_parameter_intent") as guard,
        ):
            cmd = flow._prepare_cocotb_sim_command("lite", ["run_test_001"])
        guard.assert_called_once()
        script = cmd[2]
        assert "export FLAVOR=small" in script
        assert script.index("export FLAVOR") < script.index("booley.sim.cocotb_run")

    def test_cocotb_command_carries_target_plusargs(self, tmp_path: Path):
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        resolved = _fake_sim_resolved(
            tmp_path,
            eda_tool="icarus",
            cocotb="test_noc",
            parameters={"SEED": {"datatype": "int", "paramtype": "plusarg", "default": 17}},
        )
        with patch.object(fusesoc_registry, "resolve_target", return_value=resolved):
            cmd = flow._prepare_cocotb_sim_command("lite", ["run_test_001"])
        assert "--plusarg=SEED=17" in cmd[2]

    def test_env_is_matched_through_a_vlnv_qualified_section(self, tmp_path: Path):
        """A tests.toml section may be VLNV-qualified; lookup must normalize."""
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        with (
            patch(
                "booley.flows.sim.flow._get_test_envs",
                return_value={"::sim_demo:0#lite": {"FLAVOR": "big"}},
            ),
            patch.object(
                fusesoc_registry,
                "resolve_target",
                return_value=_fake_sim_resolved(tmp_path),
            ),
        ):
            cmd = flow._prepare_sim_command("lite", None, {})
        assert "export FLAVOR=big" in cmd[2]

    def test_dry_run_preview_mirrors_the_live_exports(self, tmp_path: Path):
        flow = _make_flow(tmp_path)
        with patch(
            "booley.flows.sim.flow._get_test_envs",
            return_value={"lite": {"FLAVOR": "vanilla"}},
        ):
            cmd = flow._dry_run_command("lite", None, {})
        assert cmd[:2] == ["sh", "-c"]
        assert cmd[2].startswith("export FLAVOR=vanilla && ")

    def test_no_env_declared_leaves_the_script_alone(self, tmp_path: Path):
        from booley.fusesoc import fusesoc_registry

        flow = _make_flow(tmp_path)
        with (
            patch("booley.flows.sim.flow._get_test_envs", return_value={}),
            patch.object(
                fusesoc_registry,
                "resolve_target",
                return_value=_fake_sim_resolved(tmp_path),
            ),
        ):
            cmd = flow._prepare_sim_command("lite", None, {})
        assert "export " not in cmd[2]


# ---------------------------------------------------------------------------
# ravenoc F-32 — a missing EDA binary is a Flow error, never a test failure
# ---------------------------------------------------------------------------


def _missing_binary_execute(stdout: str, returncode: int = 2):
    def _exec(self, cmd):
        return SubprocessResult(
            returncode=returncode,
            stdout=stdout,
            stderr="",
            duration_s=0.05,
        )

    return _exec


class TestMissingExecutableDetection:
    def test_dash_not_found(self):
        from booley.flows.sim.flow import find_missing_executable

        assert find_missing_executable("/bin/sh: 1: verilator: not found") == "verilator"

    def test_bash_command_not_found(self):
        from booley.flows.sim.flow import find_missing_executable

        assert find_missing_executable("bash: fusesoc: command not found") == "fusesoc"

    def test_make_no_such_file(self):
        from booley.flows.sim.flow import find_missing_executable

        text = "make[1]: verilator: No such file or directory"
        assert find_missing_executable(text) == "verilator"

    def test_fusesoc_spawn_guard(self):
        from booley.flows.sim.flow import find_missing_executable

        text = "could not invoke fusesoc (fusesoc): [Errno 2] No such file or directory: 'fusesoc'"
        assert find_missing_executable(text) == "fusesoc"

    def test_bare_spawn_failure_names_the_eda_tool(self):
        from booley.flows.sim.flow import find_missing_executable

        text = "FileNotFoundError: [Errno 2] No such file or directory: 'xrun'"
        assert find_missing_executable(text) == "xrun"

    def test_missing_data_file_is_not_a_missing_binary(self):
        from booley.flows.sim.flow import find_missing_executable

        # A plain setup failure keeps its own message and exit-1 grading.
        text = "FileNotFoundError: [Errno 2] No such file or directory: '/work/foo.core'"
        assert find_missing_executable(text) is None

    def test_ordinary_compile_error_is_not_a_missing_binary(self):
        from booley.flows.sim.flow import find_missing_executable

        text = "%Error: tb.sv:12: Cannot find file containing module: 'dut'\nnot found\n"
        assert find_missing_executable(text) is None


class TestMissingExecutableIsEdaToolError:
    """F-32: the verdict channel must not say FAIL when nothing ever ran."""

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["t1"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_missing_fusesoc_at_setup_exits_2(self, _sel, _tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        boom = RuntimeError(
            "could not invoke fusesoc (fusesoc): [Errno 2] No such file or directory: 'fusesoc'"
        )
        with patch.object(SimulateFlow, "_prepare_sim_command", side_effect=boom):
            result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert result.detail["eda_tool_error"] == "missing_executable"
        assert result.detail["missing_executable"] == "fusesoc"
        # No invented per-test verdict rows.
        assert "FAIL" not in result.report_text
        assert "targets_passed" not in result.detail
        assert "'fusesoc'" in result.report_text

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["t1"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_missing_verilator_in_build_exits_2(self, _prep, _sel, _tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        # What the sandbox script actually prints with verilator off PATH: the
        # canonical elab marker (accurate rc, wrong stage) over sh's own gripe.
        stdout = (
            "/bin/sh: 1: verilator: not found\n"
            "make: *** [Makefile:9: Vtop] Error 127\n"
            "ERROR: Verilator elaboration failed (rc=2)\n"
        )
        with patch.object(SimulateFlow, "_execute", _missing_binary_execute(stdout)):
            result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert result.detail["missing_executable"] == "verilator"
        assert "elaboration failed" not in result.report_text.split("--- output tail ---")[0]

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["t1"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_missing_pre_run_executable_exits_2_without_criterion(
        self, _prep, _sel, _tests, tmp_path: Path
    ):
        flow = _make_flow(tmp_path, config="lite")
        missing = subprocess.CompletedProcess(
            [],
            127,
            stdout="",
            stderr="/bin/bash: line 2: riscv64-unknown-elf-gcc: command not found\n",
        )
        with (
            patch(
                "booley.flows.sim.flow._resolve_pre_run_commands",
                return_value=["riscv64-unknown-elf-gcc"],
            ),
            patch("booley.flows.sim.flow.subprocess.run", return_value=missing),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert result.detail["missing_executable"] == "riscv64-unknown-elf-gcc"
        assert result.criterion_key == ""
        assert "FAIL" not in result.report_text.split("--- output tail ---")[0]

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_elab_fail_verilator)
    def test_real_elaboration_failure_is_still_exit_1(self, _prep, _sel, _tests, tmp_path: Path):
        """A compiler that ran and rejected the design keeps its design verdict."""
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert result.detail.get("elab_failed") is True

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_live_sim_echoing_command_not_found_is_not_hijacked(
        self, _prep, _sel, _tests, tmp_path: Path
    ):
        """A running TB's own $system noise must not become a Flow error."""
        flow = _make_flow(tmp_path, config="lite")
        stdout = (
            "sh: 1: helper_script: not found\n"
            '[SIM_RESULT] FAILED\n[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n'
        )
        with patch.object(SimulateFlow, "_execute", _missing_binary_execute(stdout, 1)):
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE


# ---------------------------------------------------------------------------
# ravenoc F-35 / F-39 — report the trace artifact; keep sub-second durations
# ---------------------------------------------------------------------------


class TestTraceArtifactReported:
    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    def test_trace_path_and_size_reach_the_report(self, _prep, _sel, _tests, tmp_path: Path):
        store = tmp_path / ".booley_project" / ".runtime" / "edalize" / "sim" / "lite-trace"
        store.mkdir(parents=True)
        fst = store / "dump.fst"
        fst.write_bytes(b"x" * 4096)

        flow = _make_flow(tmp_path, config="lite", extra_args=["--trace"])
        stdout = (
            f"TRACE_OK: {fst}\n"
            '[SIM_RESULT] PASSED\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        )
        with patch.object(SimulateFlow, "_execute", _missing_binary_execute(stdout, 0)):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        rel = ".booley_project/.runtime/edalize/sim/lite-trace/dump.fst"
        assert f"trace: {rel} (4.0 KB)" in result.report_text
        report = json.loads((tmp_path / "reports" / "sim_lite.json").read_text())
        assert report["tests"][0]["trace_path"] == rel
        assert report["tests"][0]["trace_bytes"] == 4096

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_prepare_sim_command", return_value=["sh", "-c", ":"])
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_untraced_run_reports_no_trace_line(self, _prep, _sel, _tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert "trace:" not in result.report_text


class TestSubSecondDurations:
    """F-39: 10-20ms cocotb tests all rendered as an identical `0.0s`."""

    def test_format_duration_switches_to_milliseconds(self):
        from booley.flows.sim.flow import _format_duration

        assert _format_duration(0.012) == "12ms"
        assert _format_duration(0.0) == "0ms"
        assert _format_duration(0.999) == "999ms"
        assert _format_duration(1.0) == "1.0s"
        assert _format_duration(93.4) == "93.4s"

    def test_status_line_distinguishes_two_fast_tests(self):
        fast = _test_status_line(SimTestResult(name="a", passed=True, elapsed_s=0.011))
        slower = _test_status_line(SimTestResult(name="b", passed=True, elapsed_s=0.019))
        assert "11ms" in fast
        assert "19ms" in slower
        assert fast != slower

    def test_report_entry_keeps_millisecond_resolution(self):
        from booley.flows.sim.flow import _test_report_entry

        entry = _test_report_entry(SimTestResult(name="a", passed=True, elapsed_s=0.012))
        assert entry["elapsed_s"] == 0.012


# ---------------------------------------------------------------------------
# Selector round trip: producer argv -> run-half argparse (F-12 family)
# ---------------------------------------------------------------------------


class TestSelectorRoundTrip:
    """Drive the real producer methods and parse with the real run-half parsers.

    The F-12 bug family: test selectors and verdict sentinels were repeatedly
    lost or mangled at the subprocess boundary because every hop re-encoded
    them by hand (two-token vs ``=`` form, ``+`` stripped here and re-added
    there). Nothing asserted the whole chain end to end, so each producer
    broke — and was fixed — one incident at a time. These tests close the
    loop: any encoding drift on either side of the boundary fails here.
    """

    @staticmethod
    def _runner_argv(cmd: list[str], module: str) -> list[str]:
        """The argv the run-half's argparse actually sees."""
        return cmd[cmd.index(module) + 1 :]

    _SELECTORS: ClassVar[list[str]] = [
        "test_id=3",  # bare (legacy contract; run-half re-adds the '+')
        "+test_id=3",  # already plusarg-shaped
        "--meminit=ram,firmware.elf",  # getopt-style (the original F-12 case)
        "-t",  # short option, worst case for two-token parsing
    ]

    def test_verilator_selector_survives_to_binary_argv(self, tmp_path: Path):
        from booley.sim import verilator_run

        for selector in self._SELECTORS:
            cmd = _make_flow(tmp_path)._verilator_run_cmd("build/dir", "tb", [selector])
            ns = verilator_run._parse_args(self._runner_argv(cmd, "booley.sim.verilator_run"))
            assert ns.plusargs == [selector]
            # And onward to the binary: '+' restored for bare selectors,
            # option-like ones forwarded verbatim (SETUP-7).
            run_cmd, _env = verilator_run._build_run_cmd(Path("/x/Vtb"), Path("/x"), ns.plusargs)
            expected = selector if selector.startswith(("+", "-")) else f"+{selector}"
            assert run_cmd[1:] == [expected]

    def test_icarus_selector_survives_parse(self, tmp_path: Path):
        from booley.sim import iverilog_run

        for selector in self._SELECTORS:
            cmd = _make_flow(tmp_path)._icarus_run_cmd("build/dir", [selector])
            ns = iverilog_run._parse_args(self._runner_argv(cmd, "booley.sim.iverilog_run"))
            assert ns.plusargs == [selector]

    def test_cocotb_test_names_survive_parse(self, tmp_path: Path):
        from booley.sim import cocotb_run

        tests = ["test_reset", "test_count"]
        cmd = _make_flow(tmp_path)._cocotb_run_cmd("build/dir", "icarus", "tb.test_mod", tests)
        ns = cocotb_run._parse_args(self._runner_argv(cmd, "booley.sim.cocotb_run"))
        assert ns.tests == tests
        assert ns.cocotb_module == "tb.test_mod"

    def test_option_like_sentinels_survive_parse(self, tmp_path: Path):
        """A sentinel starting with '-' needs the `=` form to survive argparse."""
        from booley.sim import verilator_run

        with patch(
            "booley.flows.sim.flow._resolve_sim_sentinels",
            return_value=(["ALL TESTS PASSED."], ["-FAILED-", "ERROR!"]),
        ):
            cmd = _make_flow(tmp_path)._verilator_run_cmd("build/dir", "tb", [])
        ns = verilator_run._parse_args(self._runner_argv(cmd, "booley.sim.verilator_run"))
        assert ns.pass_sentinels == ["ALL TESTS PASSED."]
        assert ns.fail_sentinels == ["-FAILED-", "ERROR!"]


class TestInconclusiveReason:
    """fpu F-22a: the RESULT line must name the reason it actually hit."""

    def test_missing_waveform_does_not_claim_a_missing_sentinel(self, tmp_path: Path, capsys):
        from booley.flows.sim.flow import TargetResult, TestResult

        flow = _make_flow(tmp_path, config="lite")
        tr = TargetResult(
            target="lite",
            passed=False,
            inconclusive=True,
            elapsed_s=1.0,
            tests=[
                TestResult(
                    name="smoke",
                    passed=False,
                    inconclusive=True,
                    inconclusive_reason=_INCONCLUSIVE_NO_WAVEFORM,
                )
            ],
        )
        report = flow._format_summary([tr], [], False)
        assert "RESULT: INCONCLUSIVE" in report
        # The whole finding: a run that scored 96 sentinels was told it had none.
        assert "no pass/fail sentinel detected" not in report
        assert "trace_files" in report

    def test_missing_sentinel_keeps_its_own_wording(self, tmp_path: Path, capsys):
        from booley.flows.sim.flow import TargetResult, TestResult

        flow = _make_flow(tmp_path, config="lite")
        tr = TargetResult(
            target="lite",
            passed=False,
            inconclusive=True,
            elapsed_s=1.0,
            tests=[
                TestResult(
                    name="smoke",
                    passed=False,
                    inconclusive=True,
                    inconclusive_reason=_INCONCLUSIVE_NO_SENTINEL,
                )
            ],
        )
        report = flow._format_summary([tr], [], False)
        assert "no pass/fail sentinel detected" in report

    def test_interpret_marks_a_traceless_pass_with_the_trace_reason(self, tmp_path: Path):
        """The producing side: a passing sim + no TRACE_OK -> the trace reason."""
        flow = _make_flow(tmp_path, config="lite", extra_args=["--trace"])
        combined = '[SIM_RESULT] PASSED\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        proc = SubprocessResult(returncode=0, stdout=combined, stderr="")
        tr = flow._interpret_sim_result(combined, proc, "lite", "smoke")
        assert tr.inconclusive is True
        assert tr.inconclusive_reason == _INCONCLUSIVE_NO_WAVEFORM


class TestTraceFilesKnob:
    """fpu F-22b: declare where a custom main() drops its dump."""

    def test_resolve_trace_files_reads_booley_toml(self, tmp_path: Path):
        from booley.flows.sim.flow import _resolve_trace_files

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            '[flows.sim]\ntrace_files = ["fpu.vcd", "dump_*.fst"]\n',
            encoding="utf-8",
        )
        assert _resolve_trace_files(tmp_path) == ["fpu.vcd", "dump_*.fst"]
        assert _resolve_trace_files(tmp_path / "nowhere") == []

    def test_forwarded_to_both_run_halves_only_when_tracing(self, tmp_path: Path):
        with patch(
            "booley.flows.sim.flow._resolve_trace_files",
            return_value=["fpu.vcd"],
        ):
            traced = _make_flow(tmp_path, config="lite", extra_args=["--trace"])
            plain = _make_flow(tmp_path, config="lite")
            assert "--trace-file=fpu.vcd" in traced._verilator_run_cmd("d", "tb", [])
            assert "--trace-file=fpu.vcd" in traced._icarus_run_cmd("d", [])
            assert not [a for a in plain._verilator_run_cmd("d", "tb", []) if "trace-file" in a]


class TestErrorExcerptSelection:
    """fpu F-28: the excerpt must be relevant, and honestly labelled."""

    def test_selection_anchors_on_the_last_error_with_context(self):
        from booley.flows.sim.flow import select_error_lines

        log = "\n".join(
            ["noise"] * 200
            + ["f32_le", "TEST FAILED", "REFERENCE=1 CALCULATED=0", "cleanup"]
            + ["trailing"] * 100
        )
        picked = select_error_lines(log, 50)
        # The diagnosis lives AROUND the marker line, not at the end of the log.
        assert "TEST FAILED" in picked
        assert "REFERENCE=1 CALCULATED=0" in picked
        assert "f32_le" in picked
        # 100 lines of trailing chatter no longer displace it: the marker window
        # keeps the bulk of the budget, the tail keeps only its reserved slice.
        assert picked.count("trailing") <= 15
        assert len(picked) <= 50

    def test_selection_never_drops_the_end_of_the_log(self):
        """A marker at t=1 must not amputate the verdict 5,000 lines later.

        The excerpt anchors on the LAST marker, and the markers are coarse and
        case-sensitive — a benign "ERROR: unable to open coverage db" early on
        plus a lowercase failure summary at the end is enough to render six
        lines about the coverage db and lose the actual result, which is worse
        than the blind tail this replaced.
        """
        from booley.flows.sim.flow import select_error_lines

        log = "\n".join(
            ["ERROR: unable to open coverage db"]
            + [f"noise {i}" for i in range(5000)]
            + ["Result: 12 tests failed"]
        )
        picked = select_error_lines(log, 50)
        assert picked[-1] == "Result: 12 tests failed"
        assert picked[0] == "ERROR: unable to open coverage db"  # the anchor, too
        assert any("lines omitted" in ln for ln in picked)  # the gap is announced
        assert len(picked) <= 50

    def test_selection_keeps_the_tail_when_the_budget_is_tiny(self):
        """Too small to hold both halves: the end of the log is what survives."""
        from booley.flows.sim.flow import select_error_lines

        log = "\n".join(["ERROR: early"] + [f"n{i}" for i in range(100)] + ["final verdict"])
        picked = select_error_lines(log, 5)
        assert picked[-1] == "final verdict"
        assert len(picked) == 5

    def test_selection_has_no_gap_marker_when_the_halves_touch(self):
        """No elision line when the marker window runs into the tail."""
        from booley.flows.sim.flow import select_error_lines

        log = "\n".join(["TEST FAILED"] + [f"n{i}" for i in range(12)])
        picked = select_error_lines(log, 50)
        assert picked == log.splitlines()

    def test_selection_falls_back_to_the_noise_filtered_tail(self):
        from booley.flows.sim.flow import select_error_lines

        log = "\n".join(["VCD info: dumpfile opened"] + [f"TEST SUCCEEDED {i}" for i in range(40)])
        picked = select_error_lines(log, 10)
        assert picked == [f"TEST SUCCEEDED {i}" for i in range(30, 40)]

    def test_selection_on_empty_input(self):
        from booley.flows.sim.flow import select_error_lines

        assert select_error_lines("", 50) == []
        assert select_error_lines("   \n  \n", 50) == []

    def test_inconclusive_excerpt_is_not_called_error_output(self):
        from booley.flows.sim.flow import TestResult, _append_test_output_line

        tr = TestResult(
            name="sim_float",
            passed=False,
            inconclusive=True,
            error_tail="\n".join(["TEST SUCCEEDED"] * 30),
        )
        lines: list[str] = []
        _append_test_output_line(tr, lines)
        body = "\n".join(lines)
        # The whole finding: 30 lines of the TB succeeding, presented as errors.
        assert "error output" not in body
        assert "no pass/fail sentinel found" in body
        assert "end output tail" in body

    def test_real_failure_keeps_the_error_output_label(self):
        from booley.flows.sim.flow import TestResult, _append_test_output_line

        tr = TestResult(name="sim_float", passed=False, error_tail="TEST FAILED\n")
        lines: list[str] = []
        _append_test_output_line(tr, lines)
        assert "--- error output (last 1 lines) ---" in "\n".join(lines)


class TestFullRunLogOnPass:
    """fpu F-29e: a PASSING sandbox run's log carries the build section too."""

    def test_persist_writes_build_and_run_halves(self, tmp_path: Path):
        from booley.sim.sim_result import run_log_is_current

        flow = _make_flow(tmp_path, config="lite")
        log_dir = tmp_path / "build" / "lite"
        log_dir.mkdir(parents=True)
        # The real order: the prepare half claims the log, then the run-half
        # persists ITS half only — no build section anywhere.
        flow._record_run_log_dir("lite", log_dir)
        write_run_log(log_dir, "[Verilator simulation]\n[SIM_RESULT] PASSED\n")

        proc = SubprocessResult(
            returncode=0,
            stdout="make[1]: Entering build\nverilator --cc ...\nBOOLEY_BUILD_SECONDS: 12\n"
            "[Verilator simulation]\n[SIM_RESULT] PASSED\n",
            stderr="",
        )
        flow._persist_full_run_log("lite", proc)

        content = (log_dir / "run.log").read_text(encoding="utf-8")
        assert "verilator --cc" in content  # the build half, previously absent
        assert "[SIM_RESULT] PASSED" in content
        # The freshness guard behind every "see run.log" pointer still answers.
        assert run_log_is_current(log_dir) is True

    def test_persist_is_a_noop_without_a_known_log_dir(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        proc = SubprocessResult(returncode=0, stdout="x", stderr="")
        flow._persist_full_run_log("never-resolved", proc)  # must not raise
