"""Tests for AsicSynthesizeFlow — parsing, delta computation, baseline flow, dry-run."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from booley.core.boundary import BoundaryError
from booley.dev_support.criteria import BASELINE_TARGET_PARAM, TargetPair
from booley.dev_support.development_state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.clock_timing import ClockTiming, make_clock_timing
from booley.flows.synth.flow import (
    KGE_DIVISOR,
    AsicSynthesizeFlow,
    SynthMetrics,
    _aggregate_detail,
    _build_report_dict,
    _detect_critical_conditions,
    _is_io_bound_critical,
    _parse_area,
    _parse_per_clock_sta,
    _parse_physical_area,
    _parse_process_count,
    _parse_reg2reg_fmax,
    _parse_reg2reg_slack,
    _parse_synth_output,
    _parse_wire_count,
    _parse_worst_slack,
    _synth_target_warnings,
    _vlogdefine_args,
    _vlogparam_args,
    _worst_critical_path_ps,
    synth_target_report_slug,
)
from booley.flows.synth.recipe import BASELINE_REF_PARAM
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.yosys import syn_make
from booley.yosys.syn_core import StaTimingConfig


def test_report_artifact_snapshot_is_immutable(tmp_path: Path) -> None:
    flow = object.__new__(AsicSynthesizeFlow)
    flow._args = SimpleNamespace(work_dir=tmp_path)
    shared = tmp_path / "shared"
    timing = shared / "timing"
    timing.mkdir(parents=True)
    log = shared / "run.log"
    log.write_text("first log\n", encoding="utf-8")
    (timing / "slack.rpt").write_text("first timing\n", encoding="utf-8")
    metrics = SynthMetrics(log_path="shared/run.log", dirs={"timing": "shared/timing"})

    artifacts = flow._snapshot_report_artifacts(tmp_path / "reports", "demo", metrics)
    log.write_text("second log\n", encoding="utf-8")
    (timing / "slack.rpt").write_text("second timing\n", encoding="utf-8")

    assert (tmp_path / artifacts["log"]).read_text(encoding="utf-8") == "first log\n"
    copied_timing = tmp_path / artifacts["dirs"]["timing"] / "slack.rpt"
    assert copied_timing.read_text(encoding="utf-8") == "first timing\n"


@pytest.fixture(autouse=True)
def _adr0039_lenient_selection(monkeypatch):
    """Pre-0039 pass-through target selection for these layer-focused tests.

    ADR 0039 made resolve_target_selection validate every token against a
    real .core surface. These unit tests mock the build/run layers and name
    ad-hoc targets (lite/full/...) without authoring cores, so give them the
    old pass-through selection — the .core-mandatory behavior itself is
    pinned in test_fusesoc_registry.py (test_no_core_rejects_any_token) and
    the .core-authoring integration tests.
    """
    from booley.fusesoc import fusesoc_registry

    def _lenient(target_arg, project_root):
        return [c.strip() for c in (target_arg or "").split(",") if c.strip()]

    monkeypatch.setattr(fusesoc_registry, "resolve_target_selection", _lenient)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_synth_target_report_slug_is_safe_and_collision_resistant():
    first = synth_target_report_slug("vendor:lib:core#asic")
    second = synth_target_report_slug("vendor_lib_core_asic")

    assert re.fullmatch(r"[A-Za-z0-9_.-]+", first)
    assert first != second
    assert second == "vendor_lib_core_asic"


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create and return a fresh state file, set env vars."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "test-ticket"
    st.save()
    monkeypatch.setenv("BOOLEY_SLUG", "test-ticket")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))
    return sf


@pytest.fixture()
def flow_and_state(state_file: Path, tmp_path: Path):
    """Return (Flow, state_file) with common args parsed and state loaded."""
    flow = AsicSynthesizeFlow()
    flow.parse_args(
        [
            "--target",
            "lite",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    flow.read_state()
    return flow, state_file


def test_relative_ticket_criterion_auto_applies_pinned_baseline(
    flow_and_state, tmp_path: Path
) -> None:
    flow, _ = flow_and_state
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "rtl.v").write_text("module rtl; endmodule\n", encoding="utf-8")
    subprocess.run(["git", "add", "rtl.v"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    flow.state.init_criteria(
        {"synthesis_ok_lite": True},
        criterion_params={"synthesis_ok_lite": {BASELINE_REF_PARAM: base_sha}},
    )

    assert flow._apply_ticket_baseline(["lite"]) is None
    assert flow.args.baseline == base_sha


def _write_execution_config(tmp_path: Path, body: str) -> None:
    """Write a ``[flows.synth]`` booley.toml with *body* lines."""
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir(exist_ok=True)
    (project_dir / "booley.toml").write_text(
        f"[flows.synth]\n{body}",
        encoding="utf-8",
    )


# A minimal synth `.core` for the real-fusesoc resolution e2e. The `arch`
# flow_option is plumbing the edalize yosys backend's configure() requires; it
# is ignored — Booley reruns yosys its own way (the command-gen exception), so
# FuseSoC is used only to resolve sources/top/params. The header is an
# `is_include_file` (surfaced as an include dir, not a source); the TB file is
# `tb`-tagged and excluded from synthesis.
_SYN_CORE_TEXT = """\
CAPI=2:
name: ::syn_demo:0
description: asic_synthesize slice fixture
filesets:
  rtl:
    files:
      - rtl/include/defs.svh: {file_type: systemVerilogSource, is_include_file: true}
      - rtl/pkg.sv: {file_type: systemVerilogSource}
      - rtl/dut.sv: {file_type: systemVerilogSource}
    file_type: systemVerilogSource
  tb:
    files:
      - tb/tb_dut.sv: {file_type: systemVerilogSource}
    tags: [tb]
  constraints:
    files:
      - constraints/dut.sdc: {file_type: SDC}
parameters:
  WIDTH: {datatype: int, default: 8, paramtype: vlogparam}
  SYNTHESIS: {datatype: bool, paramtype: vlogdefine, default: true}
targets:
  default:
    filesets: [rtl]
  syn:
    default_tool: yosys
    flow: generic
    flow_options: {tool: yosys, arch: xilinx}
    filesets: [rtl, tb, constraints]
    parameters: [WIDTH, SYNTHESIS]
    toplevel: dut
"""


def _fake_synth_resolved(
    work_dir: Path,
    *,
    config: str = "lite",
    toplevel: str = "dut",
) -> fusesoc_registry.ResolvedTarget:
    """A ResolvedTarget shaped like a real synth EDAM, under asic_synthesize's work root."""
    build_root = (
        Path(work_dir)
        / ".booley_project"
        / ".runtime"
        / "edalize"
        / "synth"
        / config
        / "syn_demo_0"
        / "syn"
    )
    files = (
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/include/defs.svh",
            file_type="systemVerilogSource",
            is_include=True,
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/pkg.sv",
            file_type="systemVerilogSource",
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/dut.sv",
            file_type="systemVerilogSource",
        ),
        # A file_type:SDC fileset is the post-ADR-0029 norm for a synth Target
        # and (ADR 0031) the thing whose absence is now a hard error — so the
        # default fake carries one, exercising the --sta-sdc forwarding path.
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/constraints/dut.sdc",
            file_type="SDC",
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/tb/tb_dut.sv",
            file_type="systemVerilogSource",
            tags=("tb",),
        ),
    )
    params = {
        "WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 8},
        "SYNTHESIS": {"datatype": "bool", "paramtype": "vlogdefine", "default": True},
    }
    # Many boundary tests use booley.toml as a compact way to author the fake
    # Target recipe. Production never reads these keys there; Doctor has
    # separate migration tests that reject them in booley.toml.
    recipe: dict = {
        "tool": "yosys",
        "synth_mode": "logical",
    }  # upstream FuseSoC flow_options fields
    config_path = Path(work_dir) / ".booley_project" / "booley.toml"
    if config_path.is_file():
        with config_path.open("rb") as config_file:
            flow_cfg = tomllib.load(config_file).get("flows", {}).get("synth", {})
        for key in (
            "flatten",
            "frontend",
            "advanced_settings_openroad",
            "advanced_settings_yosys",
            "openroad",
            "ppa_profile",
            "slang_options",
            "synth_mode",
            "timing_engine",
            "yosys",
        ):
            if key in flow_cfg:
                recipe[key] = flow_cfg[key]
    return fusesoc_registry.ResolvedTarget(
        name=config,
        vlnv="::syn_demo:0",
        toplevel=toplevel,
        eda_tool="yosys",
        flow_options=recipe,
        files=files,
        parameters=params,
        build_root=build_root,
        edam_path=build_root / "syn_demo_0.eda.yml",
    )


def _with_synth_mode(
    resolved: fusesoc_registry.ResolvedTarget,
    mode: str,
) -> fusesoc_registry.ResolvedTarget:
    """Return a fake Target with an explicit synthesis mode."""
    return dataclasses.replace(
        resolved,
        flow_options={**resolved.flow_options, "synth_mode": mode},
    )


def _write_syn_demo_project(work_dir: Path) -> None:
    """Materialize the syn_demo fixture on disk: RTL + include + tb + SDC + core."""
    (work_dir / "rtl" / "include").mkdir(parents=True)
    (work_dir / "tb").mkdir(parents=True)
    (work_dir / "rtl" / "include" / "defs.svh").write_text(
        "`define FOO 1\n",
        encoding="utf-8",
    )
    (work_dir / "rtl" / "pkg.sv").write_text(
        "package pkg; endpackage\n",
        encoding="utf-8",
    )
    (work_dir / "rtl" / "dut.sv").write_text(
        "module dut #(parameter WIDTH=8)(input logic clk); endmodule\n",
        encoding="utf-8",
    )
    (work_dir / "tb" / "tb_dut.sv").write_text(
        "module tb_dut; dut d(.clk(1'b0)); endmodule\n",
        encoding="utf-8",
    )
    (work_dir / "constraints").mkdir()
    (work_dir / "constraints" / "dut.sdc").write_text(
        "create_clock -name clk -period 4.0 [get_ports clk]\n",
        encoding="utf-8",
    )
    (work_dir / "syn_demo.core").write_text(_SYN_CORE_TEXT, encoding="utf-8")


# Captured before the autouse fixture below patches the attribute, so the
# real-fusesoc e2e can reach the genuine resolver.
_REAL_RESOLVE = fusesoc_registry.resolve_target


@pytest.fixture(autouse=True)
def _stub_fusesoc_resolution(tmp_path: Path):
    """Default every test's FuseSoC resolution to a fake synth EDAM.

    The execution-path tests (``TestSingleConfigRun`` etc.) mock only
    ``_execute``; without this, ``_build_synth_cmd`` would shell out to a real
    ``fusesoc run --setup`` against a project with no ``.core`` and fail. Tests
    that exercise resolution itself re-patch ``resolve_target`` inside a ``with``
    block — that inner patch takes precedence for its duration; the e2e uses
    ``_REAL_RESOLVE``.
    """
    with patch.object(
        fusesoc_registry,
        "resolve_target",
        side_effect=lambda target="lite", **k: _fake_synth_resolved(tmp_path, config=target),
    ):
        yield


def _stub_plan(
    work_dir: Path,
    target: str,
    *,
    mode: str = "logical",
    design: str = "dut",
) -> syn_make.SynthPlan:
    """A minimal configured SynthPlan under *target*'s real build dir."""
    build_dir = (
        Path(work_dir) / ".booley_project" / ".runtime" / "edalize" / "synth" / target / "synth"
    )
    build_dir.mkdir(parents=True, exist_ok=True)
    spec = syn_make.SynthSpec(
        design_name=design,
        sources=(),
        inc_dirs=(),
        defines=(),
        params={},
        liberty=Path("/opt/pdk/fake.lib"),
        liberty_found=True,
        flatten=True,
        abc_recipe=None,
        frontend="sv2v",
        timing=StaTimingConfig(
            mode=mode,
            clock=None,
            period_ps=4000.0,
            input_delay_pct=30.0,
            output_delay_pct=70.0,
        ),
    )
    return syn_make.SynthPlan(build_dir=build_dir, spec=spec)


@pytest.fixture(autouse=True)
def _stub_configure_synth():
    """Default the configure half (ADR 0037 §8) to a canned plan.

    The execution-path tests mock the boundary executor and feed it synthetic
    output; real script rendering would fail because the fake resolved sources
    never exist on disk. The spec argv (``_build_synth_cmd``) still runs for
    real — only the render step is stubbed. Tests that exercise rendering
    itself call ``run_yosys_syn.resolve_spec`` / ``syn_make`` directly (see
    tests/yosys/test_syn_make.py and the golden Makefile snapshots).
    """

    def fake_configure(self, target, cmd):
        mode = cmd[cmd.index("--synth-mode") + 1]
        return _stub_plan(Path(self.args.work_dir), target, mode=mode)

    with patch.object(AsicSynthesizeFlow, "_configure_synth", fake_configure):
        yield


# ===========================================================================
# Area parsing
# ===========================================================================


class TestParseArea:
    """Test _parse_area with various stat output formats."""

    def test_top_module_line(self):
        output = "Chip area for top module '\\design_top': 52480.00\n"
        area, cells = _parse_area(output)
        assert area == 52480.0
        assert cells is None

    def test_cell_count(self):
        output = "Number of cells: 12345\nChip area for top module '\\m': 1000.0\n"
        area, cells = _parse_area(output)
        assert area == 1000.0
        assert cells == 12345

    def test_fallback_to_last_chip_area(self):
        output = "Chip area for module '\\sub': 100.0\nChip area for module '\\top': 500.0\n"
        area, _cells = _parse_area(output)
        assert area == 500.0

    def test_no_match(self):
        area, cells = _parse_area("random output with no stats")
        assert area is None
        assert cells is None

    def test_empty_string(self):
        area, cells = _parse_area("")
        assert area is None
        assert cells is None

    def test_integer_area(self):
        output = "Chip area for top module '\\x': 12345\n"
        area, _ = _parse_area(output)
        assert area == 12345.0

    def test_both_values_present(self):
        output = "Number of cells: 999\nChip area for top module '\\t': 6400.0\n"
        area, cells = _parse_area(output)
        assert area == 6400.0
        assert cells == 999


# ===========================================================================
# KGe conversion
# ===========================================================================


class TestKGeConversion:
    def test_exact(self):
        assert pytest.approx(798.0) == KGE_DIVISOR
        assert 798.0 / KGE_DIVISOR == 1.0

    def test_typical_value(self):
        assert pytest.approx(65.764, rel=1e-3) == 52480.0 / KGE_DIVISOR

    def test_zero(self):
        assert 0.0 / KGE_DIVISOR == 0.0


# ===========================================================================
# Per-clock STA parsing (critical-path / Fmax are per-clock now)
# ===========================================================================


def _perclock_marker(name: str, period_ns, wns_ns, whs_ns) -> str:
    """Render one canonical STA_PERCLOCK marker line for tests.

    ``None`` is emitted as the ``NA`` sentinel the parser maps back to None.
    """

    def _tok(v):
        return "NA" if v is None else f"{v:.6f}"

    return (
        f"STA_PERCLOCK: name={name} period_ns={period_ns:.6f} "
        f"wns_ns={_tok(wns_ns)} whs_ns={_tok(whs_ns)}\n"
    )


class TestParsePerClockSta:
    def test_derives_critical_path_and_fmax(self):
        # period 2.0 ns, wns +0.75 ns -> crit path (2.0-0.75)*1000 = 1250 ps,
        # Fmax 1e6/1250 = 800 MHz.
        output = _perclock_marker("clk", 2.0, 0.75, 0.1)
        per_clock = _parse_per_clock_sta(output)
        assert set(per_clock) == {"clk"}
        ct = per_clock["clk"]
        assert isinstance(ct, ClockTiming)
        assert ct.clock == "clk"
        assert ct.period_ns == pytest.approx(2.0)
        assert ct.wns_ns == pytest.approx(0.75)
        assert ct.whs_ns == pytest.approx(0.1)
        assert ct.critical_path_ps == pytest.approx(1250.0)
        assert ct.fmax_mhz == pytest.approx(800.0)

    def test_na_slack_leaves_derived_none(self):
        # No setup path for the clock -> wns NA -> critical path/Fmax undefined.
        output = _perclock_marker("clk", 2.0, None, None)
        ct = _parse_per_clock_sta(output)["clk"]
        assert ct.wns_ns is None
        assert ct.whs_ns is None
        assert ct.critical_path_ps is None
        assert ct.fmax_mhz is None

    def test_multi_clock_yields_one_entry_each(self):
        output = _perclock_marker("clk_a", 2.0, 0.75, 0.1) + _perclock_marker(
            "clk_b", 4.0, 1.0, 0.2
        )
        per_clock = _parse_per_clock_sta(output)
        assert set(per_clock) == {"clk_a", "clk_b"}
        # clk_b: (4.0-1.0)*1000 = 3000 ps -> 1e6/3000 = 333.33 MHz.
        assert per_clock["clk_b"].critical_path_ps == pytest.approx(3000.0)
        assert per_clock["clk_b"].fmax_mhz == pytest.approx(333.333, rel=1e-4)

    def test_no_marker_yields_empty(self):
        assert _parse_per_clock_sta("no timing info here") == {}


class TestParsePerClockRaw:
    """Raw marker parsing (syn_core.parse_perclock) as consumed by the Flow."""

    def test_na_becomes_none(self):
        from booley.yosys.syn_core import parse_perclock

        row = parse_perclock(_perclock_marker("clk", 2.0, None, 0.3))["clk"]
        assert row["period_ns"] == pytest.approx(2.0)
        assert row["wns_ns"] is None
        assert row["whs_ns"] == pytest.approx(0.3)

    def test_duplicate_clock_keeps_min_slack(self):
        from booley.yosys.syn_core import parse_perclock

        # Same clock reported twice (raw + re-emitted): the most pessimistic
        # (minimum) slack wins for both setup and hold.
        output = _perclock_marker("clk", 2.0, 0.75, 0.30) + _perclock_marker(
            "clk", 2.0, 0.20, 0.05
        )
        row = parse_perclock(output)["clk"]
        assert row["wns_ns"] == pytest.approx(0.20)
        assert row["whs_ns"] == pytest.approx(0.05)


# ===========================================================================
# Worst STA slack (timing VIOLATED detection)
# ===========================================================================


class TestParseWorstSlack:
    def test_positive_slack_met(self):
        assert _parse_worst_slack("STA_WORST_SLACK_NS: 0.250000\n") == 0.25

    def test_negative_slack_violated(self):
        assert _parse_worst_slack("STA_WORST_SLACK_NS: -1.375000\n") == -1.375

    def test_multiple_takes_most_pessimistic(self):
        output = "STA_WORST_SLACK_NS: 0.500000\nSTA_WORST_SLACK_NS: -0.200000\n"
        assert _parse_worst_slack(output) == -0.2

    def test_absent(self):
        assert _parse_worst_slack("STA_CRITICAL_PATH_PS: 1250.0\n") is None

    def test_populated_in_synth_metrics(self):
        output = (
            "Number of cells: 100\n"
            "Chip area for top module '\\top': 500.0\n"
            "STA_CRITICAL_PATH_PS: 1250.0\n"
            "STA_WORST_SLACK_NS: -0.750000\n"
        )
        metrics = _parse_synth_output(output, elapsed_s=1.0)
        assert metrics.wns_ns == -0.75


class TestParseReg2Reg:
    def test_slack_and_fmax_parsed(self):
        output = "STA_REG2REG_SLACK_NS: -0.400000\nSTA_REG2REG_FMAX_MHZ: 476.190\n"
        assert _parse_reg2reg_slack(output) == -0.4
        assert _parse_reg2reg_fmax(output) == 476.19

    def test_absent(self):
        # Overall path present but no reg2reg markers (combinational design).
        output = "STA_WORST_SLACK_NS: -1.0\nSTA_FMAX_MHZ: 200.0\n"
        assert _parse_reg2reg_slack(output) is None
        assert _parse_reg2reg_fmax(output) is None

    def test_populated_in_synth_metrics(self):
        output = (
            "Number of cells: 100\n"
            "Chip area for top module '\\top': 500.0\n"
            "STA_WORST_SLACK_NS: -2.000000\n"  # overall (I/O) path dominates
            "STA_REG2REG_SLACK_NS: -0.250000\n"  # true internal path
            "STA_REG2REG_FMAX_MHZ: 800.000\n"
        )
        metrics = _parse_synth_output(output, elapsed_s=1.0)
        assert metrics.wns_ns == -2.0
        assert metrics.reg2reg_slack_ns == -0.25
        assert metrics.reg2reg_fmax_mhz == 800.0


# ===========================================================================
# Mode-specific canonical area
# ===========================================================================


class TestModeSpecificArea:
    def test_marker(self):
        output = "OPENROAD_DESIGN_AREA_UM2: 1234.500\n"
        assert _parse_physical_area(output) == 1234.5

    def test_absent_when_logical_path(self):
        assert _parse_physical_area("Chip area for top module '\\top': 500.0\n") is None

    def test_physical_uses_openroad_area(self):
        output = (
            "Number of cells: 100\n"
            "Chip area for top module '\\top': 500.0\n"
            "STA_CRITICAL_PATH_PS: 1250.0\n"
            "OPENROAD_DESIGN_AREA_UM2: 640.250\n"
        )
        metrics = _parse_synth_output(output, elapsed_s=1.0, synth_mode="physical")
        assert metrics.area_um2 == 640.25
        assert metrics.area_source == "openroad_post_optimization"

    def test_logical_uses_yosys_area(self):
        output = "Number of cells: 100\nChip area for top module '\\top': 500.0\n"
        metrics = _parse_synth_output(output, elapsed_s=1.0, synth_mode="logical")
        assert metrics.area_um2 == 500.0
        assert metrics.area_source == "yosys_mapped"


# ===========================================================================
# Logical-mode ABC frequency estimate
# ===========================================================================


class TestLogicalEstimatedFmax:
    def test_uses_slowest_positive_mapped_partition(self):
        output = "YOSYS_ABC_LOGIC_DELAY_PS: 125.000\nYOSYS_ABC_LOGIC_DELAY_PS: 400.000\n"
        metrics = _parse_synth_output(output, elapsed_s=1.0, synth_mode="logical")
        assert metrics.estimated_fmax_mhz == pytest.approx(2500.0)

    def test_zero_or_missing_delay_has_no_estimate(self):
        output = "YOSYS_ABC_LOGIC_DELAY_PS: 0.000\n"
        metrics = _parse_synth_output(output, elapsed_s=1.0, synth_mode="logical")
        assert metrics.estimated_fmax_mhz is None

    def test_physical_mode_does_not_publish_abc_estimate(self):
        output = "YOSYS_ABC_LOGIC_DELAY_PS: 250.000\nOPENROAD_DESIGN_AREA_UM2: 15.0\n"
        metrics = _parse_synth_output(output, elapsed_s=1.0, synth_mode="physical")
        assert metrics.estimated_fmax_mhz is None


# ===========================================================================
# Critical condition detection
# ===========================================================================


class TestCriticalConditions:
    def test_latches(self):
        output = "Warning: found $dlatch in cell A\nWarning: found $dlatch in cell B\n"
        latches, loops, multi = _detect_critical_conditions(output)
        assert latches == 2
        assert loops == 0
        assert multi == 0

    def test_comb_loop(self):
        output = "ERROR: Combinational loop detected\n"
        _latches, loops, _multi = _detect_critical_conditions(output)
        assert loops == 1

    def test_multi_driven(self):
        output = "Warning: multi-driven net \\sig\n"
        _, _, multi = _detect_critical_conditions(output)
        assert multi == 1

    def test_all_conditions(self):
        output = "$dlatch\nCombinational loop\nmulti-driven\n"
        latches, loops, multi = _detect_critical_conditions(output)
        assert latches == 1
        assert loops == 1
        assert multi == 1

    def test_clean_output(self):
        latches, loops, multi = _detect_critical_conditions("synthesis completed OK")
        assert latches == 0
        assert loops == 0
        assert multi == 0

    def test_stat_tally_is_preferred_over_occurrence_counting(self):
        """F-19: the raw occurrence count folded in log noise.

        A `$dlatch` named in a techmap trace is not an extra inferred latch;
        counting it inflates the number that decides a FAIL.
        """
        output = (
            "Executing TECHMAP pass: mapping $dlatch cells\n"
            "Warning: found $dlatch in cell A\n"
            "Number of cells:                 42\n"
            "  $dlatch                          1\n"
            "  NAND2_X1                        41\n"
        )
        latches, _loops, _multi = _detect_critical_conditions(output)
        assert latches == 1

    def test_falls_back_to_occurrences_without_a_stat_tally(self):
        """A run that died before `stat` still reports; over-counting is safe."""
        output = "Warning: found $dlatch in cell A\nWarning: found $dlatch in cell B\n"
        latches, _loops, _multi = _detect_critical_conditions(output)
        assert latches == 2


class TestIntentionalLatches:
    """F-19: a standard-cell ICG is a deliberate `always_latch`.

    lowRISC's generic `prim_clock_gating` contains exactly one, so failing on
    any latch made a correct design unsynthesizable through Booley. Declaring
    the count keeps the gate meaningful without lying about the design.
    """

    def _metrics(self, latches: int, expected: int) -> SynthMetrics:
        return SynthMetrics(
            area_um2=500.0,
            cells=100,
            latches=latches,
            expected_latches=expected,
        )

    def test_undeclared_latch_is_still_critical(self):
        m = self._metrics(latches=1, expected=0)
        assert m.unexpected_latches == 1
        assert m.has_critical is True
        assert m.passed is False

    def test_declared_latch_does_not_fail_the_run(self):
        m = self._metrics(latches=1, expected=1)
        assert m.unexpected_latches == 0
        assert m.has_critical is False
        assert m.passed is True

    def test_one_more_than_declared_still_fails(self):
        """The gate must stay meaningful — this is an allowance, not a mute."""
        m = self._metrics(latches=2, expected=1)
        assert m.unexpected_latches == 1
        assert m.has_critical is True
        assert m.passed is False

    def test_raw_count_is_never_hidden(self):
        """Reporting always states what was actually inferred."""
        m = self._metrics(latches=1, expected=1)
        assert m.latches == 1

    def test_fewer_latches_than_declared_is_not_negative(self):
        m = self._metrics(latches=0, expected=2)
        assert m.unexpected_latches == 0
        assert m.has_critical is False

    def test_count_latches_prefers_stat_tally(self):
        from booley.flows.synth.flow import _count_latches

        out = "Printing statistics.\n     $_DLATCH_P_    3\nblah $dlatch blah\n"
        assert _count_latches(out) == 3

    def test_count_latches_stat_ran_with_no_dlatch_row_is_zero(self):
        """F-29 regression: transient $dlatch log chatter on a clean netlist.

        yosys-slang emits `$driver$…($dlatch)` helper cells that opt folds
        away; `stat` then prints no $_DLATCH row (it omits zero-instance cell
        types). The occurrence fallback counted 1680 such mentions and failed
        a latch-free ravenoc synthesis as CRITICAL.
        """
        from booley.flows.synth.flow import _count_latches

        chatter = "Setting constant 0-bit at position 0 on $driver$x.fifo_ff[1] ($dlatch)\n"
        out = chatter * 1680 + "Printing statistics.\n     $_DFF_PP0_   4160\n"
        assert _count_latches(out) == 0

    def test_count_latches_falls_back_when_no_stat_section(self):
        """A run that died before stat: over-counting stays the safe direction."""
        from booley.flows.synth.flow import _count_latches

        assert _count_latches("proc $dlatch here\nanother $dlatch\n") == 2

    def test_expected_latches_read_from_booley_toml(self, tmp_path):
        from booley.flows.synth.flow import _expected_latches

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            "[flows.synth]\nexpected_latches = 1\n", encoding="utf-8"
        )
        assert _expected_latches(tmp_path) == 1
        # Unconfigured -> 0, the strict historical behavior.
        assert _expected_latches(tmp_path / "nowhere") == 0

    @pytest.mark.parametrize(
        ("literal", "why"),
        [
            ("-1", "negative"),
            ('"one"', "string"),
            ("true", "bool"),
            ("1.5", "float"),
        ],
    )
    def test_malformed_value_does_not_widen_the_gate(self, tmp_path, literal, why):
        """A bad knob must fall back to strict (0), never to permissive."""
        from booley.flows.synth.flow import _expected_latches

        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            f"[flows.synth]\nexpected_latches = {literal}\n", encoding="utf-8"
        )
        assert _expected_latches(tmp_path) == 0, why


# ===========================================================================
# Full output parsing
# ===========================================================================


class TestParseSynthOutput:
    def test_complete_output(self):
        output = (
            "Chip area for top module '\\t': 52480.0\n"
            "Number of cells: 12345\n"
            + _perclock_marker("clk", 2.0, 0.75, 0.1)
            + "STA_REPORT: /tmp/reports/timing/overall.rpt\n"
        )
        m = _parse_synth_output(output, 4.2)
        assert m.area_um2 == 52480.0
        assert m.area_kge == pytest.approx(52480.0 / KGE_DIVISOR)
        assert m.cells == 12345
        # Critical path / Fmax are per-clock now (one entry per create_clock).
        assert set(m.per_clock) == {"clk"}
        assert m.per_clock["clk"].critical_path_ps == pytest.approx(1250.0)
        assert m.per_clock["clk"].fmax_mhz == pytest.approx(800.0)
        assert m.elapsed_s == 4.2
        assert not m.has_critical

    def test_sta_report_markers_do_not_become_pointers(self):
        """The STA markers are no longer scraped into a per-file map.

        Their reports live in the timing dir the ``artifacts.dirs`` block
        names, so the reader lists that dir instead of trusting a pointer whose
        filename is hardcoded in Python (``overall.rpt`` vs ``pre_repair.rpt``
        vs ``reg2reg.rpt`` depend on the engine and on whether repair ran).
        """
        m = _parse_synth_output(
            "Number of cells: 10\n"
            "STA_REPORT: /tmp/reports/timing/overall.rpt\n"
            "STA_REG2REG_REPORT: /tmp/reports/timing/reg2reg.rpt\n",
            1.0,
        )
        assert not hasattr(m, "reports")
        assert m.cells == 10  # the numbers still parse

    def test_multi_clock_output(self):
        # Two create_clock domains -> two per_clock entries, each with its own
        # derived critical path / Fmax.
        output = (
            "Chip area for top module '\\t': 52480.0\n"
            "Number of cells: 12345\n"
            + _perclock_marker("clk_core", 2.0, 0.75, 0.1)
            + _perclock_marker("clk_io", 5.0, -1.0, 0.2)
        )
        m = _parse_synth_output(output, 1.0)
        assert set(m.per_clock) == {"clk_core", "clk_io"}
        assert m.per_clock["clk_core"].critical_path_ps == pytest.approx(1250.0)
        assert m.per_clock["clk_core"].fmax_mhz == pytest.approx(800.0)
        # clk_io violated: (5.0 - (-1.0))*1000 = 6000 ps -> 1e6/6000 = 166.67 MHz.
        assert m.per_clock["clk_io"].critical_path_ps == pytest.approx(6000.0)
        assert m.per_clock["clk_io"].fmax_mhz == pytest.approx(166.667, rel=1e-4)

    def test_with_latches(self):
        output = "$dlatch cell\nChip area for top module '\\t': 100.0\n"
        m = _parse_synth_output(output, 1.0)
        assert m.latches == 1
        assert m.has_critical is True


# ===========================================================================
# SynthMetrics
# ===========================================================================


class TestSynthMetrics:
    def test_has_critical_false(self):
        m = SynthMetrics()
        assert not m.has_critical

    def test_has_critical_latches(self):
        m = SynthMetrics(latches=1)
        assert m.has_critical

    def test_has_critical_comb_loops(self):
        m = SynthMetrics(comb_loops=1)
        assert m.has_critical

    def test_has_critical_multi_driven(self):
        m = SynthMetrics(multi_driven=2)
        assert m.has_critical


# ===========================================================================
# Delta computation
# ===========================================================================


class TestDeltaComputation:
    def test_positive_delta(self):
        pct = AsicSynthesizeFlow._compute_delta_pct(10.0, 8.0)
        assert pct == pytest.approx(25.0)

    def test_negative_delta(self):
        pct = AsicSynthesizeFlow._compute_delta_pct(8.0, 10.0)
        assert pct == pytest.approx(-20.0)

    def test_zero_baseline(self):
        assert AsicSynthesizeFlow._compute_delta_pct(10.0, 0.0) is None

    def test_none_current(self):
        assert AsicSynthesizeFlow._compute_delta_pct(None, 8.0) is None

    def test_none_baseline(self):
        assert AsicSynthesizeFlow._compute_delta_pct(8.0, None) is None

    def test_equal(self):
        pct = AsicSynthesizeFlow._compute_delta_pct(5.0, 5.0)
        assert pct == pytest.approx(0.0)


# ===========================================================================
# Formatting helpers
# ===========================================================================


class TestFormatting:
    def test_fmt_area_with_value(self):
        assert "KGe" in AsicSynthesizeFlow._fmt_area(8.2)
        assert "8.2" in AsicSynthesizeFlow._fmt_area(8.2)

    def test_fmt_area_none(self):
        assert "--" in AsicSynthesizeFlow._fmt_area(None)

    def test_fmt_timing_with_value(self):
        s = AsicSynthesizeFlow._fmt_timing(1250.0)
        assert "1,250" in s
        assert "ps" in s

    def test_fmt_timing_none(self):
        assert "--" in AsicSynthesizeFlow._fmt_timing(None)

    def test_fmt_delta_positive(self):
        s = AsicSynthesizeFlow._fmt_delta(2.0)
        assert "+2.0%" in s

    def test_fmt_delta_negative(self):
        s = AsicSynthesizeFlow._fmt_delta(-1.5)
        assert "-1.5%" in s

    def test_fmt_delta_none(self):
        assert AsicSynthesizeFlow._fmt_delta(None) == ""

    def test_qor_line_surfaces_numbers(self):
        cur = SynthMetrics(
            area_kge=8.2,
            cells=12345,
            per_clock={"clk": make_clock_timing("clk", 2.0, 0.75, 0.1)},
            wns_ns=0.25,
        )
        line = AsicSynthesizeFlow._format_qor_line("lite", cur)
        assert "QoR" in line
        assert "12,345 cells" in line
        assert "8.2 kGE" in line
        assert "1,250 ps" in line
        assert "Fmax 800 MHz" in line
        assert "setup slack +0.250 ns" in line
        assert "hold slack +0.100 ns" in line
        # Single-clock design: the timing-worst clock is shown untagged.
        assert "[clk]" not in line

    def test_qor_line_tags_worst_clock_when_multiclock(self):
        cur = SynthMetrics(
            cells=10,
            per_clock={
                "fast": make_clock_timing("fast", 1.0, 0.5, 0.1),  # 500 ps
                "slow": make_clock_timing("slow", 4.0, 0.0, 0.1),  # 4000 ps (worst)
            },
        )
        line = AsicSynthesizeFlow._format_qor_line("lite", cur)
        # The worst (lowest-Fmax) clock is 'slow'; its name tags the numbers.
        assert "4,000 ps [slow]" in line
        assert "Fmax 250 MHz [slow]" in line

    def test_qor_line_negative_slack_shows_sign(self):
        cur = SynthMetrics(cells=10, wns_ns=-1.5)
        assert "setup slack -1.500 ns" in AsicSynthesizeFlow._format_qor_line("k", cur)

    def test_qor_line_labels_logical_frequency_as_estimated(self):
        cur = SynthMetrics(cells=10, synth_mode="logical", estimated_fmax_mhz=2950.25)
        line = AsicSynthesizeFlow._format_qor_line("k", cur)
        assert "estimated Fmax 2950 MHz" in line


# ===========================================================================
# SETUP-28: I/O-bound critical-path diagnostic
# ===========================================================================


class TestIoBoundCritical:
    def test_flags_when_overall_worse_than_reg2reg(self):
        # Overall worst path (-2.0 ns) is strictly worse than the internal
        # reg->reg path (-0.25 ns) → the binding path touches a port.
        m = SynthMetrics(wns_ns=-2.0, reg2reg_slack_ns=-0.25)
        assert _is_io_bound_critical(m) is True

    def test_not_flagged_when_reg2reg_is_the_worst(self):
        # Overall == reg2reg → the reg->reg path IS the binding path.
        m = SynthMetrics(wns_ns=-0.25, reg2reg_slack_ns=-0.25)
        assert _is_io_bound_critical(m) is False

    def test_not_flagged_within_epsilon(self):
        m = SynthMetrics(wns_ns=-0.2505, reg2reg_slack_ns=-0.25)
        assert _is_io_bound_critical(m) is False

    def test_not_flagged_when_either_missing(self):
        assert _is_io_bound_critical(SynthMetrics(wns_ns=-2.0)) is False
        assert _is_io_bound_critical(SynthMetrics(reg2reg_slack_ns=-0.25)) is False

    def test_note_line_names_the_sdc_knob(self):
        cur = SynthMetrics(wns_ns=-2.0, reg2reg_slack_ns=-0.25)
        line = AsicSynthesizeFlow._format_io_bound_line("lite", cur)
        assert "I/O-bound" in line
        assert "period_ps won't move it" in line
        assert "sdc" in line
        assert "-2.000 ns" in line and "-0.250 ns" in line

    def test_report_dict_sets_flag(self):
        cur = SynthMetrics(
            cells=10,
            area_um2=500.0,
            wns_ns=-2.0,
            reg2reg_slack_ns=-0.25,
        )
        report = _build_report_dict(
            "synth",
            "lite",
            cur,
            None,
            None,
            AsicSynthesizeFlow._compute_delta_pct,
        )
        assert report.get("io_bound_critical") is True

    def test_report_dict_omits_flag_when_not_io_bound(self):
        cur = SynthMetrics(
            cells=10,
            area_um2=500.0,
            wns_ns=-0.25,
            reg2reg_slack_ns=-0.25,
        )
        report = _build_report_dict(
            "synth",
            "lite",
            cur,
            None,
            None,
            AsicSynthesizeFlow._compute_delta_pct,
        )
        assert "io_bound_critical" not in report


# ===========================================================================
# Dry-run mode
# ===========================================================================


class TestDryRun:
    def test_dry_run_prints_commands(self, state_file: Path, tmp_path: Path):
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full",
                "--work-dir",
                str(tmp_path),
                "--dry-run",
            ]
        )
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda target, **k: _fake_synth_resolved(tmp_path, config=target),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert "dry-run" in result.report_text
        # Per-config label keeps the config name visible (explicit-source mode has no -c).
        assert "(lite)" in result.report_text
        assert "(full)" in result.report_text

    def test_dry_run_with_flags(self, state_file: Path, tmp_path: Path):
        # Synthesis-recipe knobs live on the Flow config, not the CLI.
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            '[flows.synth]\nflatten = true\nsdc = true\nsynth_mode = "logical"\n',
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--work-dir",
                str(tmp_path),
                "--dry-run",
            ]
        )
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda target, **k: _fake_synth_resolved(tmp_path, config=target),
        ):
            result = flow._run()
        assert "--flatten" in result.report_text
        # Boolean ``sdc = true`` must NOT emit a bare "--sdc" (it takes a path
        # argument in run_yosys_syn and would crash argparse); SDC is already
        # run_yosys_syn's default, so no flag is expected.
        assert "--sdc" not in result.report_text
        assert "--synth-mode logical" in result.report_text


# ===========================================================================
# No configs
# ===========================================================================


class TestNoConfigs:
    def test_empty_config_returns_error(self, state_file: Path, tmp_path: Path):
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "",
                "--work-dir",
                str(tmp_path),
            ]
        )
        flow.read_state()
        result = flow._run()
        assert result.exit_code == EXIT_ERROR


# ===========================================================================
# Single-config run (mocked subprocess)
# ===========================================================================


class TestSingleConfigRun:
    """Test the main _run flow with a single config, mocking _execute."""

    def test_pass_no_baseline(self, flow_and_state, tmp_path: Path):
        flow, state_file = flow_and_state
        synth_output = (
            "Chip area for top module '\\design_top': 52480.0\n"
            "Number of cells: 12345\n"
            "STA_CRITICAL_PATH_PS: 1250.0\n"
            "STA_WORST_SLACK_NS: 0.500000\n"
        )
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout=synth_output,
                stderr="",
                duration_s=4.2,
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert "RESULT: PASS" in result.report_text
        assert "KGe" in result.report_text
        # QoR numbers must be surfaced on a passing run, not buried in files.
        assert "QoR" in result.report_text
        assert "12,345 cells" in result.report_text

        # Verify criterion was set
        st = DevelopmentState.load(state_file)
        assert st.is_met("synthesis_ok_lite")

    def test_physical_mode_without_timing_fails(self, flow_and_state):
        flow, _ = flow_and_state
        metrics = SynthMetrics(
            area_um2=52_480.0,
            area_source="openroad_post_optimization",
            area_kge=65.8,
            cells=12_345,
            elapsed_s=4.2,
            synth_mode="physical",
            timing_complete=False,
            ppa_complete=False,
        )
        result = flow._aggregate_results(["lite"], {"lite": metrics}, {}, None)

        assert result.exit_code == EXIT_FAILURE
        assert "RESULT: FAIL" in result.report_text
        assert "PARTIAL" in result.report_text

    def test_violated_slack_warns_but_stays_exit_success(self, flow_and_state):
        # Negative worst slack == timing VIOLATED. syn_core does not fail synth
        # on timing, so exit stays 0 — but it must WARN loudly, never be silent.
        flow, state_file = flow_and_state
        synth_output = (
            "Chip area for top module '\\design_top': 52480.0\n"
            "Number of cells: 12345\n"
            "STA_CRITICAL_PATH_PS: 5250.0\n"
            "STA_WORST_SLACK_NS: -1.250000\n"
        )
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout=synth_output,
                stderr="",
                duration_s=4.2,
            ),
        ):
            result = flow._run()

        # Structural pass keeps exit success and a met criterion...
        assert result.exit_code == EXIT_SUCCESS
        st = DevelopmentState.load(state_file)
        assert st.is_met("synthesis_ok_lite")
        # ...but the VIOLATED timing is loudly surfaced, not swallowed.
        assert "RESULT: WARN" in result.report_text
        assert "VIOLATED" in result.report_text
        assert "-1.250 ns" in result.report_text

    def test_violated_hold_slack_warns_but_stays_exit_success(self, flow_and_state):
        flow, state_file = flow_and_state
        synth_output = (
            "Chip area for top module '\\design_top': 52480.0\n"
            "Number of cells: 12345\n"
            "STA_WORST_SLACK_NS: 0.250000\n" + _perclock_marker("clk", 2.0, 0.25, -0.125)
        )
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout=synth_output,
                stderr="",
                duration_s=4.2,
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert DevelopmentState.load(state_file).is_met("synthesis_ok_lite")
        assert "RESULT: WARN" in result.report_text
        assert "hold slack -0.125 ns" in result.report_text

    def test_fail_on_latches(self, flow_and_state):
        flow, state_file = flow_and_state
        synth_output = (
            "Chip area for top module '\\t': 52480.0\n"
            "$dlatch cell A\n$dlatch cell B\n"
            "Combinational loop detected\n"
        )
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout=synth_output,
                stderr="",
                duration_s=3.0,
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        assert "FAIL" in result.report_text
        assert "CRITICAL" in result.report_text
        assert "2 latches" in result.report_text
        assert "1 comb loop" in result.report_text

        # Criterion should be unmet
        st = DevelopmentState.load(state_file)
        assert not st.is_met("synthesis_ok_lite")

    def test_report_json_written(self, flow_and_state, tmp_path: Path):
        flow, _ = flow_and_state
        # The STA marker must name a file that really exists: report pointers
        # are relativized against the work dir and dropped when missing, so a
        # fabricated path would (correctly) not survive into the report.
        timing_rpt = tmp_path / "reports" / "timing" / "overall.rpt"
        timing_rpt.parent.mkdir(parents=True, exist_ok=True)
        timing_rpt.write_text("slack 0.5\n", encoding="utf-8")
        synth_output = (
            "Chip area for top module '\\t': 6400.0\n"
            "Number of cells: 100\n"
            f"STA_REPORT: {timing_rpt}\n"
        )
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout=synth_output,
                stderr="",
                duration_s=2.0,
            ),
        ):
            flow._run()

        report_path = tmp_path / "reports" / "synth_lite.json"
        assert report_path.exists()
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["flow"] == "synth"
        assert data["target"] == "lite"
        assert data["passed"] is True
        assert data["area_kge"] == pytest.approx(6400.0 / KGE_DIVISOR)
        assert data["area_um2"] == 6400.0
        assert data["area_source"] == "yosys_mapped"
        assert "mapped_area_um2" not in data
        assert "post_opt_area_um2" not in data
        assert data["cells"] == 100
        assert data["conditions"]["has_critical"] is False
        assert data["implementation"]["schema_version"] == 1
        assert data["implementation"]["status"]["grade"] == "pass"
        assert data["implementation"]["metrics"]["area_um2"] == 6400.0
        # No per-file map: the artifacts block is the two entry points plus the
        # directories holding everything else.
        assert "reports" not in data
        artifacts = data["artifacts"]
        assert artifacts["report"] == "reports/synth_lite.json"
        assert artifacts["log"].endswith("run.log")
        assert artifacts["dirs"]["build"].endswith("synth/lite/synth")

    def test_artifact_dirs_omit_a_timing_dir_that_never_appeared(
        self, flow_and_state, tmp_path: Path
    ):
        """A run with no timing engine names the build dir and nothing else.

        The drop-what-is-absent rule applies to directories too, so an agent
        never gets sent to list a path that does not exist.
        """
        flow, _ = flow_and_state
        synth_output = "Chip area for top module '\\t': 6400.0\nNumber of cells: 100\n"
        build_dir = flow._synth_build_dir("lite")

        def _fake_make(*_args, **_kwargs):
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "yosys.log").write_text("x\n", encoding="utf-8")
            return SubprocessResult(returncode=0, stdout=synth_output, stderr="", duration_s=2.0)

        with patch.object(flow, "_execute", side_effect=_fake_make):
            flow._run()

        dirs = json.loads((tmp_path / "reports" / "synth_lite.json").read_text(encoding="utf-8"))[
            "artifacts"
        ]["dirs"]
        assert "build" in dirs
        assert "timing" not in dirs

    def test_build_dir_is_where_the_area_report_and_netlists_live(
        self, flow_and_state, tmp_path: Path
    ):
        """One directory pointer reaches every synth artifact.

        The area report (``stat_<design>.txt`` — where ``area_um2``/``cells``
        come from, with the per-cell-type breakdown), both netlists, the stage
        logs, the rendered ``synth.ys`` and the SDC fed to STA are all siblings
        in the build dir. Naming the dir means a flow that renames any of them
        cannot silently drop a pointer.
        """
        flow, _ = flow_and_state
        synth_output = "Chip area for top module '\\t': 6400.0\nNumber of cells: 100\n"
        build_dir = flow._synth_build_dir("lite")
        written = [
            "stat_soc_top.txt",
            "log_abc_soc_top.txt",
            "synth_soc_top.v",
            "sta_soc_top.v",
            "yosys.log",
            "sta.log",
            "synth.ys",
            "sta_constraints.sdc",
        ]

        def _fake_make(*_args, **_kwargs):
            build_dir.mkdir(parents=True, exist_ok=True)
            for name in written:
                (build_dir / name).write_text("x\n", encoding="utf-8")
            return SubprocessResult(returncode=0, stdout=synth_output, stderr="", duration_s=2.0)

        with patch.object(flow, "_execute", side_effect=_fake_make):
            flow._run()

        artifacts = json.loads(
            (tmp_path / "reports" / "synth_lite.json").read_text(encoding="utf-8")
        )["artifacts"]
        # Three entries total, whatever the run produced.
        assert set(artifacts) == {"report", "log", "dirs"}
        listing = {p.name for p in (tmp_path / artifacts["dirs"]["build"]).iterdir()}
        assert set(written) <= listing, "every artifact is reachable by listing the dir"
        # Every pointer is work-dir-relative and really resolves.
        assert not artifacts["dirs"]["build"].startswith("/")
        assert (tmp_path / artifacts["log"]).is_file()

    def test_subprocess_failure_without_metrics_fails_json(
        self,
        flow_and_state,
        tmp_path: Path,
    ):
        flow, state_file = flow_and_state
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=1,
                stdout="Yosys failed before stat output\n",
                stderr="ERROR: frontend rejected converted Verilog\n",
                duration_s=2.0,
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        assert "RESULT: FAIL" in result.report_text
        assert "rc=1, no metrics" in result.report_text
        # The real subprocess error must reach the report, not just the generic
        # "no metrics" summary (it was previously discarded).
        assert "ERROR: frontend rejected converted Verilog" in result.report_text

        report_path = tmp_path / "reports" / "synth_lite.json"
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["passed"] is False
        assert data["returncode"] == 1
        assert data["timed_out"] is False
        assert data["has_metrics"] is False
        assert data["area_kge"] is None
        assert data["cells"] is None
        assert "frontend rejected converted Verilog" in data["failure_output"]

        st = DevelopmentState.load(state_file)
        assert not st.is_met("synthesis_ok_lite")


# ===========================================================================
# Session Runtime execution
# ===========================================================================


class TestFlowEnablement:
    """The built-in Flow uses the heavy Session Runtime job class."""

    @staticmethod
    def _flow(tmp_path: Path) -> AsicSynthesizeFlow:
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        return flow

    def test_job_class_is_heavy(self, state_file: Path, tmp_path: Path):
        from booley.runtime import job_slots

        assert self._flow(tmp_path)._resolve_job_class() == job_slots.CLASS_HEAVY


# ===========================================================================
# Boundary command + file-based interpretation (ADR 0037 §8)
# ===========================================================================

# Boundary Command Contract parameter regex (ADR 0037 §5). Mirrored here so
# the built-in synth argv is proven safe without coupling this test to the
# executor implementation.
_BOUNDARY_COMMAND_RE = re.compile(r"^make [^;&|<>$()\n\r\t\f\v\\\x60]*$")


class TestBoundaryCommand:
    """The builtin path's ONE crossing command is a bare ``make -C <rel>``."""

    def test_builtin_dispatches_make_argv(self, flow_and_state, tmp_path: Path):
        flow, _ = flow_and_state
        seen: dict[str, list[str]] = {}

        def mock_execute(cmd, **_kwargs):
            seen["cmd"] = cmd
            return SubprocessResult(
                returncode=0,
                stdout="Chip area for top module '\\dut': 6400.0\nNumber of cells: 100\n",
                stderr="",
                duration_s=1.0,
            )

        with patch.object(flow, "_execute", side_effect=mock_execute):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        cmd = seen["cmd"]
        assert cmd[:2] == ["make", "-C"]
        rel = cmd[2]
        # Workspace-relative -C path (contract clause b) pointing at the
        # per-target synth build dir.
        assert not Path(rel).is_absolute()
        assert rel.endswith("synth/lite/synth")
        # No python, no booley module invocation — and the joined command
        # passes the boundary command contract's regex.
        assert "python3" not in cmd
        assert _BOUNDARY_COMMAND_RE.fullmatch(" ".join(cmd))


class TestFileBasedInterpretation:
    """Results come from files under the build dir, stale-gated by dispatch time."""

    @staticmethod
    def _build_dir(tmp_path: Path) -> Path:
        return tmp_path / ".booley_project" / ".runtime" / "edalize" / "synth" / "lite" / "synth"

    def test_metrics_parsed_from_fresh_stage_files(self, flow_and_state, tmp_path: Path):
        """A make run that leaves its results as FILES (empty stdout) still
        yields full metrics — the interpret half reads the stage logs."""
        flow, state_file = flow_and_state
        build_dir = self._build_dir(tmp_path)

        def mock_execute(cmd, **_kwargs):
            # Written "during the run": mtime >= dispatched_unix.
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "yosys.log").write_text(
                "Chip area for top module '\\dut': 52480.0\nNumber of cells: 12345\n",
                encoding="utf-8",
            )
            fresh = time.time() + 1
            os.utime(build_dir / "yosys.log", (fresh, fresh))
            return SubprocessResult(
                returncode=0, stdout="BOOLEY_STAGE: yosys\n", stderr="", duration_s=1.0
            )

        with patch.object(flow, "_execute", side_effect=mock_execute):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert "12,345 cells" in result.report_text
        st = DevelopmentState.load(state_file)
        assert st.is_met("synthesis_ok_lite")

    def test_stale_artifacts_are_not_parsed(self, flow_and_state, tmp_path: Path):
        """A leftover log predating the dispatch must never read as a fresh
        result (ADR 0037 contract clause d)."""
        import time as _time

        flow, state_file = flow_and_state
        build_dir = self._build_dir(tmp_path)
        build_dir.mkdir(parents=True, exist_ok=True)
        leftover = build_dir / "yosys.log"
        leftover.write_text(
            "Chip area for top module '\\dut': 52480.0\nNumber of cells: 12345\n",
            encoding="utf-8",
        )
        stale = _time.time() - 3600
        os.utime(leftover, (stale, stale))

        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(returncode=0, stdout="", stderr="", duration_s=1.0),
        ):
            result = flow._run()

        # The stale numbers were ignored: no metrics -> structural failure.
        assert "12,345" not in result.report_text
        assert result.exit_code == EXIT_FAILURE
        assert "no metrics" in result.report_text
        st = DevelopmentState.load(state_file)
        assert not st.is_met("synthesis_ok_lite")

    def test_error_in_log_despite_exit_0_fails(self, flow_and_state, tmp_path: Path):
        """False-pass guard parity: yosys/ABC can emit ERROR: lines yet exit 0;
        the interpret half's log scan downgrades the run to a failure."""
        flow, state_file = flow_and_state
        build_dir = self._build_dir(tmp_path)

        def mock_execute(cmd, **_kwargs):
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "yosys.log").write_text(
                "Chip area for top module '\\dut': 6400.0\n"
                "Number of cells: 100\n"
                "ERROR: ABC gave up on this netlist\n",
                encoding="utf-8",
            )
            fresh = time.time() + 1
            os.utime(build_dir / "yosys.log", (fresh, fresh))
            return SubprocessResult(returncode=0, stdout="", stderr="", duration_s=1.0)

        with patch.object(flow, "_execute", side_effect=mock_execute):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        assert "FAIL (rc=1)" in result.report_text
        # The scan verdict lands in the persisted full output (the report line
        # itself carries the bounded rc-based summary, as on the legacy path).
        run_log = (
            tmp_path / ".booley_project" / ".runtime" / "edalize" / "synth" / "lite" / "run.log"
        )
        assert "despite exit 0" in run_log.read_text(encoding="utf-8")
        st = DevelopmentState.load(state_file)
        assert not st.is_met("synthesis_ok_lite")

    def test_timing_markers_rederived_from_openroad_log(self, flow_and_state, tmp_path: Path):
        """The Python-derived Fmax/critical-path markers (legacy in-process
        prints) are reconstructed from the STA log file at interpret time."""
        flow, _ = flow_and_state
        build_dir = self._build_dir(tmp_path)

        def fake_configure(target, cmd):
            return _stub_plan(tmp_path, target, mode="physical")

        def mock_execute(cmd, **_kwargs):
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "yosys.log").write_text(
                "Chip area for top module '\\dut': 6400.0\nNumber of cells: 100\n",
                encoding="utf-8",
            )
            (build_dir / "openroad.log").write_text(
                "STA_WORST_SLACK_NS: 2.000000\n"
                "STA_PERCLOCK: name=clk period_ns=4.000000 wns_ns=2.000000 whs_ns=0.1\n"
                "Design area 7000 u^2 50% utilization.\n",
                encoding="utf-8",
            )
            fresh = time.time() + 1
            for stage_log in (build_dir / "yosys.log", build_dir / "openroad.log"):
                os.utime(stage_log, (fresh, fresh))
            return SubprocessResult(returncode=0, stdout="", stderr="", duration_s=1.0)

        with (
            patch.object(flow, "_configure_synth", side_effect=fake_configure),
            patch.object(flow, "_execute", side_effect=mock_execute),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        # period 4 ns, wns 2 ns -> crit path 2000 ps -> Fmax 500 MHz, derived
        # at interpret time from the log file (not from process stdout).
        assert "crit path 2,000 ps" in result.report_text
        assert "Fmax 500 MHz" in result.report_text
        assert "slack +2.000 ns" in result.report_text

    def test_physical_all_na_clock_row_is_incomplete(self, flow_and_state, tmp_path: Path):
        """A clock label without numeric setup/hold evidence cannot complete PPA."""
        flow, state_file = flow_and_state
        build_dir = self._build_dir(tmp_path)

        def mock_execute(cmd, **_kwargs):
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "yosys.log").write_text(
                "Chip area for top module '\\dut': 6400.0\nNumber of cells: 100\n",
                encoding="utf-8",
            )
            (build_dir / "openroad.log").write_text(
                "STA_PERCLOCK: name=clk period_ns=4.000000 wns_ns=NA whs_ns=NA\n"
                "Design area 7000 u^2 50% utilization.\n",
                encoding="utf-8",
            )
            fresh = time.time() + 1
            for stage_log in (build_dir / "yosys.log", build_dir / "openroad.log"):
                os.utime(stage_log, (fresh, fresh))
            return SubprocessResult(returncode=0, stdout="", stderr="", duration_s=1.0)

        with (
            patch.object(
                flow,
                "_configure_synth",
                side_effect=lambda target, cmd: _stub_plan(tmp_path, target, mode="physical"),
            ),
            patch.object(flow, "_execute", side_effect=mock_execute),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        assert "PARTIAL" in result.report_text
        assert result.detail["lite"]["timing_complete"] is False
        assert result.detail["lite"]["ppa_complete"] is False
        assert not DevelopmentState.load(state_file).is_met("synthesis_ok_lite")


# ===========================================================================
# Multi-config run
# ===========================================================================


class TestMultiConfig:
    def test_two_configs(self, state_file: Path, tmp_path: Path):
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full",
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(tmp_path / "reports"),
            ]
        )
        flow.read_state()

        call_count = 0

        def mock_execute(cmd, **_kwargs):
            nonlocal call_count
            call_count += 1
            return SubprocessResult(
                returncode=0,
                stdout=(
                    f"Chip area for top module '\\t': {6400 * call_count}.0\n"
                    "STA_WORST_SLACK_NS: 0.500000\n"
                ),
                stderr="",
                duration_s=1.0 * call_count,
            )

        with patch.object(flow, "_execute", side_effect=mock_execute):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert "PASS" in result.report_text
        assert call_count == 2

        # Both criteria set
        st = DevelopmentState.load(state_file)
        assert st.is_met("synthesis_ok_lite")
        assert st.is_met("synthesis_ok_full")


# ===========================================================================
# Baseline comparison flow
# ===========================================================================


@contextmanager
def _fake_baseline_worktree(project_root: Path, ref: str):
    """Stand-in for ``baseline_worktree``: yields a real dir under the project
    (so path derivations work) without touching git. Records nothing itself;
    tests that need enter/exit tracking define their own."""
    wt = Path(project_root) / ".booley_project" / f".baseline-wt-fake-{ref}"
    wt.mkdir(parents=True, exist_ok=True)
    yield wt


class TestBaselineFlow:
    """Baseline comparison synthesizes the ``--baseline`` ref in a throwaway git
    worktree — never the caller's tree — so it works in both Ticket and
    Interactive Mode (see also test_interactive_smoke)."""

    def test_baseline_dual_run(self, state_file: Path, tmp_path: Path):
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(tmp_path / "reports"),
                "--baseline",
                "v1.0",
            ]
        )
        flow.read_state()

        # Track execute calls — first is baseline (in the worktree), second is
        # the current run (in the real project tree).
        execute_calls = []
        seen_work_dirs = []

        def mock_execute(cmd, **_kwargs):
            seen_work_dirs.append(Path(flow.args.work_dir))
            execute_calls.append(cmd)
            # Baseline produces smaller area
            if len(execute_calls) == 1:
                return SubprocessResult(
                    returncode=0,
                    stdout="Chip area for top module '\\t': 51200.0\nNumber of cells: 100\n"
                    "STA_WORST_SLACK_NS: 0.500000\n",
                    stderr="",
                    duration_s=3.0,
                )
            return SubprocessResult(
                returncode=0,
                stdout="Chip area for top module '\\t': 52480.0\nNumber of cells: 110\n"
                "STA_WORST_SLACK_NS: 0.400000\n",
                stderr="",
                duration_s=4.0,
            )

        with (
            patch("booley.flows.synth.flow.baseline_worktree", _fake_baseline_worktree),
            patch("booley.flows.synth.flow.git_short_sha", return_value="abc1234"),
            patch.object(flow, "_execute", side_effect=mock_execute),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert "baseline: abc1234" in result.report_text
        assert "delta" in result.report_text
        assert "PASS" in result.report_text

        # Two execute calls: baseline + current
        assert len(execute_calls) == 2
        # The baseline ran inside the throwaway worktree; the current run ran in
        # the real project tree; and work_dir was restored afterward.
        assert seen_work_dirs[0] != tmp_path
        assert ".baseline-wt-" in str(seen_work_dirs[0])
        assert seen_work_dirs[1] == tmp_path
        assert Path(flow.args.work_dir) == tmp_path

        # Check report JSON
        report_path = tmp_path / "reports" / "synth_lite.json"
        assert report_path.exists()
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert "baseline" in data
        assert data["baseline"]["ref"] == "abc1234"
        assert data["delta_pct"] == pytest.approx(2.5, rel=0.1)

    def test_worktree_setup_failure_rejected(self, state_file: Path, tmp_path: Path):
        """A worktree that cannot be created (bad ref, not a repo) is surfaced
        as a returncode-1 config error rather than crashing the run."""
        from booley.flows.baseline_worktree import BaselineWorktreeError

        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--work-dir",
                str(tmp_path),
                "--baseline",
                "bogus-ref",
            ]
        )
        flow.read_state()

        def boom(project_root, ref):
            raise BaselineWorktreeError(
                "git worktree add for baseline ref 'bogus-ref' failed: unknown revision"
            )

        with (
            patch("booley.flows.synth.flow.baseline_worktree", boom),
            patch("booley.flows.synth.flow.git_short_sha", return_value="bogus"),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert "synth:" in result.report_text
        assert "worktree add" in result.report_text

    def test_baseline_resolution_failure_is_published_with_canonical_error(
        self,
        state_file: Path,
        tmp_path: Path,
    ):
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--work-dir",
                str(tmp_path),
                "--baseline",
                "v1.0",
                "--report-dir",
                str(tmp_path / "reports"),
            ]
        )
        flow.read_state()
        flow.state.init_criteria(
            {"synthesis_ok_lite": True},
            criterion_params={"synthesis_ok_lite": {BASELINE_TARGET_PARAM: "synth_before"}},
        )
        infra_metrics = SynthMetrics(
            returncode=2,
            infra_error="FuseSoC could not resolve submodule source vendor/ip/top.sv",
            termination="infrastructure_error",
            yosys_complete=False,
            timing_complete=False,
            structural_checks_complete=False,
            ppa_complete=False,
        )
        calls: list[str] = []

        current_metrics = SynthMetrics(area_kge=12.0, cells=100, wns_ns=0.2)

        def run_one(target: str):
            calls.append(target)
            if target == "synth_before":
                return infra_metrics, "resolution failed"
            return current_metrics, "current complete"

        with (
            patch("booley.flows.synth.flow.baseline_worktree", _fake_baseline_worktree),
            patch("booley.flows.synth.flow.git_short_sha", return_value="abc1234"),
            patch.object(flow, "_run_single_config", side_effect=run_one),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert "[synth] baseline lite: ERROR" in result.report_text
        assert "FuseSoC could not resolve submodule source" in result.report_text
        assert calls == ["synth_before", "lite"]
        report = json.loads((flow.args.report_dir / "synth_lite.json").read_text(encoding="utf-8"))
        assert report["implementation"]["status"]["grade"] == "error"
        progress = json.loads(
            (flow.reserve_invocation_dir() / "progress.json").read_text(encoding="utf-8")
        )
        assert progress["complete"] is True

    def test_aggregate_cannot_report_pass_with_baseline_infra_error(self, flow_and_state):
        flow, _ = flow_and_state
        current = SynthMetrics(
            area_kge=12.0,
            cells=100,
            wns_ns=0.2,
            per_clock={"clk": make_clock_timing("clk", 4.0, 0.2, None)},
        )
        baseline = SynthMetrics(
            returncode=2,
            infra_error="baseline source resolution failed",
            termination="infrastructure_error",
            ppa_complete=False,
        )

        result = flow._aggregate_results(
            ["lite"],
            {"lite": current},
            {"lite": baseline},
            "abc1234",
        )

        assert result.exit_code == EXIT_ERROR
        assert "baseline lite: ERROR -- baseline source resolution failed" in result.report_text
        assert "RESULT: FAIL" in result.report_text
        assert result.detail["passed"] is False
        assert result.detail["lite"]["baseline_metrics"]["infra_error"] == (
            "baseline source resolution failed"
        )

    def test_worktree_cleaned_up_and_workdir_restored_on_crash(
        self,
        state_file: Path,
        tmp_path: Path,
    ):
        """Even if baseline synth crashes, the worktree context manager exits
        (cleanup) and ``work_dir`` is restored to the real project tree."""
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--work-dir",
                str(tmp_path),
                "--baseline",
                "v1.0",
            ]
        )
        flow.read_state()

        exited = []

        @contextmanager
        def tracking_worktree(project_root, ref):
            wt = Path(project_root) / ".booley_project" / ".baseline-wt-track"
            wt.mkdir(parents=True, exist_ok=True)
            try:
                yield wt
            finally:
                exited.append(True)

        with (
            patch("booley.flows.synth.flow.baseline_worktree", tracking_worktree),
            patch("booley.flows.synth.flow.git_short_sha", return_value="deadbeef"),
            patch.object(flow, "_execute", side_effect=RuntimeError("synthesis exploded")),
            pytest.raises(RuntimeError, match="synthesis exploded"),
        ):
            flow._run()

        # Worktree cleanup ran and work_dir was restored despite the crash.
        assert exited == [True]
        assert Path(flow.args.work_dir) == tmp_path


# ===========================================================================
# Criterion key pattern
# ===========================================================================


class TestCriterionKey:
    def test_criterion_key_per_config(self, state_file: Path, tmp_path: Path):
        """Each config should set synthesis_ok_<config>."""
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full,combo",
                "--work-dir",
                str(tmp_path),
            ]
        )
        flow.read_state()

        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout="Chip area for top module '\\t': 6400.0\n",
                stderr="",
                duration_s=1.0,
            ),
        ):
            flow._run()

        st = DevelopmentState.load(state_file)
        assert st.is_met("synthesis_ok_lite")
        assert st.is_met("synthesis_ok_full")
        assert st.is_met("synthesis_ok_combo")


# ===========================================================================
# Build command
# ===========================================================================


class TestBuildSynthCmd:
    def test_default_ppa_profile_forwarded(self, flow_and_state, tmp_path: Path):
        flow, _ = flow_and_state
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert cmd[cmd.index("--ppa-profile") + 1] == "balanced"

    def test_profile_and_backend_config_forwarded(self, state_file: Path, tmp_path: Path):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            '[flows.synth]\nppa_profile = "compact"\nsynth_mode = "physical"\n'
            "[flows.synth.advanced_settings_yosys]\nabc_delay_ps = 3333\n"
            "[flows.synth.advanced_settings_openroad]\n"
            "placement_density = 0.72\nrepair_hold = true\n",
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert cmd[cmd.index("--ppa-profile") + 1] == "compact"
        assert cmd[cmd.index("--abc-delay-ps") + 1] == "3333"
        assert cmd[cmd.index("--placement-density") + 1] == "0.72"
        assert "--repair-hold" in cmd

    def test_cli_profile_resets_project_backend_overrides(self, state_file: Path, tmp_path: Path):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            '[flows.synth]\nppa_profile = "compact"\n'
            '[flows.synth.advanced_settings_yosys]\nabc_recipe = "fast"\n',
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            ["--target", "lite", "--work-dir", str(tmp_path), "--ppa-profile", "balanced"]
        )
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert cmd[cmd.index("--ppa-profile") + 1] == "balanced"
        assert "--abc-recipe" not in cmd

    @pytest.mark.parametrize(
        ("config_line", "cli_args", "present", "absent"),
        [
            (
                'abc_script = "+strash;map"',
                ["--abc-recipe", "fast"],
                "--abc-recipe",
                "--abc-script",
            ),
            (
                'abc_recipe = "fast"',
                ["--abc-script", "+strash;map"],
                "--abc-script",
                "--abc-recipe",
            ),
        ],
    )
    def test_cli_abc_control_replaces_other_configured_form(
        self,
        state_file: Path,
        tmp_path: Path,
        config_line: str,
        cli_args: list[str],
        present: str,
        absent: str,
    ):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            f"[flows.synth.advanced_settings_yosys]\n{config_line}\n",
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path), *cli_args])
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert present in cmd
        assert absent not in cmd

    def test_resolves_to_configure_command(self, flow_and_state, tmp_path: Path):
        """FuseSoC resolution drives the run_yosys_syn configure surface.

        The resolved RTL sources go to ``--extra-rtl`` (relative to the worktree
        so they cross the sandbox boundary), include headers to ``--inc-dir``,
        the top to ``-t`` (decision 12), and typed params to ``-d``/``-p``.
        The configure argv carries no ``-c`` because Target resolution already
        supplied the complete design specification (decision 4).
        """
        flow, _ = flow_and_state
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert "configure" in cmd
        assert "-c" not in cmd
        assert cmd[cmd.index("-t") + 1] == "dut"  # resolved toplevel
        extra = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--extra-rtl"]
        assert any(e.endswith("rtl/dut.sv") for e in extra)
        assert any(e.endswith("rtl/pkg.sv") for e in extra)
        assert not any("defs.svh" in e for e in extra)  # include, not a source
        inc = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--inc-dir"]
        assert any(d.endswith("rtl/include") for d in inc)
        # Typed params: vlogdefine -> bare -d NAME, vlogparam -> -p NAME=VALUE.
        assert cmd[cmd.index("-d") + 1] == "SYNTHESIS"
        assert "WIDTH=8" in cmd
        # Relative paths only — absolute worktree paths would not satisfy the
        # relocatable Session Runtime contract.
        assert not any(Path(e).is_absolute() for e in extra)

    def test_sta_sdc_forwarded_from_fileset(self, flow_and_state, tmp_path: Path):
        """The Target's file_type:SDC fileset reaches run_yosys_syn as --sta-sdc,
        one per file, as sandbox-safe relative paths (ADR 0029)."""
        flow, _ = flow_and_state
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _with_synth_mode(
                _fake_synth_resolved(tmp_path), "physical"
            ),
        ):
            cmd = flow._build_synth_cmd("lite")
        sta = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--sta-sdc"]
        assert any(s.endswith("constraints/dut.sdc") for s in sta)
        assert not any(Path(s).is_absolute() for s in sta)

    def test_no_sdc_no_default_clock_hard_errors(
        self,
        flow_and_state,
        tmp_path: Path,
    ):
        """ADR 0031: a Target with no SDC fileset and no --default-clock is a
        hard error (BoundaryError), not a silent 250 MHz default."""
        flow, _ = flow_and_state
        no_sdc = _fake_synth_resolved(tmp_path)
        no_sdc = dataclasses.replace(
            no_sdc,
            files=tuple(f for f in no_sdc.files if f.file_type != "SDC"),
        )
        no_sdc = _with_synth_mode(no_sdc, "physical")
        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=lambda *a, **k: no_sdc,
            ),
            pytest.raises(BoundaryError, match=r"no timing constraints"),
        ):
            flow._build_synth_cmd("lite")

    def test_default_clock_opt_in_forwarded(self, state_file: Path, tmp_path: Path):
        """--default-clock lets a no-SDC Target run against a named clock,
        forwarded to run_yosys_syn (no hard error)."""
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--work-dir",
                str(tmp_path),
                "--default-clock",
                "5000",
            ]
        )
        flow.read_state()
        no_sdc = _fake_synth_resolved(tmp_path)
        no_sdc = dataclasses.replace(
            no_sdc,
            files=tuple(f for f in no_sdc.files if f.file_type != "SDC"),
        )
        no_sdc = _with_synth_mode(no_sdc, "physical")
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: no_sdc,
        ):
            cmd = flow._build_synth_cmd("lite")
        assert cmd[cmd.index("--default-clock") + 1] == "5000.0"
        assert "--sta-sdc" not in cmd

    def test_with_flags(self, state_file: Path, tmp_path: Path):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            "[flows.synth]\nflatten = true\nsdc = true\n",
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--work-dir",
                str(tmp_path),
            ]
        )
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert "--flatten" in cmd
        # Boolean ``sdc = true``: no flag emitted (run_yosys_syn's --sdc takes
        # a path and defaults to abc_simple.sdc; bare "--sdc" would crash).
        assert "--sdc" not in cmd

    def test_stale_stage_cleared_before_resolve(self, flow_and_state, tmp_path: Path):
        """The synth build_root is wiped before resolution so `fusesoc --setup`
        re-stages current RTL (guards against synthesizing stale staged sources)."""
        from booley.flows.edam import work_root_for

        flow, _ = flow_and_state
        build_root = work_root_for(tmp_path, "synth", "lite")
        build_root.mkdir(parents=True, exist_ok=True)
        stale = build_root / "core_0" / "lite" / "src" / "old.sv"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("module old; endmodule\n", encoding="utf-8")

        seen = {}

        def _resolve(*_a, **_k):
            # Record whether the stale tree still exists at resolve time.
            seen["stale_present"] = stale.exists()
            return _fake_synth_resolved(tmp_path)

        with patch.object(fusesoc_registry, "resolve_target", side_effect=_resolve):
            flow._build_synth_cmd("lite")
        assert seen["stale_present"] is False  # cleared before FuseSoC re-stages

    def test_base_defines_absent_ok(self, flow_and_state, tmp_path: Path):
        """No base_defines config: only the Target's vlogdefine params appear."""
        flow, _ = flow_and_state
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        defs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-d"]
        assert defs == ["SYNTHESIS"]

    def test_frontend_default_is_explicit(self, flow_and_state, tmp_path: Path):
        """The normalized recipe spells out the effective sv2v default."""
        flow, _ = flow_and_state
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert cmd[cmd.index("--frontend") + 1] == "sv2v"

    def test_frontend_cli_forwarded(self, state_file: Path, tmp_path: Path):
        """--frontend slang on the Flow CLI reaches run_yosys_syn."""
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path), "--frontend", "slang"])
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert cmd[cmd.index("--frontend") + 1] == "slang"

    def test_target_frontend_forwarded(self, state_file: Path, tmp_path: Path):
        """Target flow_options.frontend is forwarded when no CLI override."""
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            '[flows.synth]\nfrontend = "slang"\n',
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert cmd[cmd.index("--frontend") + 1] == "slang"

    def test_frontend_invalid_config_hard_errors(self, state_file: Path, tmp_path: Path):
        """A bogus Target frontend is a loud BoundaryError."""
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            '[flows.synth]\nfrontend = "verilator"\n',
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
            ),
            pytest.raises(BoundaryError, match=r"frontend must be one of"),
        ):
            flow._build_synth_cmd("lite")

    def test_target_slang_options_forwarded(self, state_file: Path, tmp_path: Path):
        """Target flow_options.slang_options become repeated --slang-option= flags.

        The `=`-joined form is load-bearing, not style: the canonical value is
        itself a flag (--single-unit), which argparse's two-token form rejects
        ("expected one argument") — the ravenoc halt #2b regression.
        """
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            '[flows.synth]\nfrontend = "slang"\n'
            'slang_options = ["--single-unit", "--allow-use-before-declare"]\n',
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert "--slang-option=--single-unit" in cmd
        assert "--slang-option=--allow-use-before-declare" in cmd

    def test_slang_options_invalid_config_hard_errors(self, state_file: Path, tmp_path: Path):
        """A non-list slang_options value is a loud BoundaryError, not a silent no-op."""
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            '[flows.synth]\nslang_options = "--single-unit"\n',
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
            ),
            pytest.raises(BoundaryError, match=r"slang_options must be a non-empty list"),
        ):
            flow._build_synth_cmd("lite")

    def test_result_dir_keyed_on_target_not_toplevel(self, flow_and_state, tmp_path: Path):
        """Even without a suffix, ``-w`` keys the result dir on the target name.

        Regression (data-loss footgun): without ``-w`` configuration uses the
        default ``syn_result/standalone.<toplevel>/`` directory, so two
        targets that share a toplevel module (scalar vs parallel configs of one
        DUT) silently overwrite each other's reports.
        """
        flow, _ = flow_and_state
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        # Result dir is the target name, not the resolved toplevel ("dut").
        assert cmd[cmd.index("-w") + 1] == "lite"

    def test_sta_sdc_forwarded_from_target_fileset(self, flow_and_state, tmp_path: Path):
        """file_type:SDC files on the Target become --sta-sdc args (ADR 0029),
        relative to the worktree; a tb-tagged SDC is excluded."""
        import dataclasses

        flow, _ = flow_and_state
        base = _fake_synth_resolved(tmp_path)
        resolved = dataclasses.replace(
            base,
            files=(
                *base.files,
                fusesoc_registry.ResolvedFile(
                    name="src/syn_demo_0/sdc/core.sdc",
                    file_type="SDC",
                ),
                fusesoc_registry.ResolvedFile(
                    name="src/syn_demo_0/tb/tb.sdc",
                    file_type="SDC",
                    tags=("tb",),
                ),
            ),
        )
        resolved = _with_synth_mode(resolved, "physical")
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: resolved,
        ):
            cmd = flow._build_synth_cmd("lite")
        sta = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--sta-sdc"]
        assert any(s.endswith("sdc/core.sdc") for s in sta)
        assert not any("tb.sdc" in s for s in sta)  # tb-tagged SDC excluded
        assert not any(Path(s).is_absolute() for s in sta)  # boundary-safe

    def test_no_flatten_cli_overrides_toml(self, state_file: Path, tmp_path: Path):
        """--no-flatten wins over Target ``flow_options.flatten = true``."""
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            "[flows.synth]\nflatten = true\n",
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            ["--target", "lite", "--work-dir", str(tmp_path), "--no-flatten"],
        )
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert "--no-flatten" in cmd
        assert "--flatten" not in cmd

    def test_flatten_false_toml_is_honoured(self, state_file: Path, tmp_path: Path):
        """``flatten = false`` now emits --no-flatten (previously ignored)."""
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            "[flows.synth]\nflatten = false\n",
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert "--no-flatten" in cmd

    def test_flatten_default_when_unset(self, flow_and_state, tmp_path: Path):
        """No CLI flag and no TOML knob → flatten on (run_yosys_syn's default)."""
        flow, _ = flow_and_state
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _fake_synth_resolved(tmp_path),
        ):
            cmd = flow._build_synth_cmd("lite")
        assert "--flatten" in cmd
        assert "--no-flatten" not in cmd


# ===========================================================================
# Flow-config trust boundary (plan P2 2026-07-05)
# ===========================================================================


class TestFlowConfigBoundary:
    """Wrong-typed ``[flows.synth]`` knobs fail before EDA execution.

    The ca5adaf class: an untyped config read leaks a stringified Python value
    into the sandbox argv (``synth_mode = true`` → ``--synth-mode True``)
    and dies as an opaque argparse crash inside the container. Every knob read
    in ``_build_synth_cmd`` is now routed through ``core.boundary`` and must
    reject wrong types with an actionable message instead.
    """

    @staticmethod
    def _flow_with_config(tmp_path: Path, toml_body: str) -> AsicSynthesizeFlow:
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(
            f"[flows.synth]\n{toml_body}",
            encoding="utf-8",
        )
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
        flow.read_state()
        return flow

    def test_bool_synth_mode_rejected(self, state_file: Path, tmp_path: Path):
        """`synth_mode = true` must not become ``--synth-mode True``."""
        flow = self._flow_with_config(tmp_path, "synth_mode = true\n")
        with pytest.raises(BoundaryError, match="synth_mode"):
            flow._build_synth_cmd("lite")

    def test_string_synth_mode_forwarded(self, state_file: Path, tmp_path: Path):
        flow = self._flow_with_config(tmp_path, 'synth_mode = "logical"\n')
        cmd = flow._build_synth_cmd("lite")
        assert cmd[cmd.index("--synth-mode") + 1] == "logical"

    def test_unknown_synth_mode_rejected(self, state_file: Path, tmp_path: Path):
        """Typos are caught host-side with the valid choices in the message."""
        flow = self._flow_with_config(tmp_path, 'synth_mode = "placed"\n')
        with pytest.raises(BoundaryError, match="must be one of"):
            flow._build_synth_cmd("lite")

    def test_retired_timing_engine_rejected(self, state_file: Path, tmp_path: Path):
        flow = self._flow_with_config(tmp_path, 'timing_engine = "opensta"\n')
        with pytest.raises(BoundaryError, match="timing_engine is retired"):
            flow._build_synth_cmd("lite")

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("yosys", "advanced_settings_yosys"),
            ("openroad", "advanced_settings_openroad"),
        ],
    )
    def test_retired_expert_table_names_are_hard_errors(
        self,
        state_file: Path,
        tmp_path: Path,
        old: str,
        new: str,
    ):
        flow = self._flow_with_config(tmp_path, f"[flows.synth.{old}]\n")
        with pytest.raises(BoundaryError, match=rf"{old} is retired.*{new}"):
            flow._build_synth_cmd("lite")

    def test_logical_mode_rejects_nonempty_openroad_settings(
        self,
        state_file: Path,
        tmp_path: Path,
    ):
        flow = self._flow_with_config(
            tmp_path,
            'synth_mode = "logical"\n'
            "[flows.synth.advanced_settings_openroad]\nplacement_density = 0.72\n",
        )
        with pytest.raises(BoundaryError, match=r"cannot be set.*logical"):
            flow._build_synth_cmd("lite")

    def test_non_bool_flatten_rejected(self, state_file: Path, tmp_path: Path):
        flow = self._flow_with_config(tmp_path, 'flatten = "yes"\n')
        with pytest.raises(BoundaryError, match="flatten"):
            flow._build_synth_cmd("lite")

    def test_unknown_ppa_profile_rejected(self, state_file: Path, tmp_path: Path):
        flow = self._flow_with_config(tmp_path, 'ppa_profile = "speed"\n')
        with pytest.raises(BoundaryError, match="ppa_profile"):
            flow._build_synth_cmd("lite")

    def test_non_bool_openroad_override_rejected(self, state_file: Path, tmp_path: Path):
        flow = self._flow_with_config(
            tmp_path,
            'synth_mode = "physical"\n'
            '[flows.synth.advanced_settings_openroad]\nrepair_hold = "yes"\n',
        )
        with pytest.raises(BoundaryError, match="repair_hold"):
            flow._build_synth_cmd("lite")

    def test_conflicting_yosys_abc_controls_rejected(self, state_file: Path, tmp_path: Path):
        flow = self._flow_with_config(
            tmp_path,
            '[flows.synth.advanced_settings_yosys]\nabc_recipe = "fast"\nabc_script = "+strash"\n',
        )
        with pytest.raises(BoundaryError, match="cannot set both"):
            flow._build_synth_cmd("lite")

    def test_fractional_abc_delay_rejected(self, state_file: Path, tmp_path: Path):
        flow = self._flow_with_config(
            tmp_path,
            "[flows.synth.advanced_settings_yosys]\nabc_delay_ps = 3333.5\n",
        )
        with pytest.raises(BoundaryError, match="positive integer"):
            flow._build_synth_cmd("lite")

    @pytest.mark.parametrize(
        ("section", "setting"),
        [
            ("advanced_settings_yosys", 'abc_recip = "fast"'),
            ("advanced_settings_openroad", "repair_setp = false"),
        ],
    )
    def test_unknown_backend_setting_rejected(
        self, state_file: Path, tmp_path: Path, section: str, setting: str
    ):
        flow = self._flow_with_config(
            tmp_path,
            ('synth_mode = "physical"\n' if section == "advanced_settings_openroad" else "")
            + f"[flows.synth.{section}]\n{setting}\n",
        )
        with pytest.raises(BoundaryError, match="unknown setting"):
            flow._build_synth_cmd("lite")

    def test_config_error_surfaces_as_infra_error(
        self,
        state_file: Path,
        tmp_path: Path,
    ):
        """The run path reports a config error like a resolution failure
        (returncode-2 infra error), not an unhandled crash of the Flow."""
        flow = self._flow_with_config(tmp_path, "synth_mode = true\n")
        metrics, output = flow._run_single_config("lite")
        assert metrics.returncode == 2
        assert "synth_mode" in (metrics.infra_error or "")
        assert "synth_mode" in output

    def test_dry_run_surfaces_config_error(self, state_file: Path, tmp_path: Path):
        """Dry-run exists to vet the command — a config error must fail it."""
        flow = self._flow_with_config(tmp_path, "synth_mode = true\n")
        result = flow._dry_run(["lite"])
        assert result.exit_code == EXIT_ERROR
        assert "config error" in result.report_text
        assert "synth_mode" in result.report_text


# ===========================================================================
# Timing output format
# ===========================================================================


class TestTimingOutputFormat:
    def test_logical_target_labels_estimate_and_warns(self, flow_and_state):
        flow, _ = flow_and_state
        synth_output = (
            "Chip area for top module '\\t': 6400.0\nYOSYS_ABC_LOGIC_DELAY_PS: 500.000\n"
        )
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout=synth_output,
                stderr="",
                duration_s=2.0,
            ),
        ):
            result = flow._run()
        assert "logical estimate" in result.report_text
        assert "estimated Fmax 2000 MHz" in result.report_text
        assert "estimated Fmax is probably inaccurate" in result.report_text
        assert "excludes placement and wire delays" in result.report_text
        assert "-- ps" not in result.report_text

    def test_logical_is_labelled_without_false_sdc_warning(self, flow_and_state):
        flow, _ = flow_and_state
        metrics = SynthMetrics(
            area_um2=126_880.0,
            area_source="yosys_mapped",
            area_kge=158.6,
            cells=82_463,
            elapsed_s=97.8,
            synth_mode="logical",
        )

        result = flow._aggregate_results(["lite"], {"lite": metrics}, {}, None)

        assert "logical estimate" in result.report_text
        assert "-- ps" not in result.report_text
        assert "no timing was reported" not in result.report_text
        assert "RESULT: PASS" in result.report_text
        assert result.detail["lite"]["synth_mode"] == "logical"
        assert result.detail["lite"]["area_source"] == "yosys_mapped"


# ===========================================================================
# Wire/process count parsing
# ===========================================================================


class TestParseWireCount:
    def test_basic(self):
        assert _parse_wire_count("Number of wires:   450\n") == 450

    def test_no_match(self):
        assert _parse_wire_count("no wire info") == 0

    def test_empty(self):
        assert _parse_wire_count("") == 0


class TestParseProcessCount:
    def test_basic(self):
        assert _parse_process_count("Number of processes:      3\n") == 3

    def test_zero(self):
        assert _parse_process_count("Number of processes:      0\n") == 0

    def test_no_match(self):
        assert _parse_process_count("synthesis completed") == 0


# ===========================================================================
# Per-clock fmax derivation + worst-clock scalars
# ===========================================================================


class TestFmaxDerivation:
    def test_normal_derivation(self):
        # period 2.0 ns, wns 1.0 ns -> crit path 1000 ps -> Fmax 1000 MHz.
        m = _parse_synth_output(
            "Chip area for top module '\\t': 6400.0\n" + _perclock_marker("clk", 2.0, 1.0, 0.1),
            1.0,
        )
        assert m.per_clock["clk"].fmax_mhz == pytest.approx(1000.0)

    def test_no_timing_path_leaves_per_clock_empty(self):
        # No STA_PERCLOCK marker -> no clock timing at all.
        parsed = _parse_synth_output(
            "Chip area for top module '\\t': 100.0\n",
            1.0,
        )
        assert parsed.per_clock == {}
        assert _worst_critical_path_ps(parsed) is None

    def test_none_critical_path(self):
        parsed = _parse_synth_output("no timing info", 1.0)
        assert parsed.per_clock == {}


class TestWorstClockScalars:
    """_worst_critical_path_ps picks the timing-worst clock."""

    def test_picks_lowest_fmax_clock(self):
        m = SynthMetrics(
            per_clock={
                "fast": make_clock_timing("fast", 1.0, 0.5, 0.1),  # 500 ps, 2000 MHz
                "slow": make_clock_timing("slow", 4.0, 0.0, 0.1),  # 4000 ps, 250 MHz
            }
        )
        # Worst = 'slow': largest critical path / lowest Fmax.
        assert _worst_critical_path_ps(m) == pytest.approx(4000.0)

    def test_empty_per_clock_is_none(self):
        m = SynthMetrics()
        assert _worst_critical_path_ps(m) is None

    def test_single_clock_is_that_clock(self):
        m = SynthMetrics(per_clock={"clk": make_clock_timing("clk", 2.0, 0.75, 0.1)})
        assert _worst_critical_path_ps(m) == pytest.approx(1250.0)


# ===========================================================================
# Process count triggers has_critical
# ===========================================================================


class TestProcessCountCritical:
    def test_process_count_fails_implicit(self):
        m = SynthMetrics(process_count=1)
        assert m.has_critical is True

    def test_zero_processes_clean(self):
        m = SynthMetrics(process_count=0)
        assert not m.has_critical


# ===========================================================================
# Detail dict completeness
# ===========================================================================


class TestDetailDict:
    def test_all_metrics_in_detail(self, flow_and_state, tmp_path: Path):
        flow, state_file = flow_and_state
        synth_output = (
            "Chip area for top module '\\t': 6400.0\n"
            "Number of cells: 100\n"
            "Number of wires:   200\n"
            "Number of processes:      0\n" + _perclock_marker("clk", 1.0, 0.5, 0.1)
            # period 1.0 ns, wns 0.5 ns -> crit path 500 ps -> Fmax 2000 MHz.
        )
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout=synth_output,
                stderr="",
                duration_s=2.0,
            ),
        ):
            flow._run()

        st = DevelopmentState.load(state_file)
        entry = st.criteria.get("synthesis_ok_lite")
        assert entry is not None
        d = entry.detail
        assert d["area_um2"] == 6400.0
        assert d["area_kge"] == pytest.approx(6400.0 / KGE_DIVISOR)
        assert d["cells"] == 100
        assert d["wire_count"] == 200
        assert d["process_count"] == 0
        # Critical path / Fmax live per-clock in the detail now.
        assert set(d["per_clock"]) == {"clk"}
        assert d["per_clock"]["clk"]["critical_path_ps"] == pytest.approx(500.0)
        assert d["per_clock"]["clk"]["fmax_mhz"] == pytest.approx(2000.0)
        assert d["per_clock"]["clk"]["period_ns"] == pytest.approx(1.0)
        assert d["per_clock"]["clk"]["wns_ns"] == pytest.approx(0.5)
        assert d["wns_ns"] is None
        assert d["has_critical"] is False
        assert d["latches"] == 0


# ===========================================================================
# Typed-parameter mapping (vlogdefine/vlogparam -> run_yosys_syn CLI)
# ===========================================================================


class TestParamMapping:
    def test_vlogdefine_bool_true_is_bare_define(self):
        params = {"SYNTHESIS": {"paramtype": "vlogdefine", "default": True}}
        assert _vlogdefine_args(params) == ["SYNTHESIS"]

    def test_vlogdefine_value_is_named_define(self):
        params = {"WIDTH": {"paramtype": "vlogdefine", "default": 16}}
        assert _vlogdefine_args(params) == ["WIDTH=16"]

    def test_vlogdefine_false_or_absent_is_undefined(self):
        params = {
            "OFF": {"paramtype": "vlogdefine", "default": False},
            "NONE": {"paramtype": "vlogdefine"},
        }
        assert _vlogdefine_args(params) == []

    def test_vlogparam_emits_assignment(self):
        params = {"WIDTH": {"paramtype": "vlogparam", "default": 8}}
        assert _vlogparam_args(params) == ["WIDTH=8"]

    def test_vlogparam_normalizes_boolean_literals(self):
        params = {
            "ENABLED": {"datatype": "bool", "paramtype": "vlogparam", "default": True},
            "DISABLED": {"datatype": "bool", "paramtype": "vlogparam", "default": False},
        }
        assert _vlogparam_args(params) == ["ENABLED=1", "DISABLED=0"]

    def test_paramtype_filtering_is_exclusive(self):
        params = {
            "D": {"paramtype": "vlogdefine", "default": True},
            "P": {"paramtype": "vlogparam", "default": 4},
            "X": {"paramtype": "plusarg", "default": "y"},
        }
        # vlogdefine helper ignores vlogparam/plusarg and vice versa.
        assert _vlogdefine_args(params) == ["D"]
        assert _vlogparam_args(params) == ["P=4"]

    def test_empty_or_none_params(self):
        assert _vlogdefine_args(None) == []
        assert _vlogparam_args({}) == []


# ===========================================================================
# Synth-target sanity warnings (testbench top / SIMULATION define)
# ===========================================================================


class TestSynthTargetWarnings:
    def test_testbench_top_warns(self):
        [w] = _synth_target_warnings("core_tb", [])
        assert "testbench" in w and "core_tb" in w

    def test_testbench_top_case_insensitive(self):
        assert _synth_target_warnings("MY_TB", [])

    def test_dut_top_is_clean(self):
        assert _synth_target_warnings("core", []) == []
        # a substring '_tb' that isn't the suffix must not trip the check
        assert _synth_target_warnings("u_tb_ctrl", []) == []

    def test_simulation_bare_define_warns(self):
        [w] = _synth_target_warnings("dut", ["SIMULATION"])
        assert "SIMULATION" in w

    def test_simulation_valued_define_warns(self):
        assert _synth_target_warnings("dut", ["SIMULATION=1"])

    def test_simulation_substring_is_clean(self):
        # SIMULATION_SPEED must not be mistaken for the SIMULATION define
        assert _synth_target_warnings("dut", ["SIMULATION_SPEED=2"]) == []

    def test_both_problems_yield_two_warnings(self):
        assert len(_synth_target_warnings("foo_tb", ["SIMULATION"])) == 2


# ===========================================================================
# FuseSoC resolution slice (ADR 0022 Phase 2)
# ===========================================================================


class TestSynthResolution:
    def test_setup_failure_propagates(self, flow_and_state, tmp_path: Path):
        """A FuseSoC resolution failure surfaces (caller records it as infra error)."""
        flow, _ = flow_and_state
        with (
            patch.object(
                fusesoc_registry,
                "resolve_target",
                side_effect=fusesoc_registry.TargetResolutionError("boom"),
            ),
            pytest.raises(fusesoc_registry.TargetResolutionError, match="boom"),
        ):
            flow._build_synth_cmd("lite")

    def test_setup_failure_recorded_as_infra_error(self, flow_and_state):
        """Through the full _run, a resolution failure becomes EXIT_ERROR, not a crash."""
        flow, state_file = flow_and_state
        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=fusesoc_registry.TargetResolutionError("no such target"),
        ):
            result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert "infrastructure error" in result.report_text
        # An infra error must not set the synthesis_ok criterion.
        st = DevelopmentState.load(state_file)
        assert not st.is_met("synthesis_ok_lite")

    def test_resolution_forwards_target_and_isolated_build_root(
        self,
        flow_and_state,
        tmp_path: Path,
    ):
        """resolve_target gets the config name and asic_synthesize's own build root."""
        flow, _ = flow_and_state
        captured = {}

        def fake_resolve(target, *, project_root, build_root, **kw):
            captured.update(
                target=target,
                project_root=project_root,
                build_root=build_root,
            )
            return _fake_synth_resolved(tmp_path)

        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=fake_resolve,
        ):
            flow._build_synth_cmd("lite")
        assert captured["target"] == "lite"
        assert captured["project_root"] == tmp_path
        # Compare build_root in POSIX form for Windows portability.
        assert (
            captured["build_root"]
            .as_posix()
            .endswith(".booley_project/.runtime/edalize/synth/lite")
        )

    def test_real_fusesoc_synth_setup(self, state_file: Path, tmp_path: Path):
        """End-to-end: a real `fusesoc run --setup` leaves a resolved synth EDAM.

        Proves the RTL sources/top/typed-params resolve, the build dir is
        relocatable, and asic_synthesize feeds the resolved filelist + include
        dir + defines to the run_yosys_syn configure surface.
        """
        pytest.importorskip("fusesoc")
        pytest.importorskip("edalize")
        import shutil
        import sys

        work_dir = tmp_path / "proj"
        _write_syn_demo_project(work_dir)

        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                "syn",
                "--work-dir",
                str(work_dir),
            ]
        )
        flow.read_state()

        if shutil.which("fusesoc"):
            fusesoc_cmd = list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
        else:
            fusesoc_cmd = [sys.executable, "-c", "from fusesoc.main import main; main()"]

        with patch.object(
            fusesoc_registry,
            "resolve_target",
            side_effect=lambda *a, **k: _REAL_RESOLVE(
                *a,
                **{**k, "fusesoc_cmd": fusesoc_cmd},
            ),
        ):
            cmd = flow._build_synth_cmd("syn")

        joined = " ".join(cmd)
        # Configuration over the resolved sources (no legacy config selector).
        assert cmd[:4] == ["python3", "-m", "booley.yosys.run_yosys_syn", "configure"]
        assert "-c" not in cmd
        assert cmd[cmd.index("-t") + 1] == "dut"
        # Resolved RTL sources are forwarded as relative (sandbox-safe) paths.
        extra = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--extra-rtl"]
        assert any(e.endswith("rtl/dut.sv") for e in extra)
        assert any(e.endswith("rtl/pkg.sv") for e in extra)
        assert not any("defs.svh" in e for e in extra)
        assert not any(Path(e).is_absolute() for e in extra)
        # The include header surfaces as an include dir, not a source.
        inc = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--inc-dir"]
        assert any(d.endswith("rtl/include") for d in inc)
        # The file_type:SDC fileset is forwarded as --sta-sdc (ADR 0029), sandbox-
        # safe relative path (ADR 0031: a no-SDC target would have hard-errored).
        sta = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--sta-sdc"]
        assert any(s.endswith("constraints/dut.sdc") for s in sta)
        assert not any(Path(s).is_absolute() for s in sta)
        # Typed params reached the CLI.
        assert "SYNTHESIS" in cmd
        assert "WIDTH=8" in joined
        # Resolved build dir lives under the worktree and is relocatable.
        edam = next((work_dir / ".booley_project" / ".runtime").rglob("*.eda.yml"))
        assert str(work_dir) not in edam.read_text(encoding="utf-8")


class TestTimeoutResolution:
    """Per-config timeout precedence: CLI > booley.toml timeout_ms > default."""

    def _flow(self, work_dir: Path, extra_args: list[str]) -> AsicSynthesizeFlow:
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", "lite", "--work-dir", str(work_dir), *extra_args])
        return flow

    def test_default_when_unset(self, tmp_path: Path):
        flow = self._flow(tmp_path, [])
        assert flow._timeout_ms() == 1800000
        assert flow._get_timeout() == 1800

    def test_toml_knob_picked_up(self, tmp_path: Path):
        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            "[flows.synth]\ntimeout_ms = 5400000\n",
            encoding="utf-8",
        )
        flow = self._flow(tmp_path, [])
        assert flow._timeout_ms() == 5400000
        assert flow._get_timeout() == 5400

    def test_cli_overrides_toml(self, tmp_path: Path):
        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            "[flows.synth]\ntimeout_ms = 5400000\n",
            encoding="utf-8",
        )
        flow = self._flow(tmp_path, ["--timeout", "900000"])
        assert flow._timeout_ms() == 900000


# ===========================================================================
# Truncation-resilient output (persisted run.log, demoted logger noise)
# ===========================================================================


class TestTruncationResilientOutput:
    """The MCP layer tail-truncates Flow stdout to ~12KB: the full synth output
    must be persisted per target (pass AND fail), the report must point at it,
    and logger noise (argv echo, multi-line failure dump) must not eat the
    captured-stderr budget."""

    _PASS_OUTPUT = (
        "Chip area for top module '\\design_top': 52480.0\n"
        "Number of cells: 12345\n"
        "STA_CRITICAL_PATH_PS: 1250.0\n"
    )

    @staticmethod
    def _log_file(tmp_path: Path) -> Path:
        return tmp_path / ".booley_project" / ".runtime" / "edalize" / "synth" / "lite" / "run.log"

    def test_synth_log_written_on_pass(self, flow_and_state, tmp_path: Path):
        flow, _ = flow_and_state
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0,
                stdout=self._PASS_OUTPUT,
                stderr="",
                duration_s=1.0,
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        log_file = self._log_file(tmp_path)
        assert log_file.is_file()
        assert "Number of cells: 12345" in log_file.read_text(encoding="utf-8")
        # The report points at the persisted log, project-relative.
        assert (
            "[synth] lite: log: .booley_project/.runtime/edalize/synth/lite/run.log"
        ) in result.report_text

    def test_synth_log_written_on_fail(self, flow_and_state, tmp_path: Path):
        flow, _ = flow_and_state
        with patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=1,
                stdout="Yosys died before stat output\n",
                stderr="ERROR: frontend rejected converted Verilog\n",
                duration_s=1.0,
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_FAILURE
        log_file = self._log_file(tmp_path)
        assert log_file.is_file()
        text = log_file.read_text(encoding="utf-8")
        # Full captured output (stdout AND stderr) is persisted.
        assert "Yosys died before stat output" in text
        assert "ERROR: frontend rejected converted Verilog" in text
        assert (
            "[synth] lite: log: .booley_project/.runtime/edalize/synth/lite/run.log"
        ) in result.report_text

    def test_command_echo_demoted_to_debug(
        self,
        flow_and_state,
        caplog: pytest.LogCaptureFixture,
    ):
        """The full argv (one --extra-rtl per source) must not hit INFO — it
        sprayed into captured stderr and ate the MCP output budget."""
        import logging

        flow, _ = flow_and_state
        with (
            caplog.at_level(logging.DEBUG, logger="booley.flows.synth.flow"),
            patch.object(
                flow,
                "_execute",
                return_value=SubprocessResult(
                    returncode=0,
                    stdout=self._PASS_OUTPUT,
                    stderr="",
                    duration_s=1.0,
                ),
            ),
        ):
            flow._run()

        info_and_up = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert not any("--extra-rtl" in m for m in info_and_up)
        # The full command stays reachable at DEBUG.
        debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("--extra-rtl" in m for m in debug_msgs)

    def test_failure_logger_is_single_line_with_log_pointer(
        self,
        flow_and_state,
        caplog: pytest.LogCaptureFixture,
    ):
        """The no-metrics failure logs ONE single-line ERROR pointing at the
        persisted log — the multi-line excerpt already lives in report_text."""
        import logging

        flow, _ = flow_and_state
        with (
            caplog.at_level(logging.ERROR, logger="booley.flows.synth.flow"),
            patch.object(
                flow,
                "_execute",
                return_value=SubprocessResult(
                    returncode=1,
                    stdout="Yosys died\n",
                    stderr="ERROR: frontend rejected converted Verilog\n",
                    duration_s=1.0,
                ),
            ),
        ):
            flow._run()

        errors = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR and r.name == "booley.flows.synth.flow"
        ]
        assert len(errors) == 1
        assert "\n" not in errors[0]  # single line, no excerpt dump
        assert "run.log" in errors[0]  # pointer to the durable copy
        assert "rc=1" in errors[0]


# ===========================================================================
# Baseline self-compare guard — _baseline_self_compare_warning
# ===========================================================================

# A minimal stealth `.core` (authored under .booley_project/cores/) whose single
# rtl file is what compute_source_fingerprint hashes into the "rtl" digest.
_STEALTH_CORE_TEXT = (
    "CAPI=2:\n"
    "name: ::stealth:0\n"
    "filesets:\n"
    "  rtl:\n"
    "    files:\n"
    "      - rtl/dut.sv: {file_type: systemVerilogSource}\n"
    "targets:\n"
    "  default:\n"
    "    filesets: [rtl]\n"
)


def _author_stealth_core(root: Path, rtl_body: str) -> None:
    """Author a stealth .core + its rtl source under ``root/.booley_project/cores/``."""
    from booley.fusesoc.fusesoc_registry import state_cores_dir

    cores = state_cores_dir(root)
    cores.mkdir(parents=True, exist_ok=True)
    (cores / "design.core").write_text(_STEALTH_CORE_TEXT, encoding="utf-8")
    rtl = cores / "rtl" / "dut.sv"
    rtl.parent.mkdir(parents=True, exist_ok=True)
    rtl.write_text(rtl_body, encoding="utf-8")


class TestBaselineSelfCompareWarning:
    def test_none_without_stealth_cores(self, tmp_path: Path):
        from booley.flows.synth.flow import _baseline_self_compare_warning

        cur = tmp_path / "cur"
        wt = tmp_path / "wt"
        cur.mkdir()
        wt.mkdir()
        # No .booley_project/cores/ on either side → nothing to warn about,
        # even though two empty trees fingerprint identically.
        assert _baseline_self_compare_warning(cur, wt) is None

    def test_warns_on_byte_identical_stealth_rtl(self, tmp_path: Path):
        from booley.flows.synth.flow import _baseline_self_compare_warning

        cur = tmp_path / "cur"
        wt = tmp_path / "wt"
        body = "module dut(input logic clk); endmodule\n"
        _author_stealth_core(cur, body)
        _author_stealth_core(wt, body)  # byte-identical stealth RTL
        msg = _baseline_self_compare_warning(cur, wt)
        assert msg is not None
        assert "self-comparison" in msg

    def test_none_when_stealth_rtl_differs(self, tmp_path: Path):
        from booley.flows.synth.flow import _baseline_self_compare_warning

        cur = tmp_path / "cur"
        wt = tmp_path / "wt"
        _author_stealth_core(cur, "module dut(input logic clk); endmodule\n")
        _author_stealth_core(wt, "module dut(input logic clk, rst); endmodule\n")
        assert _baseline_self_compare_warning(cur, wt) is None


class TestFailOnTimingViolation:
    """[flows.synth].fail_on_timing_violation (ravenoc F-37).

    A -2.633 ns design printed `RESULT: WARN -- timing VIOLATED` and exited 0,
    so an rc-only consumer (a ticket gate, a CI step) read it as success. The
    default stays 0 — synthesis genuinely succeeded and many projects run with
    placeholder constraints — but a project with real constraints can now make
    negative slack a design FAIL.
    """

    @staticmethod
    def _project(tmp_path: Path, body: str) -> None:
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir(exist_ok=True)
        (project_dir / "booley.toml").write_text(body, encoding="utf-8")

    def test_default_is_false(self, tmp_path: Path):
        from booley.flows.synth.flow import _fail_on_timing_violation

        assert _fail_on_timing_violation(tmp_path) is False

    def test_reads_the_knob(self, tmp_path: Path):
        from booley.flows.synth.flow import _fail_on_timing_violation

        self._project(tmp_path, "[flows.synth]\nfail_on_timing_violation = true\n")
        assert _fail_on_timing_violation(tmp_path) is True

    def test_wrong_type_is_a_loud_error(self, tmp_path: Path):
        """A `"yes"` string must not quietly leave the gate disarmed."""
        from booley.flows.synth.flow import _fail_on_timing_violation

        self._project(tmp_path, '[flows.synth]\nfail_on_timing_violation = "yes"\n')
        with pytest.raises(BoundaryError, match="fail_on_timing_violation"):
            _fail_on_timing_violation(tmp_path)

    @staticmethod
    def _violated_run(flow_and_state, *, fatal: bool):
        flow, _ = flow_and_state
        flow._timing_violation_is_fatal = fatal
        metrics = SynthMetrics(
            area_kge=74.4,
            cells=1000,
            wns_ns=-2.633,
            returncode=0,
            per_clock={"clk": make_clock_timing("clk", 4.0, -2.633, None)},
        )
        return flow._aggregate_results(["lite"], {"lite": metrics}, {}, None)

    def test_violation_warns_and_exits_zero_by_default(self, flow_and_state):
        result = self._violated_run(flow_and_state, fatal=False)
        assert result.exit_code == EXIT_SUCCESS
        assert "RESULT: WARN -- timing VIOLATED" in result.report_text

    def test_violation_fails_when_the_knob_is_on(self, flow_and_state):
        result = self._violated_run(flow_and_state, fatal=True)
        assert result.exit_code == EXIT_FAILURE
        assert "RESULT: FAIL" in result.report_text
        # The verdict names the policy so nobody hunts for a structural failure
        # that does not exist.
        assert "fail_on_timing_violation = true" in result.report_text
        assert "-2.633" in result.report_text

    def test_fatal_timing_is_consistent_in_report_and_criterion(self, flow_and_state):
        flow, state_file = flow_and_state
        flow._timing_violation_is_fatal = True
        flow._target_pairs = (TargetPair("lite", "lite"),)
        flow._implementation_reports = {}
        metrics = SynthMetrics(area_kge=1.0, cells=10, wns_ns=-0.25)

        flow._persist_target_outcome("lite", metrics, None, None)

        report = json.loads((flow.args.report_dir / "synth_lite.json").read_text(encoding="utf-8"))
        assert report["passed"] is False
        assert report["implementation"]["status"]["grade"] == "fail"
        entry = DevelopmentState.load(state_file).criteria["synthesis_ok_lite"]
        assert entry.met is False
        assert entry.detail["implementation"]["status"]["grade"] == "fail"

    def test_clean_timing_is_unaffected_by_the_knob(self, flow_and_state):
        flow, _ = flow_and_state
        flow._timing_violation_is_fatal = True
        metrics = SynthMetrics(
            area_kge=74.4,
            cells=1000,
            wns_ns=0.4,
            returncode=0,
            per_clock={"clk": make_clock_timing("clk", 4.0, 0.4, None)},
        )
        result = flow._aggregate_results(["lite"], {"lite": metrics}, {}, None)
        assert result.exit_code == EXIT_SUCCESS
        assert "RESULT: PASS" in result.report_text


class TestResultLine:
    """`_result_line` — one headline, strict severity order."""

    def test_fail_outranks_every_warn(self):
        from booley.flows.synth.flow import _result_line

        line = _result_line(["lite: boom"], "self-compare", ["lite: -1 ns"])
        assert line == "RESULT: FAIL (lite: boom)"

    def test_selfcompare_outranks_timing(self):
        from booley.flows.synth.flow import _result_line

        line = _result_line([], "identical sources", ["lite: -1 ns"])
        assert "baseline delta not meaningful" in line

    def test_timing_violation_warns(self):
        from booley.flows.synth.flow import _result_line

        assert "timing VIOLATED" in _result_line([], None, ["lite: -1 ns"])

    def test_clean_run_passes(self):
        from booley.flows.synth.flow import _result_line

        assert _result_line([], None, []) == "RESULT: PASS"


class TestAggregateDetailIsSelfContained:
    """The flat asic_synthesize.json used to carry ``detail: {}`` (SETUP-F-29b).

    Every number lived only in the per-target ``synthesis_ok_<tgt>`` criteria,
    so a consumer reading the Flow's own report saw a verdict with nothing
    behind it.
    """

    def _metrics(self, **kw) -> SynthMetrics:
        base = {
            "area_um2": 500.0,
            "cells": 1200,
            "wire_count": 800,
            "per_clock": {"clk": make_clock_timing("clk", 2.0, 0.75, 0.1)},
            "wns_ns": 0.25,
        }
        base.update(kw)
        return SynthMetrics(**base)

    def test_carries_targets_and_per_target_qor(self):
        cur = {"asic_a": self._metrics(), "asic_b": self._metrics(cells=99)}
        detail = _aggregate_detail(["asic_a", "asic_b"], cur)
        assert detail["targets"] == ["asic_a", "asic_b"]
        assert detail["passed"] is True
        assert detail["asic_a"]["cells"] == 1200
        assert detail["asic_b"]["cells"] == 99
        assert detail["asic_a"]["per_clock"]["clk"]["fmax_mhz"] == 800.0
        assert detail["asic_a"]["wns_ns"] == 0.25

    def test_failing_target_flips_the_aggregate_verdict(self):
        cur = {"asic_a": self._metrics(), "asic_b": self._metrics(returncode=1)}
        detail = _aggregate_detail(["asic_a", "asic_b"], cur)
        assert detail["passed"] is False
        assert detail["asic_b"]["returncode"] == 1
        assert detail["asic_b"]["passed"] is False

    def test_baseline_ref_and_metrics_ride_along(self):
        cur = {"asic_a": self._metrics()}
        base = {"asic_a": self._metrics(cells=1000)}
        detail = _aggregate_detail(["asic_a"], cur, base, "deadbee")
        assert detail["baseline_ref"] == "deadbee"
        assert detail["asic_a"]["baseline_metrics"]["cells"] == 1000

    def test_result_from_aggregate_carries_it(self, tmp_path: Path):
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "asic_a"])
        flow.read_state()
        with patch.object(AsicSynthesizeFlow, "_set_config_criterion"):
            result = flow._aggregate_results(["asic_a"], {"asic_a": self._metrics()}, {}, None)
        assert result.detail["targets"] == ["asic_a"]
        assert result.detail["asic_a"]["area_um2"] == 500.0


class TestIncompleteResourceResults:
    def test_partial_structural_counts_are_not_critical(self):
        metrics = SynthMetrics(
            area_um2=100.0,
            cells=10,
            latches=512,
            termination="timeout",
            structural_checks_complete=False,
            ppa_complete=False,
        )
        assert metrics.has_metrics
        assert not metrics.has_critical
        assert not metrics.passed

    def test_cgroup_evidence_distinguishes_oom_from_ambiguous_rc137(self):
        from booley.flows.synth.flow import _termination_reason

        ambiguous = SubprocessResult(returncode=2, stderr="make: Error 137")
        corroborated = SubprocessResult(
            returncode=2,
            stderr="make: Error 137",
            oom_kill_delta=1,
        )
        assert _termination_reason(ambiguous, ambiguous.stderr) == "resource_killed"
        assert _termination_reason(corroborated, corroborated.stderr) == "oom"

    def test_completed_target_survives_later_matrix_crash(self, tmp_path: Path):
        flow = AsicSynthesizeFlow()
        report_dir = tmp_path / "reports"
        flow.parse_args(
            [
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(report_dir),
                "--target",
                "asic_a,asic_b",
            ]
        )
        flow.read_state()
        first = SynthMetrics(area_um2=500.0, area_kge=1.0, cells=100)
        calls = 0

        def run_one(_target: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first, "ok"
            raise RuntimeError("simulated outer interruption")

        with (
            patch.object(
                fusesoc_registry, "resolve_target_selection", return_value=["asic_a", "asic_b"]
            ),
            patch.object(flow, "_run_baseline_configs", return_value=({}, None)),
            patch.object(flow, "_run_single_config", side_effect=run_one),
            pytest.raises(RuntimeError, match="outer interruption"),
        ):
            flow._run()

        assert (report_dir / "synth_asic_a.json").is_file()
        invocation_dirs = sorted((report_dir / "synth").iterdir())
        progress = json.loads((invocation_dirs[-1] / "progress.json").read_text())
        assert progress["completed_targets"] == ["asic_a"]
        assert progress["pending_targets"] == ["asic_b"]
        assert (invocation_dirs[-1] / "targets" / "asic_a.json").is_file()
