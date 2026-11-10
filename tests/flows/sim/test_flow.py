"""Tests for SimulateFlow — multi-config sim, cycle parsing, dry-run, reports."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.run_log import write_run_log
from booley.flows.sim.adapter_transport import (
    write_adapter_result,
)
from booley.flows.sim.build import PreparedSimulationBuild
from booley.flows.sim.execution import (
    SimulationArtifactEvidence,
    SimulationExecution,
    SimulationOptions,
)
from booley.flows.sim.execution.engine import _adapter_shell_command
from booley.flows.sim.execution.telemetry import (
    parse_build_seconds,
    parse_run_seconds,
    parse_sim_cpu_seconds,
)
from booley.flows.sim.flow import (
    _INCONCLUSIVE_NO_SENTINEL,
    TargetResult,
    _append_batch_output_lines,
    _append_error_excerpt,
    _artifact_path_component,
    _build_display_lines,
    _build_run_script,
    _filter_tests,
    _resolve_sim_campaign_work_units,
    _test_status_line,
    parse_sva_errors,
)
from booley.flows.sim.flow import (
    SimulateFlow as ProductionSimulateFlow,
)
from booley.flows.sim.flow import (
    TestResult as SimTestResult,  # aliased: a Test* name would be pytest-collected
)
from booley.flows.sim.mode import SimulationMode
from booley.flows.sim.trace_recipe import TraceMode
from booley.fusesoc.fusesoc_registry import ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.runtime.endpoint_execution import ExecutionResult
from booley.targets.catalog import TargetCatalog
from tests.flows.sim import campaign_boundary
from tests.flows.sim.campaign_boundary import (
    FAIL_PROCESS,
    INCONCLUSIVE_PROCESS,
    PASS_PROCESS,
    CampaignSimRun,
    Launch,
    run_campaign_sim,
)
from tests.flows.sim.leased_session import canned_build_session, without_build_record
from tests.target_test_support import write_sim_preview_project

# Built-in Flow execution inside the Sandbox.
_FLOW_ENABLED = True


class SimulateFlow(ProductionSimulateFlow):
    """Concrete Flow used by the campaign compatibility tests."""


@pytest.fixture(autouse=True)
def _canned_build_session():
    """Run canned Flow tests against a leased build session with stubbed inputs."""
    with canned_build_session():
        yield


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
        patch("booley.flows.sim.flow.monotonic", side_effect=[10.0, 10.25]),
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
        "10/10 targets passed, 10s",
        "✓ config_0  1s",
        "✓ config_1  1s",
        "✓ config_2  1s",
        "... and 7 more targets",
    ]


def test_human_display_does_not_repeat_single_passing_target_or_test():
    results = [
        TargetResult(
            target="sim_mul",
            passed=True,
            elapsed_s=9.8,
            tests=[SimTestResult(name="smoke", passed=True, elapsed_s=9.8)],
        )
    ]

    assert _build_display_lines(results, total_elapsed=10.0) == ["✓ PASS  10s"]


def test_human_display_keeps_target_detail_for_multiple_passing_tests():
    results = [
        TargetResult(
            target="sim_mul",
            passed=True,
            tests=[
                SimTestResult(name="smoke", passed=True),
                SimTestResult(name="edge", passed=True),
            ],
        )
    ]

    assert _build_display_lines(results, total_elapsed=2.0) == [
        "1/1 targets passed, 2s",
        "✓ sim_mul (2/2 tests)  0s",
    ]


def test_human_display_keeps_names_for_single_target_failure():
    results = [
        TargetResult(
            target="sim_mul",
            passed=False,
            elapsed_s=1.0,
            tests=[SimTestResult(name="edge", passed=False, error_tail="assertion failed")],
        )
    ]

    lines = _build_display_lines(results, total_elapsed=1.0)

    assert lines[1] == "✗ sim_mul  1s"
    assert any("edge" in line for line in lines)


def _display_flow(tmp_path: Path, target: str, *args: str) -> SimulateFlow:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", target, *args])
    return flow


def test_display_label_names_one_requested_test(tmp_path: Path):
    flow = _display_flow(tmp_path, "::demo:core:0#sim_mul", "--test", "smoke")

    with patch("booley.flows.sim.flow._get_test_names", return_value={}):
        label = flow._resolve_display_label()

    assert label == "target sim_mul · test smoke"


def test_display_label_counts_distinct_exact_tests(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_mul", "--test", "mul_base", "--test", "mul_edge")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"sim_mul": ["mul_base", "mul_edge"]},
        ),
        patch("booley.flows.sim.flow._get_test_skips", return_value={}),
    ):
        label = flow._resolve_display_label()

    assert label == "target sim_mul · 2 tests"


def test_display_label_applies_configured_skips_before_naming_test(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_mul")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"sim_mul": ["smoke", "edge"]},
        ),
        patch("booley.flows.sim.flow._get_test_skips", return_value={"sim_mul": ["edge"]}),
    ):
        label = flow._resolve_display_label()

    assert label == "target sim_mul · test smoke"


def test_display_label_deduplicates_same_test_across_targets(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_a,sim_b", "--test", "smoke")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"sim_a": ["smoke"], "sim_b": ["smoke"]},
        ),
        patch("booley.flows.sim.flow._get_test_skips", return_value={}),
    ):
        label = flow._resolve_display_label()

    assert label == "2 targets · test smoke"


def test_display_label_uses_unknown_suite_when_any_target_is_undeclared(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_a,sim_b")

    with (
        patch(
            "booley.flows.sim.flow._get_test_names",
            return_value={"sim_a": ["smoke"]},
        ),
        patch("booley.flows.sim.flow._get_test_skips", return_value={}),
    ):
        label = flow._resolve_display_label()

    assert label == "2 targets · tests"


def test_display_label_uses_elaboration_mode_without_test_discovery(tmp_path: Path):
    flow = _display_flow(tmp_path, "sim_core", "--mode", "elab-only-standalone")

    with patch("booley.flows.sim.flow._get_test_names") as get_tests:
        label = flow._resolve_display_label()

    assert label == "target sim_core · elaboration"
    get_tests.assert_not_called()


def test_display_label_failure_uses_neutral_tests_with_short_target(tmp_path: Path):
    flow = _display_flow(tmp_path, "::demo:core:0#sim_mul")

    with patch("booley.flows.sim.flow._get_test_names", side_effect=RuntimeError("boom")):
        label = flow._resolve_display_label()

    assert label == "target sim_mul · tests"


def test_display_label_does_not_resolve_target_catalog(tmp_path: Path):
    flow = _display_flow(tmp_path, "::demo:core:0#sim_mul", "--test", "smoke")

    with (
        patch("booley.flows.sim.flow._get_test_names", return_value={}),
        patch("booley.targets.catalog.TargetCatalog.build") as build_catalog,
    ):
        flow._resolve_display_label()

    build_catalog.assert_not_called()


def test_campaign_work_units_count_native_tests_and_cocotb_batches(tmp_path: Path):
    with (
        patch(
            "booley.flows.sim.flow._target_is_cocotb",
            side_effect=lambda _root, target: target == "cocotb",
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


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (SimulationMode.ELAB_ONLY, 2),
        (SimulationMode.ELAB_ONLY_STANDALONE, 3),
    ],
)
def test_campaign_work_units_count_elaboration_modes(
    tmp_path: Path,
    mode: SimulationMode,
    expected: int,
) -> None:
    assert _resolve_sim_campaign_work_units(tmp_path, "one,two", mode=mode) == expected


def test_artifact_path_component_never_embeds_unsafe_test_names():
    assert _artifact_path_component("sign_case_65") == "sign_case_65"
    encoded = _artifact_path_component("../../outside/reports")
    assert encoded.startswith("~sha256-")
    assert "/" not in encoded
    assert encoded != _artifact_path_component("../../different")


def test_direct_evidence_namespaces_separate_candidate_and_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _make_flow(tmp_path, config="lite")
    with flow.publication_resources:
        invocation = flow.reserve_invocation_dir()
        assert invocation is not None
        monkeypatch.setattr(
            flow,
            "_planned_cycle_baseline_selection",
            lambda: ("abc123", ["lite"], None),
        )

        assert flow._simulation_artifact_root(None) == invocation / "artifacts/candidate"
        assert flow._simulation_artifact_root("baseline") == (
            invocation / "artifacts/baseline/abc123"
        )


def test_baseline_evidence_is_reported_and_prunable(tmp_path: Path) -> None:
    from booley.flows.sim.campaign_retention import prune_invocation
    from booley.flows.sim.coverage_progress import CoverageProgress

    flow = _make_flow(tmp_path, config="lite")
    with flow.publication_resources:
        invocation = flow.reserve_invocation_dir()
        assert invocation is not None
        log = invocation / "artifacts/baseline/abc123/sim_lite/tests/smoke/run.log"
        log.parent.mkdir(parents=True)
        log.write_text("baseline output", encoding="utf-8")
        identity = "acme:lib:demo:1#lite"
        flow._baseline_results = {
            identity: TargetResult(
                target="lite",
                target_identity=identity,
                artifacts=(
                    SimulationArtifactEvidence(
                        "run_log", str(log), log.stat().st_size, ("smoke",)
                    ),
                ),
            )
        }
        candidate = TargetResult(target="lite", target_identity=identity, passed=True)

        flow._write_target_report(candidate)
        CoverageProgress(invocation, ("lite",)).checkpoint(complete=True)
        report = json.loads((invocation / "targets/lite/simulation.json").read_text())

        assert report["baseline_evidence"][identity]["artifacts"]["log"].endswith(
            "artifacts/baseline/abc123/sim_lite/tests/smoke/run.log"
        )
    prune_invocation(tmp_path / "reports", 1)
    assert not invocation.exists()


def test_artifact_report_keys_do_not_drop_colliding_test_names(tmp_path: Path) -> None:
    flow = _make_flow(tmp_path, config="lite")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    block = flow._artifacts_for(
        TargetResult(
            target="lite",
            artifacts=(
                SimulationArtifactEvidence("result", str(first), 2, ("a_b", "c")),
                SimulationArtifactEvidence("result", str(second), 2, ("a", "b_c")),
            ),
        )
    )

    assert set(block.values()) == {"first.json", "second.json"}
    assert len(block) == 2


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
    # (BOOLEY_DIR=tmp_path). Skip for multi/empty config (not a Target)
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
    booley_dir = tmp_path / ".booley"
    if booley_dir.exists():
        env["BOOLEY_DIR"] = str(booley_dir)
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
        self._stashed = None
        super().__init__(
            invoke=self._invoke_test_process,
            artifact_root=flow.args.report_dir,
            options=SimulationOptions(
                trace=flow.args.trace,
                timeout_ms=flow.args.timeout_ms,
                result_verbosity=flow.args.result_verbosity,
            ),
        )

    def _prepare_build(self, handle):
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
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

    def _prepare_attempt(self, handle, test_names, **kwargs):
        attempt = super()._prepare_attempt(handle, test_names, **kwargs)
        self._attempt = attempt
        return attempt

    def _invoke_test_process(self, command, *, timeout):
        del timeout
        assert self._attempt is not None
        if "BOOLEY_BUILD_STAGE" not in command[-1]:
            stashed, self._stashed = self._stashed, None
            write_adapter_result(
                self._attempt.identity,
                campaign_boundary.adapter_result_for_test_process(
                    self._attempt.identity.selected_tests, stashed
                ),
            )
            fresh_ns = time.time_ns()
            os.utime(self._attempt.identity.result_path, ns=(fresh_ns, fresh_ns))
            return without_build_record(stashed)
        launch = _adapter_shell_command(self._attempt)[-1]
        process = self._flow._execute([*command[:-1], f"{command[-1]}\n{launch}"])
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
            self._stashed = process
            return SubprocessResult(returncode=0, stdout=marker)
        return SubprocessResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=process.stderr,
            timed_out=process.timed_out,
            duration_s=process.duration_s,
            peak_rss_mb=process.peak_rss_mb,
            oom_kill_delta=process.oom_kill_delta,
        )


# ---------------------------------------------------------------------------
# Build-time attribution (first test of a run pays the edalize make)
# ---------------------------------------------------------------------------


class TestBuildTimeAttribution:
    """After an RTL edit the first test absorbs the whole model rebuild; the
    report must say so ("reset PASS 5.0s" with a 0.1s sim misleads timing
    triage)."""

    def test_parse_build_seconds_extracts_marker(self):
        output = "make stuff\nBOOLEY_BUILD_MILLISECONDS: 5000\n[SIM_RESULT] PASSED"
        assert parse_build_seconds(output) == 5.0

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
        assert parse_build_seconds("echo BOOLEY_BUILD_MILLISECONDS: 9000") == 0.0

    def test_build_run_script_brackets_the_make_half(self):
        script = _build_run_script(
            ["make", "-C", "bld"], "Verilator elaboration failed", "python3 -m run"
        )
        lines = script.splitlines()
        assert lines[0] == "_booley_build_start_ns=$(date +%s%N)"
        assert lines[1] == "make -C bld"
        assert lines[2] == "_booley_build_rc=$?"
        assert "BOOLEY_BUILD_MILLISECONDS:" in script
        assert "BOOLEY_BUILD_SECONDS" not in script
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
    def test_partial_name_does_not_match(self):
        tests = ["lite_smoke", "lite_stress", "lite_boot"]
        assert _filter_tests(tests, "stress") == []

    def test_multiple_exact_names_preserve_request_order(self):
        tests = ["lite_smoke", "lite_stress", "lite_boot"]
        assert _filter_tests(tests, ("lite_boot", "lite_smoke")) == [
            "lite_boot",
            "lite_smoke",
        ]

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

    def test_multiple_config_skips(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        with patch(
            "booley.flows.sim.flow._get_test_skips",
            return_value={"lite": ["lite_stress", "lite_boot"]},
        ):
            assert flow._resolve_tests_to_run("lite", self._NAMES) == ["lite_smoke"]
            assert sorted(flow._skipped_tests("lite", self._NAMES)) == [
                "lite_boot",
                "lite_stress",
            ]

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

    def test_valid_exact_name_passes(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "lite_stress"])
        assert flow._validate_test_selector(["lite"], self._NAMES) is None

    def test_no_selector_is_noop(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        assert flow._validate_test_selector(["lite"], self._NAMES) is None

    def test_no_declared_list_rejects_named_selection(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "anything"])
        assert flow._validate_test_selector(["lite"], {}) is not None

    def test_unknown_for_one_of_many_targets_errors(self, tmp_path: Path):
        # Valid for one target, absent from another → still an error: the target
        # missing the test would otherwise run its default and report a PASS.
        names = {"lite": ["lite_smoke"], "full": ["full_sign"]}
        flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "lite_smoke"])
        result = flow._validate_test_selector(["lite", "full"], names)
        assert result is not None
        assert result.exit_code == EXIT_ERROR
        assert "full" in result.report_text


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

    def test_no_state_file_records_workload_criterion(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        flow.args.state_file = None  # standalone / Interactive Mode
        flow.set_criterion = MagicMock()
        flow._record_sim_criterion(self._passed_target())
        flow.set_criterion.assert_called_once()
        assert flow.set_criterion.call_args.args == ("sim_pass_lite", True)

    def test_state_file_records_criterion(self, tmp_path: Path):
        flow = _make_flow(tmp_path, config="lite")
        assert flow.args.state_file is not None  # Ticket Mode
        flow.set_criterion = MagicMock()
        flow._record_sim_criterion(self._passed_target())
        flow.set_criterion.assert_called_once()
        assert flow.set_criterion.call_args.kwargs["detail"]["mode"] == "simulate"


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------


class TestExecutionValidation:
    def test_job_class_is_heavy(self, tmp_path: Path):
        from booley.runtime import job_slots

        flow = _make_flow(tmp_path, config="lite")
        assert flow._resolve_job_class() == job_slots.CLASS_HEAVY


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def _preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    targets: dict[str, list[str]],
    **request: Any,
) -> ExecutionResult:
    """Dry-run ``SimulateFlow.execute`` over one ``.core`` per Target in *targets*.

    *targets* maps each selector to its configured Tests (empty: none configured).
    """
    from booley.flows.sim.request import SimRequest
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    data = write_sim_preview_project(tmp_path, targets)
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    reset_cache()
    return SimulateFlow().execute(
        SimRequest(dry_run=True, work_dir=tmp_path, report_dir=tmp_path / "reports", **request)
    )


class TestDryRun:
    def test_plan_keeps_non_shell_commands_unchanged(self, tmp_path: Path) -> None:
        flow = _make_flow(tmp_path, config="lite")
        command = ("make", "-C", ".booley_work/sim")

        assert flow._redact_plan_environment("lite", command) == command

    def test_plan_collects_preview_errors(self, tmp_path: Path) -> None:
        flow = _make_flow(tmp_path, config="lite")
        execution = MagicMock()
        execution.preview.side_effect = ValueError("preview failed")
        flow._simulation_execution_override = execution

        units, errors = flow._plan_simulation_targets(
            ["lite"],
            {},
            role="ordinary",
            revision=None,
        )

        assert units == []
        assert errors == ["lite: preview failed"]

    def test_plan_reports_a_refused_private_mirror_as_a_planning_error(
        self, tmp_path: Path
    ) -> None:
        """A mirror refusal (#2895) is a planning error, not an escaped exception."""
        from booley.flows.sim.build import SimulationBuildPreparationError

        flow = _make_flow(tmp_path, config="lite")
        execution = MagicMock()
        execution.preview.side_effect = SimulationBuildPreparationError("mirror refused")
        flow._simulation_execution_override = execution

        units, errors = flow._plan_simulation_targets(["lite"], {}, role="ordinary", revision=None)

        assert units == []
        assert errors == ["lite: mirror refused"]

    @patch("booley.flows.sim.flow._get_test_names", return_value={})
    @patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED)
    def test_plan_fingerprint_changes_with_configured_environment(
        self,
        _mock_backend,
        _mock_tests,
        tmp_path: Path,
    ) -> None:
        flow = _make_flow(tmp_path, config="lite")
        with patch.object(flow, "_target_sim_env", return_value={"FLAVOR": "fast"}):
            first = flow._plan_simulation(["lite"], {})
        with patch.object(flow, "_target_sim_env", return_value={"FLAVOR": "safe"}):
            second = flow._plan_simulation(["lite"], {})

        assert first.semantic_plan_fingerprint != second.semantic_plan_fingerprint

    @pytest.mark.parametrize(
        ("targets", "request_args", "expected"),
        [
            pytest.param({"lite": []}, {}, {"lite": 1}, id="default-selection"),
            pytest.param(
                {"lite": ["smoke", "stress", "boot"]}, {}, {"lite": 3}, id="one-item-per-test"
            ),
            pytest.param(
                {"lite": ["smoke", "stress"]}, {"test": ("smoke",)}, {"lite": 1}, id="test-filter"
            ),
            pytest.param(
                {"lite": ["smoke", "stress"], "full": ["smoke"]},
                {"target": "lite,full", "test": ("smoke",)},
                {"lite": 1, "full": 1},
                id="multi-target",
            ),
        ],
    )
    def test_dry_run_previews_one_work_item_per_selected_test(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        targets: dict[str, list[str]],
        request_args: dict[str, Any],
        expected: dict[str, int],
    ) -> None:
        request_args = {"target": "lite", **request_args}
        result = _preview(tmp_path, monkeypatch, targets, **request_args)

        outcome = result.outcome
        assert result.exit_code == EXIT_SUCCESS, outcome.report_text
        assert {
            preview["target"]["selector"]: preview["work_items"]
            for preview in outcome.detail["campaign_preview"]
        } == expected
        assert outcome.detail["mutated"] is False
        # A dry run reserves no invocation directory and takes no lock.
        report_root = tmp_path / "reports/sim"
        assert not [p for p in report_root.glob("*") if p.name.isdigit() or p.suffix == ".lock"]


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


# ---------------------------------------------------------------------------
# Ordinary simulation through the campaign path (SimulateFlow.execute)
#
# These run the real endpoint, campaign planning and execution, and Criteria
# reconciliation; only the process edge is faked (``campaign_boundary``).
# ---------------------------------------------------------------------------


def _projection(tmp_path: Path, target: str = "lite") -> dict[str, Any]:
    """Return the ``simulation.json`` projection Target *target* published."""
    path = tmp_path / f"reports/sim/1/targets/{target}/simulation.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _launched_tests(run: CampaignSimRun) -> list[str]:
    return [test for _target, tests in run.launched for test in tests]


def test_campaign_rejects_unknown_test_without_launching(tmp_path: Path, monkeypatch):
    # The reported bug: --test no_such_test used to run the TB's default test and
    # report PASS. The campaign must reject it before any simulator launches.
    run = run_campaign_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress"]},
        PASS_PROCESS,
        target="lite",
        test=("typo",),
    )
    assert run.result.exit_code == EXIT_ERROR
    report_text = run.result.outcome.report_text
    assert "typo" in report_text
    assert "lite" in report_text
    # The declared tests are listed so the fix (a typo) is one hop away.
    assert "smoke" in report_text
    assert run.launched == []


@pytest.mark.parametrize(
    ("selector", "expected"),
    [(None, ["smoke", "boot"]), (("stress",), ["stress"])],
    ids=["one-skip", "explicit-test-overrides-skip"],
)
def test_campaign_runs_tests_left_by_skip_list(
    tmp_path: Path, monkeypatch, selector: tuple[str, ...] | None, expected: list[str]
):
    # Naming a skipped test by hand is a clear override of the skip list.
    run = run_campaign_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress", "boot"]},
        PASS_PROCESS,
        skip={"lite": ["stress"]},
        target="lite",
        test=selector,
    )
    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    assert _launched_tests(run) == expected


def test_campaign_rejects_target_whose_tests_are_all_skipped(tmp_path: Path, monkeypatch):
    run = run_campaign_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress"]},
        PASS_PROCESS,
        skip={"lite": ["smoke", "stress"]},
        target="lite",
    )
    assert run.result.exit_code == EXIT_ERROR
    assert "no runnable tests" in run.result.outcome.report_text
    assert run.launched == []


def _lite_passes_full_fails(launch: Launch) -> SubprocessResult:
    return PASS_PROCESS if launch.target == "lite" else FAIL_PROCESS


@pytest.mark.parametrize(
    ("targets", "process", "target", "exit_code", "grades"),
    [
        ({"lite": ["smoke"]}, PASS_PROCESS, "lite", EXIT_SUCCESS, {"lite": "pass"}),
        ({"lite": ["smoke"]}, FAIL_PROCESS, "lite", EXIT_FAILURE, {"lite": "fail"}),
        (
            {"lite": ["smoke"]},
            INCONCLUSIVE_PROCESS,
            "lite",
            EXIT_FAILURE,
            {"lite": "inconclusive"},
        ),
        (
            {"lite": ["smoke", "stress"]},
            PASS_PROCESS,
            "lite",
            EXIT_SUCCESS,
            {"lite": "pass"},
        ),
        (
            {"lite": ["smoke"], "full": ["smoke"]},
            _lite_passes_full_fails,
            "lite,full",
            EXIT_FAILURE,
            {"lite": "pass", "full": "fail"},
        ),
    ],
    ids=["summary-pass", "summary-fail", "inconclusive", "two-tests-pass", "mixed-targets"],
)
def test_campaign_verdict_sets_exit_code(
    tmp_path: Path, monkeypatch, targets, process, target, exit_code, grades
):
    run = run_campaign_sim(tmp_path, monkeypatch, targets, process, target=target)
    outcome = run.result.outcome
    assert run.result.exit_code == exit_code, outcome.report_text
    assert {name: row["grade"] for name, row in outcome.detail["campaigns"].items()} == grades
    assert f"report: {tmp_path / 'reports/sim.json'}" in outcome.report_text
    if grades == {"lite": "inconclusive"}:
        assert "INCONCLUSIVE" in outcome.report_text


def _ticket_state(tmp_path: Path, monkeypatch, keys: list[str]) -> Path:
    """Save a strict Ticket state declaring *keys*, and name it as the state file."""
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.slug, state.ticket_type = "ticket", "implementation"
    state.init_criteria(dict.fromkeys(keys, True), strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    return state_path


@pytest.mark.parametrize(
    ("process", "ticket", "expected"),
    [
        (_lite_passes_full_fails, True, {"sim_pass_lite": True, "sim_pass_full": False}),
        (INCONCLUSIVE_PROCESS, True, {"sim_pass_lite": False, "sim_pass_full": False}),
        (PASS_PROCESS, False, {"sim_pass_lite": True}),
        (FAIL_PROCESS, False, {"sim_pass_lite": False}),
        (INCONCLUSIVE_PROCESS, False, {}),
    ],
    ids=[
        "ticket-mixed",
        "ticket-inconclusive",
        "standalone-pass",
        "standalone-fail",
        "standalone-inconclusive",
    ],
)
def test_campaign_records_criteria(
    tmp_path: Path, monkeypatch, process, ticket: bool, expected: dict[str, bool]
):
    """Ticket Mode reconciles every declared Criterion through the acceptance
    coordinator; a standalone run records pass and fail but nothing when inconclusive."""
    from tests.flows.sim.test_coverage_flow import _AcceptanceAdapter

    targets = {"lite": ["smoke"], "full": ["smoke"]} if ticket else {"lite": ["smoke"]}
    if not ticket:
        monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
        run = run_campaign_sim(tmp_path, monkeypatch, targets, process, target="lite")
        criteria = run.flow.state.criteria
        assert {key: criterion.met for key, criterion in criteria.items()} == expected
        return
    state_path = _ticket_state(tmp_path, monkeypatch, list(expected))
    recorder = _AcceptanceAdapter(
        log_dir=tmp_path / "logs",
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    run_campaign_sim(tmp_path, monkeypatch, targets, process, target="lite,full", adapter=recorder)
    saved = DevelopmentState.load(state_path)
    assert {key: saved.is_met(key) for key in expected} == expected
    # One acceptance transaction per declared Criterion: the coordinator recorded them.
    assert len(saved.acceptance_transactions) == len(expected)


@pytest.mark.skipif(os.name == "nt", reason="POSIX unreadable-directory regression")
@pytest.mark.parametrize("relative", ["unreadable", ".booley/unreadable"])
def test_unreadable_unrelated_directory_does_not_block_plain_simulation(
    tmp_path: Path, monkeypatch, relative: str
) -> None:
    unreadable = tmp_path / relative
    unreadable.mkdir(parents=True)
    unreadable.chmod(0)
    try:
        run = run_campaign_sim(
            tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite"
        )
    finally:
        unreadable.chmod(0o700)

    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text


def test_campaign_projection_reports_tests_and_label(tmp_path: Path, monkeypatch):
    run = run_campaign_sim(
        tmp_path, monkeypatch, {"lite": ["smoke", "stress"]}, PASS_PROCESS, target="lite"
    )
    report = _projection(tmp_path)
    assert report["flow"] == "sim"
    assert report["target"] == "lite"
    assert report["passed"] is True
    assert [test["name"] for test in report["tests"]] == ["smoke", "stress"]
    assert report["tests"][0]["cycles"] == 2561
    assert run.result.outcome.display_label == "target lite · 2 tests"


def test_configured_cycle_sentinel_reaches_report_and_projection(tmp_path: Path, monkeypatch):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(
        '[flows.sim]\ncycle_sentinels = ["CoreMark completed in:"]\n', encoding="utf-8"
    )
    process = SubprocessResult(
        returncode=0,
        stdout=(
            "[SIM_RESULT] PASSED\n"
            "CoreMark completed in: coremark 31415\n"
            '[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        ),
        duration_s=1.0,
    )
    run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["coremark"]}, process, target="lite")

    assert "coremark: cycles=31415" in run.result.outcome.report_text
    assert _projection(tmp_path)["tests"][0]["cycles"] == 31415


def _run_logs(tmp_path: Path) -> list[Path]:
    """Every work item's evidence ``run.log``, in work-item order."""
    return sorted((tmp_path / "reports").rglob("execution-evidence/run.log"))


def test_campaign_keeps_each_test_log_in_its_own_work_item(tmp_path: Path, monkeypatch):
    """Each HDL launch gets durable evidence before the next one starts."""
    first_bytes: list[bytes] = []

    def process(launch: Launch) -> SubprocessResult:
        if launch.tests == ("stress",):
            first_bytes.append(_run_logs(tmp_path)[0].read_bytes())
            return SubprocessResult(
                returncode=1, stdout="STRESS_ONLY\n[SIM_RESULT] FAILED\n", duration_s=0.1
            )
        return SubprocessResult(
            returncode=0, stdout="SMOKE_ONLY\n[SIM_RESULT] PASSED\n", duration_s=0.1
        )

    run = run_campaign_sim(
        tmp_path, monkeypatch, {"lite": ["smoke", "stress"]}, process, target="lite"
    )

    assert run.result.exit_code == EXIT_FAILURE
    assert _launched_tests(run) == ["smoke", "stress"]
    smoke_log, stress_log = _run_logs(tmp_path)
    assert smoke_log.read_bytes() == first_bytes[0]
    assert "SMOKE_ONLY" in smoke_log.read_text(encoding="utf-8")
    assert "STRESS_ONLY" not in smoke_log.read_text(encoding="utf-8")
    assert "STRESS_ONLY" in stress_log.read_text(encoding="utf-8")
    assert "SMOKE_ONLY" not in stress_log.read_text(encoding="utf-8")


def test_consecutive_campaigns_preserve_first_evidence(tmp_path: Path, monkeypatch):
    def run(output: str) -> tuple[Path, bytes]:
        run_campaign_sim(
            tmp_path,
            monkeypatch,
            {"lite": ["smoke"]},
            SubprocessResult(returncode=0, stdout=f"{output}\n[SIM_RESULT] PASSED\n"),
            target="lite",
            test=("smoke",),
        )
        path = _run_logs(tmp_path)[-1]
        return path, path.read_bytes()

    first_path, first_bytes = run("FIRST_ONLY")
    second_path, second_bytes = run("SECOND_ONLY")

    assert first_path != second_path
    assert first_path.read_bytes() == first_bytes
    assert b"FIRST_ONLY" in first_bytes
    assert b"SECOND_ONLY" in second_bytes


def test_campaign_crash_keeps_completed_target_in_progress(tmp_path: Path, monkeypatch):
    def process(launch: Launch) -> SubprocessResult:
        if launch.target == "full":
            raise RuntimeError("simulated outer interruption")
        return PASS_PROCESS

    monkeypatch.setenv("BOOLEY_RUN_ID", "sim-checkpoint-1")
    run = run_campaign_sim(
        tmp_path, monkeypatch, {"lite": ["smoke"], "full": ["smoke"]}, process, target="lite,full"
    )

    assert run.result.exit_code == EXIT_ERROR
    assert "simulated outer interruption" in run.result.outcome.report_text
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text(encoding="utf-8"))
    assert progress["run_id"] == "sim-checkpoint-1"
    assert progress["complete"] is True
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == ["lite"]
    assert progress["pending_targets"] == ["full"]


def test_default_report_root_uses_project_data(tmp_path: Path, monkeypatch):
    rtl = tmp_path / "rtl-checkout"
    rtl.mkdir()
    project_data = tmp_path / "project-data"
    run = run_campaign_sim(
        rtl,
        monkeypatch,
        {"lite": []},
        PASS_PROCESS,
        target="lite",
        report_dir=None,
        project_data=project_data,
    )
    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    invocation = project_data / "flow-reports/sim/1"
    assert (invocation / "progress.json").is_file()
    assert (invocation / "targets/lite/simulation.json").is_file()
    assert not (rtl / "flow-reports").exists()


# ---------------------------------------------------------------------------
# Structured report generation
# ---------------------------------------------------------------------------


class TestReportGeneration:
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
            flow.publication_resources,
        ):
            result = flow._run()

        report = json.loads((tmp_path / "reports/sim/1/targets/lite/simulation.json").read_text())
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
        with flow.publication_resources:
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

            report_path = tmp_path / "reports/sim/1/targets/lite/simulation.json"
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
        flow = _make_flow(tmp_path, extra_args=["--timeout-ms", "120000"])
        assert flow._get_timeout() == 120

    def test_timeout_results_in_fail(self, tmp_path: Path, monkeypatch):
        # The wrapper killed the launch: no adapter result, only the timed-out process.
        process = SubprocessResult(
            returncode=-1,
            stdout="partial output",
            timed_out=True,
            duration_s=600.0,
        )
        run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["smoke"]}, process, target="lite")

        assert run.result.exit_code == EXIT_FAILURE
        test = _projection(tmp_path)["tests"][0]
        assert test["timed_out"] is True
        assert test["execution"] == "timeout"
        hint = "raise --timeout-ms or [flows.sim].timeout_ms if the test legitimately needs longer"
        assert "TIMEOUT: simulation exceeded" in test["error_tail"]
        assert hint in test["error_tail"]
        assert hint in run.result.outcome.report_text


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
    the build's stderr lint-warning storm — except for a failed build, whose only
    diagnostic lives on stderr with no sim stdout to fall back to."""

    def test_sim_failure_tail_from_stdout_not_stderr_noise(self, tmp_path: Path, monkeypatch):
        """A real sim FAIL: the build's stderr warning storm must not bury the
        stdout mismatch line in the 50-line excerpt window."""
        noise = "\n".join(
            f"/work/rtl/foo.sv:{i}: warning: @* is sensitive to all 16 words" for i in range(60)
        )
        process = SubprocessResult(
            returncode=1,
            stdout="[SIM_RESULT] FAILED\nFAIL: coeff mismatch at index 7\n",
            stderr=noise,
            duration_s=5.0,
        )
        run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["smoke"]}, process, target="lite")

        assert run.result.exit_code == EXIT_FAILURE
        tail = _projection(tmp_path)["tests"][0]["error_tail"]
        assert "coeff mismatch at index 7" in tail
        assert "warning:" not in tail

    def test_elab_failure_tail_falls_back_to_stderr(self, tmp_path: Path, monkeypatch):
        """A failed build produces no sim stdout — the excerpt must carry the
        stderr diagnostic so it is not lost."""
        build = SubprocessResult(returncode=1, stderr="%Error: rtl/top.sv:17: syntax error\n")
        run = run_campaign_sim(
            tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite", build=build
        )

        assert run.result.exit_code == EXIT_FAILURE
        assert run.launched == []
        assert "syntax error" in _projection(tmp_path)["tests"][0]["error_tail"]


# ---------------------------------------------------------------------------
# The Sandbox boundary path (wrapper-Makefile builds, ADR 0037)
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
            with flow.publication_resources:
                result = flow._run()

        lines = result.report_text.splitlines()
        # The final verdict and report destination remain in the protected tail.
        assert lines[-1].startswith("RESULT: FAIL")
        assert lines[-3] == f"  report: {(Path(flow.args.report_dir) / 'sim.json').resolve()}"
        # The headline block sits after ALL verbose per-test detail.
        assert "--- summary (Sandbox) ---" in lines
        marker = lines.index("--- summary (Sandbox) ---")
        assert "--- end error output ---" not in lines[marker:]
        tail = "\n".join(lines[marker:])
        # Both targets' verdicts and the passing target's cycle count survive
        # in the tail-truncation-protected block.
        assert "[sim] lite: PASS (1/1 tests" in tail
        assert re.search(r"^  lite\s+PASS\s+4,242\s", tail, re.MULTILINE)
        assert "[sim] full: FAIL (0/1 tests" in tail

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
            with flow.publication_resources:
                flow._run()

        report = capsys.readouterr().out
        # error_tail keeps the last 50 stdout lines; the report shows only the
        # last 30 of those, with an explicit marker for the 20 omitted ones.
        assert "... (20 lines omitted, see run.log)" in report
        assert "noise-079" in report  # tail of the excerpt kept
        assert "noise-051" not in report  # older lines dropped from the report

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

    def test_headline_prints_numbered_run_log_path_on_failure(self, tmp_path: Path, capsys):
        """The failure headline cites immutable invocation evidence."""
        from booley.flows.sim.flow import TargetResult, TestResult

        flow = _make_flow(tmp_path, config="lite")
        build_root = (
            tmp_path / ".booley" / ".runtime" / "edalize" / "sim" / "lite" / "demo_0" / "lite"
        )
        flow._record_run_log_dir("lite", build_root)
        write_run_log(build_root, "[SIM_RESULT] FAILED\n")
        pointer = "reports/sim/1/artifacts/candidate/sim_lite/tests/smoke/run.log"
        tr = TargetResult(
            target="lite",
            passed=False,
            elapsed_s=1.0,
            tests=[
                TestResult(
                    name="smoke",
                    passed=False,
                    elapsed_s=1.0,
                    run_log_path=pointer,
                )
            ],
        )

        report = flow._format_summary([tr], [], False)

        assert f"log: {pointer}" in report
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

        with flow.publication_resources:
            flow._write_target_report(tr)

        report = json.loads(
            (tmp_path / "reports" / "sim/1/targets/sim/simulation.json").read_text()
        )
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
        from booley.flows.sim.flow import TargetResult

        (tmp_path / "sim_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")
        flow = _make_flow(tmp_path, config="sim", seed_core=False)
        flow._test_names_map = {}
        with flow.publication_resources:
            invocation = flow.reserve_invocation_dir()
            assert invocation is not None
            build_root = invocation / "artifacts/candidate/sim_sim/tests/smoke"
            build_root.mkdir(parents=True)
            log = write_run_log(build_root, "simulator output\n")
            result = build_root / "result.json"
            result.write_text("{}", encoding="utf-8")
            trace = build_root / "trace.fst"
            trace.write_text("fst", encoding="utf-8")

            flow._write_target_report(
                TargetResult(
                    target="sim",
                    passed=False,
                    elapsed_s=1.0,
                    artifacts=(
                        SimulationArtifactEvidence("run_log", str(log), log.stat().st_size, ()),
                        SimulationArtifactEvidence(
                            "result", str(result), result.stat().st_size, ()
                        ),
                        SimulationArtifactEvidence("trace", str(trace), trace.stat().st_size, ()),
                    ),
                )
            )

            artifacts = json.loads(
                (tmp_path / "reports" / "sim/1/targets/sim/simulation.json").read_text()
            )["artifacts"]
            prefix = "reports/sim/1/artifacts/candidate/sim_sim/tests/smoke"
            assert artifacts["log"] == f"{prefix}/run.log"
            assert artifacts["result"] == f"{prefix}/result.json"
            assert artifacts["trace"] == f"{prefix}/trace.fst"
            assert artifacts["report"] == "reports/sim/1/targets/sim/simulation.json"
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
        with flow.publication_resources:
            flow._write_target_report(TargetResult(target="sim", passed=False, elapsed_s=1.0))

        artifacts = json.loads(
            (tmp_path / "reports" / "sim/1/targets/sim/simulation.json").read_text()
        )["artifacts"]
        assert set(artifacts) == {"report"}, "only this run's own report may be cited"
        assert artifacts["report"] == "reports/sim/1/targets/sim/simulation.json"

    def test_trace_pointer_follows_a_custom_dump_path(self, tmp_path: Path):
        """A project's own ``trace_files`` dump path (F-22) still gets cited —
        ``trace.fst`` is the conventional name, not the only one."""
        from booley.flows.sim.flow import TargetResult

        (tmp_path / "sim_demo.core").write_text(_SIM_CORE_TEXT, encoding="utf-8")
        flow = _make_flow(tmp_path, config="sim", seed_core=False)
        flow._test_names_map = {}
        build_root = tmp_path / "build"
        build_root.mkdir()
        custom = build_root / "waves" / "dump.fst"
        custom.parent.mkdir()
        custom.write_text("fst", encoding="utf-8")

        with flow.publication_resources:
            flow._write_target_report(
                TargetResult(
                    target="sim",
                    passed=True,
                    elapsed_s=1.0,
                    artifacts=(
                        SimulationArtifactEvidence(
                            "trace", str(custom), custom.stat().st_size, ("smoke",)
                        ),
                    ),
                )
            )

        artifacts = json.loads(
            (tmp_path / "reports" / "sim/1/targets/sim/simulation.json").read_text()
        )["artifacts"]
        assert artifacts["trace"] == "build/waves/dump.fst"

    def test_report_omits_context_keys_when_uncomposable(self, tmp_path: Path):
        """Best-effort contract: an unauthored Target (no .core) yields a
        report WITHOUT the context keys — never a failed Flow."""
        from booley.flows.sim.flow import TargetResult

        flow = _make_flow(tmp_path, config="ghost", seed_core=False)
        flow._test_names_map = {}
        tr = TargetResult(target="ghost", passed=False, elapsed_s=0.1)

        with flow.publication_resources:
            flow._write_target_report(tr)

        report = json.loads(
            (tmp_path / "reports" / "sim/1/targets/ghost/simulation.json").read_text()
        )
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
        # Counts match the .core partition: 1 rtl + 3 tb-tagged files.
        assert "  fileset: 4 files (3 tb)" in report

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

    build_root = tmp_path / ".booley" / ".runtime" / "edalize" / "sim" / "lite"
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

    def test_missing_verilator_in_build_exits_2(self, tmp_path: Path, monkeypatch):
        # What the sandbox script actually prints with verilator off PATH.
        build = SubprocessResult(
            returncode=127,
            stdout=(
                "/bin/sh: 1: verilator: not found\n"
                "make: *** [Makefile:9: Vtop] Error 127\n"
                "ERROR: Verilator elaboration failed (rc=127)\n"
            ),
        )
        run = run_campaign_sim(
            tmp_path, monkeypatch, {"lite": ["t1"]}, PASS_PROCESS, target="lite", build=build
        )
        outcome = run.result.outcome
        assert run.result.exit_code == EXIT_ERROR
        assert outcome.detail["eda_tool_error"] == "build_infrastructure"
        assert "missing tool: verilator" in outcome.report_text
        assert run.launched == []

    def test_live_sim_echoing_command_not_found_is_not_hijacked(self, tmp_path: Path, monkeypatch):
        """A running TB's own $system noise must not become a Flow error."""
        process = SubprocessResult(
            returncode=1,
            stdout=(
                "sh: 1: helper_script: not found\n"
                '[SIM_RESULT] FAILED\n[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n'
            ),
        )
        run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["t1"]}, process, target="lite")
        assert run.result.exit_code == EXIT_FAILURE
        assert "eda_tool_error" not in run.result.outcome.detail


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

        with patch.object(SimulateFlow, "_execute", _execute), flow.publication_resources:
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        rel = "reports/sim/1/artifacts/candidate/sim_lite/tests/lite/trace/dump.fst"
        assert f"trace: {rel} (4.0 KB, 42 signals, scope tb.dut, 900 ticks)" in result.report_text
        report = json.loads((tmp_path / "reports/sim/1/targets/lite/simulation.json").read_text())
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
        with flow.publication_resources:
            result = flow._run()
        assert "trace:" not in result.report_text


class TestSubSecondDurations:
    """F-39: 10-20ms cocotb tests all rendered as an identical `0.0s`."""

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
                    inconclusive_reason=(
                        "trace requested but no fresh .fst store or convertible .vcd "
                        "was produced; declare it in [flows.sim].trace_files"
                    ),
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


@pytest.mark.parametrize("met", [True, False, None])
def test_standalone_elab_only_final_headline(tmp_path, met):
    from booley.flows.sim.build import BuildOutcome
    from booley.flows.sim.flow import ElabOnlyTargetResult
    from booley.runtime.endpoint_execution import EndpointOutcome
    from tests.flows.report_headline import final_report_omits_headline

    flow = _make_flow(tmp_path, config="lite", extra_args=["--mode", "elab-only"])
    flow.args.state_file = None
    flow.read_state()
    build = BuildOutcome(
        True,
        None if met is None else ("pass" if met else "fail"),
        "infrastructure" if met is None else (None if met else "design"),
    )
    flow._record_elab_only_criterion(ElabOnlyTargetResult("lite", outcome=build))
    result = EndpointOutcome(exit_code=2 if met is None else (0 if met else 1))
    with flow.publication_resources:
        report = final_report_omits_headline(flow, result)
    assert report["exit_code"] == result.exit_code
    if met is None:
        assert "elab_pass_lite" not in flow.state.criteria
    else:
        assert flow.state.criteria["elab_pass_lite"].met is met
    assert flow.state._file_path is None


def test_legacy_report_preserves_per_target_tool_identity(tmp_path):
    import json

    from booley.flows.sim.flow import TargetResult

    flow = _make_flow(tmp_path, config="lite,full")
    results = [
        TargetResult("lite", eda_tool="Verilator", passed=True),
        TargetResult("full", eda_tool="icarus", passed=True),
    ]
    with flow.publication_resources:
        outcome = flow._legacy_endpoint_outcome(["lite", "full"], results, [], True, 0, 0)
        report = json.loads(flow.write_report(outcome).read_bytes())
    assert report.get("eda_tools") == {"lite": "Verilator", "full": "icarus"}
    assert "eda_tool" not in report


def test_target_report_write_survives_transient_replace_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.flow import _atomic_write_json
    from tests.sharing_error_support import hold_replaces

    path = tmp_path / "targets" / "sim_a" / "simulation.json"
    path.parent.mkdir(parents=True)
    attempts = hold_replaces(monkeypatch, {path: 1})
    _atomic_write_json(path, {"passed": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"passed": True}
    assert attempts[path] == 2


def test_target_report_write_retries_before_raising_lasting_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.flow import _atomic_write_json
    from tests.sharing_error_support import hold_replaces

    path = tmp_path / "targets" / "sim_a" / "simulation.json"
    path.parent.mkdir(parents=True)
    attempts = hold_replaces(monkeypatch, {path: 100})
    with pytest.raises(PermissionError):
        _atomic_write_json(path, {"passed": True})
    assert attempts[path] > 1


def test_run_log_freshness_warns_on_lasting_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.sharing_error_support import hold_opens

    flow = _make_flow(tmp_path, config="lite")
    build_root = tmp_path / "build" / "lite"
    build_root.mkdir(parents=True)
    flow._record_run_log_dir("lite", build_root)
    write_run_log(build_root, "[SIM_RESULT] PASSED\n")
    hold_opens(monkeypatch, {build_root / "run.log": 100})
    with caplog.at_level("WARNING"):
        assert flow._run_log_is_fresh("lite") is False
    assert any("lite" in record.getMessage() for record in caplog.records)
    assert flow._run_log_pointer("lite") is None
