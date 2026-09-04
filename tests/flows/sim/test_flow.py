"""Tests for SimulateFlow — multi-config sim, cycle parsing, dry-run, reports."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.run_log import write_run_log
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    write_adapter_result,
)
from booley.flows.sim.backends import cocotb_results
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.execution import SimulationExecution, SimulationOptions
from booley.flows.sim.execution.telemetry import (
    parse_build_seconds,
    parse_run_seconds,
    parse_sim_cpu_seconds,
)
from booley.flows.sim.flow import (
    _INCONCLUSIVE_NO_SENTINEL,
    _INCONCLUSIVE_NO_WAVEFORM,
    TargetResult,
    _append_batch_output_lines,
    _append_error_excerpt,
    _artifact_path_component,
    _build_display_lines,
    _build_run_script,
    _filter_tests,
    _resolve_sim_campaign_work_units,
    _test_status_line,
    parse_cycles,
    parse_sva_errors,
)
from booley.flows.sim.flow import (
    SimulateFlow as ProductionSimulateFlow,
)
from booley.flows.sim.flow import (
    TestResult as SimTestResult,  # aliased: a Test* name would be pytest-collected
)
from booley.flows.sim.result import parse_sim_verdict, parse_summary_line
from booley.flows.sim.trace_recipe import TraceMode
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.targets.target import inspect_target

# Built-in Flow execution inside the Session Runtime.
_FLOW_ENABLED = True


class SimulateFlow(ProductionSimulateFlow):
    """Concrete Flow used by the campaign compatibility tests."""


def test_target_metadata_resolution_is_counted_as_setup(tmp_path: Path) -> None:
    flow = _make_flow(tmp_path, config="lite")
    target_result = TargetResult(
        target="lite",
        elapsed_s=1.0,
        phase_timings_s={
            "setup": 0.1,
            "unattributed": 0.2,
            "execution_total": 1.0,
        },
    )
    with (
        patch.object(flow, "_tb_top_for_target", return_value="alu_tb"),
        patch.object(flow, "_run_target", return_value=target_result),
        patch("booley.flows.sim.flow.time.monotonic", side_effect=[10.0, 10.25]),
    ):
        result = flow._run_resolved_target("lite", {}, [])

    assert result.elapsed_s == 1.25
    assert result.phase_timings_s == {
        "setup": 0.35,
        "unattributed": 0.2,
        "execution_total": 1.25,
    }


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
    flow._simulation_execution_override = _BoundaryHarness(flow)
    return flow


class _BoundaryHarness(SimulationExecution):
    """Real execution engine with only its external build/process edges faked."""

    def __init__(self, flow: SimulateFlow) -> None:
        self._flow = flow
        self._attempt = None
        super().__init__(
            invoke=self._invoke_test_process,
            artifact_root=flow.args.report_dir,
            options=SimulationOptions(
                trace=flow.args.trace,
                timeout_ms=int(flow.args.timeout) if flow.args.timeout else None,
                result_verbosity=flow.args.result_verbosity,
            ),
        )

    def _prepare_build(self, handle):
        inspection = inspect_target(handle.project_root, handle)
        eda_tool = (
            getattr(self._flow, "_boundary_eda_tool", None) or inspection.eda_tool or "verilator"
        )
        if eda_tool not in {"icarus", "verilator"}:
            from booley.flows.sim.build import SimulationBuildPreparationError

            raise SimulationBuildPreparationError(
                f"simulator {eda_tool!r} is not supported by the public sim Flow; "
                "select a Verilator or Icarus Target"
            )
        build_root = handle.project_root / "build" / handle.selector
        build_root.mkdir(parents=True, exist_ok=True)
        resolved = ResolvedTarget(
            name=handle.selector,
            vlnv=handle.vlnv,
            toplevel=inspection.toplevel,
            eda_tool=eda_tool,
            files=(),
            parameters=dict(inspection.parameters),
            build_root=build_root,
            edam_path=build_root / "test.eda.yml",
            flow_options=dict(inspection.flow_options),
            cocotb_module=inspection.flow_options.get("cocotb_module"),
        )
        prepared = PreparedSimulationBuild(
            handle.selector,
            handle.identity,
            resolved,
            build_root,
            build_root,
            eda_tool,
            inspection.toplevel,
            ("make", "-C", str(build_root)),
        )
        return prepared, TraceMode.VCD_FIFO

    def _prepare_attempt(self, handle, test_names):
        attempt = super()._prepare_attempt(handle, test_names)
        self._attempt = attempt
        return attempt

    def _invoke_test_process(self, command, *, timeout):
        del timeout
        process = self._flow._execute(command)
        assert self._attempt is not None
        combined = process.stdout + ("\n" + process.stderr if process.stderr else "")
        lowered = combined.lower()
        build_failed = (
            "compilation failed" in lowered
            or "elaboration failed" in lowered
            or "%error" in lowered
        )
        marker = (
            f"BOOLEY_BUILD_STAGE token={self._attempt.identity.attempt_token} "
            f"rc={1 if build_failed else 0}\n"
        )
        stdout = f"{process.stdout}\n{marker}" if build_failed else f"{marker}{process.stdout}"
        if not build_failed:
            write_adapter_result(
                self._attempt.identity,
                _adapter_result_for_test_process(self._attempt.identity.selected_tests, process),
            )
            fresh_ns = time.time_ns()
            os.utime(self._attempt.identity.result_path, ns=(fresh_ns, fresh_ns))
        return SubprocessResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=process.stderr,
            timed_out=process.timed_out,
            duration_s=process.duration_s,
            peak_rss_mb=process.peak_rss_mb,
            oom_kill_delta=process.oom_kill_delta,
        )


def _adapter_result_for_test_process(
    names: tuple[str, ...],
    process: SubprocessResult,
) -> AdapterResult:
    output = process.stdout + ("\n" + process.stderr if process.stderr else "")
    try:
        summary = parse_summary_line(output)
    except ValueError:
        summary = None
    parsed = cocotb_results.parse_results_line(output)
    if parsed is not None:
        return _cocotb_adapter_result(names, process, output, summary, parsed)
    return _native_adapter_result(names, process, output, summary)


def _cocotb_adapter_result(names, process, output, summary, parsed) -> AdapterResult:
    if "cocotb simulation timed out" in output.lower() or process.timed_out:
        parsed = cocotb_results.recover_timeout_progress(output, list(names), parsed)
    elapsed = {test.name: test.elapsed_s for test in parsed.tests}
    tests = tuple(
        AdapterTestResult(
            name,
            "timeout" if detail == cocotb_results.TIMEOUT_ACTIVE_DETAIL else verdict,
            elapsed.get(name, process.duration_s),
            detail,
        )
        for name, verdict, detail in cocotb_results.reconcile(list(names), parsed)
    )
    extras = tuple(test.name for test in parsed.tests if test.name not in names)
    diagnostics = (
        (
            "results.xml reports extra non-selected test(s): "
            f"{', '.join(extras)} — logged, not verdict-bearing",
        )
        if extras
        else ()
    )
    sva_errors = int(summary.get("sva_errors", 0)) if summary else 0
    inconclusive = any(test.verdict == "inconclusive" for test in tests)
    passed = bool(
        process.returncode == 0
        and summary is not None
        and summary["passed"]
        and sva_errors == 0
        and tests
        and all(test.verdict == "pass" for test in tests)
    )
    return _test_adapter_result(names, tests, passed, inconclusive, sva_errors, diagnostics)


def _native_adapter_result(names, process, output, summary) -> AdapterResult:
    sentinel = parse_sim_verdict(output)
    passed = bool(
        process.returncode == 0
        and (summary["passed"] if summary is not None else sentinel is True)
    )
    inconclusive = summary is None and sentinel is None and not process.timed_out
    verdict = (
        "timeout"
        if process.timed_out
        else "pass"
        if passed
        else "inconclusive"
        if inconclusive
        else "fail"
    )
    tests = tuple(AdapterTestResult(name, verdict, process.duration_s) for name in names)
    sva_errors = int(summary.get("sva_errors", 0)) if summary else 0
    return _test_adapter_result(names, tests, passed, inconclusive, sva_errors, ())


def _test_adapter_result(names, tests, passed, inconclusive, sva_errors, diagnostics):
    timed_out = any(test.verdict == "timeout" for test in tests)
    return AdapterResult(
        passed=passed,
        inconclusive=inconclusive,
        sva_errors=sva_errors,
        tests=names,
        failure_kind=(
            "timeout"
            if timed_out
            else "inconclusive"
            if inconclusive
            else "design"
            if not passed
            else ""
        ),
        test_results=tests,
        diagnostics=diagnostics,
    )


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

    def test_parse_build_seconds_prefers_millisecond_marker(self):
        output = "BOOLEY_BUILD_MILLISECONDS: 17\nBOOLEY_BUILD_SECONDS: 0"
        assert parse_build_seconds(output) == 0.017

    def test_parse_run_seconds_extracts_authenticated_wrapper_record(self):
        output = "BOOLEY_RUN_STAGE token=abc123 rc=0 duration_ms=19"
        assert parse_run_seconds(output) == 0.019

    def test_parse_sim_cpu_seconds_extracts_user_and_system_time(self):
        output = "BOOLEY_SIM_CPU_SECONDS: user=1.250000 system=0.125000"
        assert parse_sim_cpu_seconds(output) == (1.25, 0.125)

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
        assert lines[0] == "_booley_build_start_ns=$(date +%s%N)"
        assert lines[1] == "make -C bld"
        assert lines[2] == "_booley_build_rc=$?"
        assert "BOOLEY_BUILD_MILLISECONDS:" in script
        assert script.index("BOOLEY_BUILD_STAGE token=") < script.index("python3 -m run")
        assert "BOOLEY_RUN_STAGE token=" in script
        assert script.endswith('exit "$_booley_run_rc"')

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
# Target selection
# ---------------------------------------------------------------------------


class TestMultiConfig:
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
    def test_dry_run_prints_json(self, _mock_backend, _mock_tests, tmp_path: Path, capsys):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--dry-run"])
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        captured = capsys.readouterr()
        commands = json.loads(captured.out)
        assert isinstance(commands, list)
        assert len(commands) == 1
        assert commands[0][:2] == ["sh", "-c"]
        assert "--top alu_tb" in commands[0][2]
        assert "BOOLEY_TARGET=lite" in commands[0][2]

    @patch(
        "booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress", "boot"]}
    )
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_dry_run_expands_tests(self, _mock_backend, _mock_tests, tmp_path: Path, capsys):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--dry-run"])
        flow._run()
        captured = capsys.readouterr()
        commands = json.loads(captured.out)
        assert len(commands) == 3  # one per test

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_dry_run_with_test_filter(
        self,
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
        assert "BOOLEY_TEST_NAMES=smoke" in commands[0][2]

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_dry_run_multi_config(
        self,
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
            "    toplevel: alu_tb\n    flow_options:\n      tool: verilator\n",
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
    # Adapter command rendering is covered through SimulationExecution.preview.

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
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_summary_pass(self, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_execute", _mock_execute_fail)
    def test_summary_fail(self, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE


# ---------------------------------------------------------------------------
# Inconclusive detection
# ---------------------------------------------------------------------------


class TestInconclusiveDetection:
    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_execute", _mock_execute_inconclusive)
    def test_inconclusive_no_criterion(self, _mock_backend, _mock_tests, tmp_path: Path):
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
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_single_config_pass(self, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert result.criterion_met is True

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_execute", _mock_execute_fail)
    def test_single_config_fail(self, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert result.criterion_met is False

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_multi_test_all_pass(self, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_multi_config_mixed(self, _mock_backend, _mock_tests, tmp_path: Path):
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
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_sets_sim_pass_lite(self, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        flow._run()
        assert flow.state.is_met("sim_pass_lite")

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_execute", _mock_execute_fail)
    def test_sets_sim_pass_full_false(self, _mock_backend, _mock_tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="full")
        flow._run()
        assert not flow.state.is_met("sim_pass_full")

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_sets_criteria_per_config(self, _mock_backend, _mock_tests, tmp_path: Path):
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
    @patch.object(SimulateFlow, "_execute", _mock_execute_inconclusive)
    def test_inconclusive_skips_criterion(self, _mock_backend, _mock_tests, tmp_path: Path):
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
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_writes_config_report(self, _mock_backend, _mock_tests, tmp_path: Path):
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

    def test_native_report_persists_phase_and_resource_telemetry(self, tmp_path: Path):
        output = (
            "BOOLEY_BUILD_MILLISECONDS: 125\n"
            "BOOLEY_RUN_STAGE token=abc123 rc=0 duration_ms=875\n"
            "BOOLEY_SIM_CPU_SECONDS: user=0.750000 system=0.125000\n"
            '[SIM_RESULT] PASSED\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        )
        proc = SubprocessResult(
            returncode=0,
            stdout=output,
            duration_s=1.0,
            peak_rss_mb=42.5,
            oom_kill_delta=0,
        )
        flow = _make_flow(tmp_path, config="lite")
        with (
            patch("booley.flows.sim.flow._get_test_names", return_value={}),
            patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED),
            patch.object(flow, "_execute", return_value=proc),
        ):
            result = flow._run()

        report = json.loads((tmp_path / "reports/sim_lite.json").read_text())
        test = report["tests"][0]
        assert report["complete"] is True
        assert result.detail["resolution_s"] >= 0.0
        assert test["phase_timings_s"]["build"] == 0.125
        assert test["phase_timings_s"]["run"] == 0.875
        assert report["phase_timings_s"]["setup"] >= test["phase_timings_s"]["setup"]
        assert report["phase_timings_s"]["execution_total"] == report["elapsed_s"]
        assert test["resources"] == {
            "command_peak_rss_mb": 42.5,
            "command_oom_kill_delta": 0,
            "simulation_user_cpu_s": 0.75,
            "simulation_system_cpu_s": 0.125,
        }

    def test_interrupted_publication_is_explicitly_recoverable(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        target = TargetResult(
            target="lite",
            passed=True,
            elapsed_s=1.0,
            tests=[SimTestResult(name="smoke", passed=True)],
            phase_timings_s={"execution_total": 1.0},
        )
        with (
            patch.object(flow, "_compile_command_str", return_value=None),
            patch.object(flow, "_fileset_for_report", return_value=None),
            patch.object(flow, "_artifacts_for", return_value={}),
            patch.object(flow.state, "save", side_effect=RuntimeError("interrupted")),
            pytest.raises(RuntimeError, match="interrupted"),
        ):
            flow._persist_target_outcome(target)

        report_path = tmp_path / "reports/sim_lite.json"
        assert json.loads(report_path.read_text())["complete"] is False

        with (
            patch.object(flow, "_compile_command_str", return_value=None),
            patch.object(flow, "_fileset_for_report", return_value=None),
            patch.object(flow, "_artifacts_for", return_value={}),
        ):
            flow._persist_target_outcome(target)
        recovered = json.loads(report_path.read_text())
        assert recovered["complete"] is True
        assert recovered["phase_timings_s"]["total"] >= 1.0

    @patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["coremark"]})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_execute", _mock_execute_custom_cycle_pass)
    def test_configured_cycle_sentinel_reaches_mcp_and_json_reports(
        self,
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
        build_root = tmp_path / "build" / "lite"
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
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_no_report_dir_skips(self, _mock_backend, _mock_tests, tmp_path: Path):
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
        flow._simulation_execution_override = _BoundaryHarness(flow)
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
    def test_timeout_results_in_fail(self, _mock_backend, _mock_tests, tmp_path: Path):
        def _timeout_execute(self_inner, cmd):
            return SubprocessResult(
                returncode=-1,
                stdout="BOOLEY_BUILD_MILLISECONDS: 250\npartial output",
                stderr="",
                timed_out=True,
                duration_s=600.0,
                peak_rss_mb=96.5,
                oom_kill_delta=1,
            )

        with patch.object(SimulateFlow, "_execute", _timeout_execute):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        report = json.loads((tmp_path / "reports" / "sim_lite.json").read_text(encoding="utf-8"))
        test = report["tests"][0]
        assert report["complete"] is True
        assert test["timed_out"] is True
        assert test["verdict"] == "timeout"
        assert "TIMEOUT: simulation exceeded" in test["error_tail"]
        assert test["phase_timings_s"]["build"] == 0.25
        assert test["phase_timings_s"]["run"] == 599.75
        assert test["resources"] == {
            "command_peak_rss_mb": 96.5,
            "command_oom_kill_delta": 1,
        }


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
    def test_sim_failure_tail_from_stdout_not_stderr_noise(
        self,
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
    def test_elab_failure_tail_falls_back_to_stderr(
        self,
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
                stderr="%Error: rtl/top.sv:17: syntax error\n",
                duration_s=4.2,
            )

        with patch.object(SimulateFlow, "_execute", _execute):
            flow = _make_flow(tmp_path, config="lite")
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        assert "syntax error" in self._read_tail(tmp_path)


# ---------------------------------------------------------------------------
# The Session Runtime boundary path (wrapper-Makefile builds, ADR 0037)
# ---------------------------------------------------------------------------


class TestTruncationResilientReport:
    """The MCP layer tail-truncates Flow stdout (keeps the END), so the compact
    per-target headline block + RESULT verdict must come LAST in report_text,
    failure excerpts must be bounded, and the run.log pointer must be printed."""

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_headline_block_is_last(
        self,
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
    def test_error_excerpt_capped_with_marker(
        self,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
        capsys,
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
            flow._run()

        report = capsys.readouterr().out
        # error_tail keeps the last 50 stdout lines; the report shows only the
        # last 30 of those, with an explicit marker for the 20 omitted ones.
        assert "... (20 lines omitted, see run.log)" in report
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

        with patch.object(SimulateFlow, "_execute", _fail_and_write_log):
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
        from booley.flows.run_log import begin_run_log

        flow = _make_flow(tmp_path, config="lite")
        build_root = tmp_path / "build" / "lite"
        build_root.mkdir(parents=True)
        flow._record_run_log_dir("lite", build_root)
        begin_run_log(build_root, flow="sim", target="lite", run="somebody-else")
        write_run_log(build_root, "[SIM_RESULT] PASSED\n")

        assert flow._run_log_is_fresh("lite") is False
        assert flow._run_log_pointer("lite") is None


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
            target_identity="::sim_demo:0#sim",
            tb_top="tb_counter",
            passed=False,
            elapsed_s=1.0,
            tests=[SimTestResult(name="smoke", passed=False, elapsed_s=1.0)],
        )

        flow._write_target_report(tr)

        report = json.loads((tmp_path / "reports" / "sim_sim.json").read_text())
        assert report["target"] == "sim"
        assert report["target_identity"] == "::sim_demo:0#sim"
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
        from booley.flows.run_log import write_run_log
        from booley.flows.sim.flow import TargetResult

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
        from booley.flows.run_log import write_run_log
        from booley.flows.sim.flow import TargetResult

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

    def test_report_fileset_uses_condition_selected_inputs(self, tmp_path: Path):
        (tmp_path / "conditional.core").write_text(
            "CAPI=2:\n"
            "name: acme:ip:conditional:1.0\n"
            "filesets:\n"
            "  sources:\n"
            "    files:\n"
            "      - tool_verilator ? (rtl/selected.sv)\n"
            "      - tool_icarus ? (rtl/unselected.sv)\n"
            "      - tb/test.sv: {tags: [tb]}\n"
            "targets:\n"
            "  sim:\n"
            "    flow: sim\n"
            "    flow_options: {tool: verilator}\n"
            "    filesets: [sources]\n"
            "    toplevel: test\n",
            encoding="utf-8",
        )
        flow = _make_flow(tmp_path, config="sim", seed_core=False)

        assert flow._fileset_for_report("sim") == {
            "rtl": ["rtl/selected.sv"],
            "tb": ["tb/test.sv"],
        }


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
    def test_missing_verilator_in_build_exits_2(self, _sel, _tests, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        # What the sandbox script actually prints with verilator off PATH: the
        # canonical elab marker (accurate rc, wrong stage) over sh's own gripe.
        stdout = (
            "/bin/sh: 1: verilator: not found\n"
            "make: *** [Makefile:9: Vtop] Error 127\n"
            "ERROR: Verilator elaboration failed (rc=2)\n"
            "BOOLEY_BUILD_STAGE token=abc123 rc=2\n"
        )
        with patch.object(SimulateFlow, "_execute", _missing_binary_execute(stdout)):
            result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert result.detail["missing_executable"] == "verilator"
        assert "elaboration failed" not in result.report_text.split("--- output tail ---")[0]

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_live_sim_echoing_command_not_found_is_not_hijacked(
        self, _sel, _tests, tmp_path: Path
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
    def test_trace_path_and_size_reach_the_report(self, _sel, _tests, tmp_path: Path):
        store = tmp_path / "build" / "lite"
        store.mkdir(parents=True)
        fst = store / "dump.fst"

        flow = _make_flow(tmp_path, config="lite", extra_args=["--trace"])

        def _execute(_self, _command):
            fst.write_bytes(b"x" * 4096)
            stdout = (
                f"TRACE_OK: {fst}\n"
                'TRACE_METADATA: {"top_scope":"tb.dut","signal_count":42,'
                '"total_ticks":900,"size_bytes":4096}\n'
                '[SIM_RESULT] PASSED\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
            )
            return SubprocessResult(returncode=0, stdout=stdout, duration_s=0.05)

        with patch.object(SimulateFlow, "_execute", _execute):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        rel = "build/lite/dump.fst"
        assert f"trace: {rel} (4.0 KB, 42 signals, scope tb.dut, 900 ticks)" in result.report_text
        report = json.loads((tmp_path / "reports" / "sim_lite.json").read_text())
        assert report["tests"][0]["trace_path"] == rel
        assert report["tests"][0]["trace_bytes"] == 4096
        assert report["tests"][0]["trace_top_scope"] == "tb.dut"
        assert report["tests"][0]["trace_signal_count"] == 42
        assert report["tests"][0]["trace_total_ticks"] == 900

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    @patch.object(SimulateFlow, "_execute", _mock_execute_pass)
    def test_untraced_run_reports_no_trace_line(self, _sel, _tests, tmp_path: Path):
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

    def test_format_bytes_supports_gigabytes(self):
        from booley.flows.sim.flow import _format_bytes

        assert _format_bytes(5 * 1024**3) == "5.0 GB"

    def test_status_line_distinguishes_two_fast_tests(self):
        fast = _test_status_line(SimTestResult(name="a", passed=True, elapsed_s=0.011))
        slower = _test_status_line(SimTestResult(name="b", passed=True, elapsed_s=0.019))
        assert "11ms" in fast
        assert "19ms" in slower
        assert fast != slower

    def test_report_entry_keeps_millisecond_resolution(self):
        from booley.flows.sim.flow import _test_report_entry

        entry = _test_report_entry(
            SimTestResult(
                name="a",
                passed=True,
                elapsed_s=0.012,
                build_s=0.007,
                phase_timings_s={"build": 0.007, "run": 0.005},
                resources={"command_peak_rss_mb": 12.5},
            )
        )
        assert entry["elapsed_s"] == 0.012
        assert entry["build_s"] == 0.007
        assert entry["phase_timings_s"] == {"build": 0.007, "run": 0.005}
        assert entry["resources"] == {"command_peak_rss_mb": 12.5}


# ---------------------------------------------------------------------------
# Selector round trip: producer argv -> run-half argparse (F-12 family)
# ---------------------------------------------------------------------------


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
