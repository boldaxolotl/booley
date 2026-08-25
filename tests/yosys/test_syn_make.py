"""Tests for the make-driven boundary split of the synthesis flow (ADR 0037 §8).

Covers the two in-sandbox halves in :mod:`booley.yosys.syn_make`:

* configure — script + Makefile rendering (relative script-internal paths,
  EDA-binaries-only recipes, BOOLEY_STAGE markers, physical timing stage,
  the read-time clock probe SDC), plus ``run_yosys_syn.resolve_spec``'s
  root-anchored resolution;
* interpret — file-based report reconstruction with freshness gating, the
  re-derived physical STA markers, the false-pass log scan, and the SETUP-26
  provenance hint.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from booley.yosys import run_yosys_syn, syn_core, syn_make
from booley.yosys.syn_core import StaTimingConfig

# run_host_command's parameter regex — the Boundary Command Contract's literal
# enforcement point (ADR 0037 §5).
_HOST_COMMAND_RE = re.compile(r"^make [^;&|<>$()\n\r\t\f\v\\\x60]*$")


def _spec(
    tmp_path: Path,
    *,
    mode: str = "physical",
    frontend: str = "sv2v",
    clock: str | None = None,
    sdc: tuple[Path, ...] = (),
    repair_timing: bool = True,
    slang_options: tuple[str, ...] = (),
) -> syn_make.SynthSpec:
    """A minimal SynthSpec over one real source file under *tmp_path*."""
    src = tmp_path / "rtl" / "dut.sv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("module dut(input clk); endmodule\n", encoding="utf-8")
    inc = tmp_path / "rtl" / "include"
    inc.mkdir(exist_ok=True)
    return syn_make.SynthSpec(
        design_name="dut",
        sources=(src,),
        inc_dirs=(inc,),
        defines=("SYNTHESIS",),
        params={"WIDTH": "8"},
        liberty=Path("/opt/pdk/cell/lib/NangateOpenCellLibrary_typical_ccs.lib"),
        liberty_found=True,
        flatten=True,
        abc_recipe="balanced",
        frontend=frontend,
        slang_options=slang_options,
        timing=StaTimingConfig(
            mode=mode,
            clock=clock,
            period_ps=4000.0,
            input_delay_pct=30.0,
            output_delay_pct=70.0,
            sdc=sdc,
            repair_timing=repair_timing,
        ),
    )


def _build_dir(tmp_path: Path) -> Path:
    return tmp_path / ".booley_project" / ".runtime" / "edalize" / "synth" / "s" / "synth"


# ===========================================================================
# Configure half — rendering
# ===========================================================================


class TestConfigureSynthesis:
    def test_slang_options_reach_rendered_synth_ys(self, tmp_path: Path):
        """spec.slang_options land on the read_slang line of synth.ys.

        Regression (ravenoc halt #2b tail): the knob was plumbed through the
        legacy do_run path but dropped by the production configure path
        (resolve_spec -> SynthSpec -> _write_yosys_script), so the rendered
        script silently lacked --single-unit while the CLI parsed it fine.
        """
        plan = syn_make.configure_synthesis(
            _spec(tmp_path, frontend="slang", slang_options=("--single-unit",)),
            _build_dir(tmp_path),
        )
        script = (plan.build_dir / "synth.ys").read_text(encoding="utf-8")
        read_line = next(l for l in script.splitlines() if l.startswith("read_slang"))
        assert "--single-unit" in read_line

    def test_renders_scripts_and_makefile(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        bd = plan.build_dir
        assert (bd / "Makefile").is_file()
        assert (bd / "synth.ys").is_file()
        assert (bd / "sta_constraints.sdc").is_file()
        assert (bd / "run_openroad.tcl").is_file()
        assert (bd / "reports" / "timing").is_dir()
        assert not (bd / "run_opensta.tcl").exists()
        recipe = json.loads((bd / "synthesis_recipe.json").read_text(encoding="utf-8"))
        assert recipe["ppa_profile"] == "balanced"
        assert recipe["flatten"] is True
        assert recipe["yosys"]["abc_recipe"] == "balanced"
        assert recipe["openroad"]["placement_density"] == 0.65

    def test_default_mapping_is_one_liberty_aware_abc_pass(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        script = (plan.build_dir / "synth.ys").read_text(encoding="utf-8")
        assert "synth -top dut -noabc -flatten" in script
        assert script.count("abc -liberty") == 1

    def test_enabled_define_guard_precedes_mapping(self, tmp_path: Path):
        spec = dataclasses.replace(_spec(tmp_path), defines=("ENABLE_ZBB=1", "TRACE=0"))
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        script = (plan.build_dir / "synth.ys").read_text(encoding="utf-8")
        assert "effective_params_dut.il" in script
        assert 'logger -warn "parameter ' in script
        assert "ENABLE_ZBB" in script
        assert "0+)[[:space:]]*$" in script
        assert "TRACE" not in script
        assert script.index("dump -n dut") < script.index("synth -top dut")

    def test_generic_abc_compatibility_override(self, tmp_path: Path):
        spec = dataclasses.replace(_spec(tmp_path), generic_abc_before_mapping=True)
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        script = (plan.build_dir / "synth.ys").read_text(encoding="utf-8")
        assert "synth -top dut -flatten\n" in script
        assert "synth -top dut -noabc -flatten" not in script

    def test_makefile_recipes_are_eda_only_with_stage_markers(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        text = (plan.build_dir / "Makefile").read_text(encoding="utf-8")
        # No Booley on the far side (contract clause c).
        assert "python" not in text
        assert "booley" not in text.replace("Booley asic_synthesize", "")
        # Stage chaining + post-mortem attribution markers.
        assert "all: sta" in text
        for stage in ("sv2v", "yosys", "sta"):
            assert f"BOOLEY_STAGE: {stage}" in text
        # Stage stdout/stderr is captured to per-stage log files.
        assert "> sv2v.log 2>&1" in text
        assert "> yosys.log 2>&1" in text
        assert "> openroad.log 2>&1" in text

    def test_script_paths_are_build_dir_relative(self, tmp_path: Path):
        """Script-internal workspace paths must be relative (the same rendered
        tree is executed via ``make -C`` in either venue)."""
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        makefile = (plan.build_dir / "Makefile").read_text(encoding="utf-8")
        sta_tcl = (plan.build_dir / "run_openroad.tcl").read_text(encoding="utf-8")
        assert str(tmp_path) not in makefile  # sources referenced relatively
        assert "../" in makefile  # ... via an upward relative path
        assert str(tmp_path) not in sta_tcl
        assert "read_verilog {sta_dut.v}" in sta_tcl
        assert "read_sdc {sta_constraints.sdc}" in sta_tcl

    def test_boundary_command_passes_host_regex(self, tmp_path: Path):
        import os

        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        rel = os.path.relpath(plan.build_dir, tmp_path).replace("\\", "/")
        assert _HOST_COMMAND_RE.fullmatch(f"make -C {rel}")

    def test_logical_mode_skips_physical_timing_stage(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path, mode="logical"), _build_dir(tmp_path))
        text = (plan.build_dir / "Makefile").read_text(encoding="utf-8")
        assert "all: yosys" in text
        assert "run_opensta.tcl" not in text
        assert "run_openroad.tcl" not in text
        assert not (plan.build_dir / "run_opensta.tcl").exists()
        assert not (plan.build_dir / "run_openroad.tcl").exists()

    def test_physical_mode_requires_openroad_without_fallback(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        text = (plan.build_dir / "Makefile").read_text(encoding="utf-8")
        assert (plan.build_dir / "run_openroad.tcl").is_file()
        assert "command -v openroad" in text
        assert "Nangate45_tech.lef" in text
        assert "falling back" not in text
        assert "run_opensta.tcl" not in text

    def test_physical_mode_fails_when_openroad_is_missing(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        for name in ("make", "echo"):
            (fake_bin / name).symlink_to(shutil.which(name))

        result = subprocess.run(
            ["make", "-o", "yosys", "-C", str(plan.build_dir), "sta"],
            env={**os.environ, "PATH": str(fake_bin)},
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "requires OpenROAD" in result.stdout

    def test_slang_frontend_skips_sv2v_stage(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(
            _spec(tmp_path, frontend="slang"), _build_dir(tmp_path)
        )
        makefile = (plan.build_dir / "Makefile").read_text(encoding="utf-8")
        script = (plan.build_dir / "synth.ys").read_text(encoding="utf-8")
        assert "BOOLEY_STAGE: sv2v" not in makefile
        assert "sv2v.log" not in makefile
        assert "yosys:\n" in makefile  # no sv2v prerequisite
        assert "read_slang" in script
        assert "sv2v_converted" not in script

    def test_sv2v_frontend_reads_converted_file(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        script = (plan.build_dir / "synth.ys").read_text(encoding="utf-8")
        assert "read_verilog sv2v_converted.v" in script
        # ABC recipe token survives the one-command-per-line split intact.
        assert "+strash;ifraig" in script

    def test_missing_liberty_is_a_warning_not_an_error(self, tmp_path: Path):
        import dataclasses

        spec = dataclasses.replace(_spec(tmp_path), liberty_found=False)
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        assert any("liberty" in w for w in plan.warnings)


class TestBoundarySdc:
    def test_probe_block_when_no_static_clock(self, tmp_path: Path):
        """No --clock and no authored ``create_clock -name`` → the netlist
        doesn't exist at configure time, so the clock port is probed in Tcl."""
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        sdc = (plan.build_dir / "sta_constraints.sdc").read_text(encoding="utf-8")
        assert "foreach _c {clk_i clk clock i_clk aclk}" in sdc
        assert "create_clock -name $_booley_clk" in sdc
        assert "set_input_delay -clock $_booley_clk 1.200000" in sdc
        assert "set_output_delay -clock $_booley_clk 2.800000" in sdc

    def test_static_when_clock_configured(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path, clock="clk"), _build_dir(tmp_path))
        sdc = (plan.build_dir / "sta_constraints.sdc").read_text(encoding="utf-8")
        assert "$_booley_clk" not in sdc
        assert "create_clock -name clk -period 4.000000 [get_ports {clk}]" in sdc

    def test_static_when_authored_sdc_names_its_clock(self, tmp_path: Path):
        authored = tmp_path / "dut.sdc"
        authored.write_text(
            "create_clock -name sys_clk -period 2.0 [get_ports clk]\n",
            encoding="utf-8",
        )
        plan = syn_make.configure_synthesis(_spec(tmp_path, sdc=(authored,)), _build_dir(tmp_path))
        sdc = (plan.build_dir / "sta_constraints.sdc").read_text(encoding="utf-8")
        assert "$_booley_clk" not in sdc
        # Authored SDC owns the clock; generated I/O delays reference its name.
        assert sdc.count("create_clock") == 1
        assert "set_input_delay -clock sys_clk" in sdc


# ===========================================================================
# Interpret half — file-based report reconstruction
# ===========================================================================


def _fresh(_path: Path) -> bool:
    return False


class TestBoundaryOutput:
    def test_emits_delay_marker_from_final_liberty_mapped_abc_log(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path, mode="logical"), _build_dir(tmp_path))
        (plan.build_dir / "log_abc_dut.txt").write_text(
            "ABC: netlist : i/o = 4/2 area =10.0 delay =83.15 lev = 3\n",
            encoding="utf-8",
        )

        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)

        assert "YOSYS_ABC_LOGIC_DELAY_PS: 83.150" in outcome.text

    def test_collects_fresh_stage_files(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        (plan.build_dir / "yosys.log").write_text(
            "Chip area for top module '\\dut': 6400.0\n", encoding="utf-8"
        )
        (plan.build_dir / "stat_dut.txt").write_text("Number of cells: 100\n", encoding="utf-8")
        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)
        assert "Chip area for top module" in outcome.text
        assert "Number of cells: 100" in outcome.text
        assert outcome.forced_failure is None

    def test_recipe_summary_includes_all_expert_controls(self, tmp_path: Path):
        base = _spec(tmp_path)
        timing = base.timing._replace(
            setup_margin_ns=0.2,
            repair_tns_percent=75.0,
        )
        spec = dataclasses.replace(
            base,
            abc_recipe=None,
            abc_script="+strash;map",
            abc_delay_ps=3333,
            timing=timing,
        )
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)
        assert "YOSYS_ABC_RECIPE: default" in outcome.text
        assert "YOSYS_ABC_SCRIPT: +strash;map" in outcome.text
        assert "YOSYS_ABC_DELAY_PS: 3333" in outcome.text
        assert "OPENROAD_SETUP_MARGIN_NS: 0.2" in outcome.text
        assert "OPENROAD_REPAIR_TNS_PERCENT: 75.0" in outcome.text

    def test_stale_files_are_skipped(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        (plan.build_dir / "yosys.log").write_text(
            "Chip area for top module '\\dut': 6400.0\n", encoding="utf-8"
        )
        outcome = syn_make.boundary_output(plan, 0, is_stale=lambda p: True)
        assert "Chip area" not in outcome.text

    def test_rederives_sta_markers_from_log(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        (plan.build_dir / "openroad.log").write_text(
            "STA_WORST_SLACK_NS: 2.000000\n"
            "STA_PERCLOCK: name=clk period_ns=4.000000 wns_ns=2.000000 whs_ns=0.1\n",
            encoding="utf-8",
        )
        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)
        # period 4000 ps - slack 2000 ps -> crit path 2000 ps -> Fmax 500 MHz.
        assert "STA_CRITICAL_PATH_PS: 2000.000" in outcome.text
        assert "STA_FMAX_MHZ: 500.000" in outcome.text
        assert "STA_REPORT:" in outcome.text

    def test_ignores_stale_standalone_opensta_log(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        (plan.build_dir / "openroad.log").write_text(
            "STA_WORST_SLACK_NS: 1.000000\nDesign area 235 u^2 33% utilization.\n",
            encoding="utf-8",
        )
        (plan.build_dir / "sta.log").write_text("STA_WORST_SLACK_NS: 3.000000\n", encoding="utf-8")
        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)
        # The retired standalone OpenSTA path is never consulted.
        assert "STA_WORST_SLACK_NS: 1.000000" in outcome.text
        assert "STA_WORST_SLACK_NS: 3.000000" not in outcome.text
        assert "OPENROAD_DESIGN_AREA_UM2: 235.000" in outcome.text

    def test_scan_forces_failure_on_error_despite_exit_0(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        (plan.build_dir / "yosys.log").write_text("ERROR: ABC gave up\n", encoding="utf-8")
        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)
        assert outcome.forced_failure is not None
        assert "despite exit 0" in outcome.text

    def test_effective_parameter_mismatch_forces_failure(self, tmp_path: Path):
        spec = dataclasses.replace(_spec(tmp_path), defines=("ENABLE_ZBB=1",))
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        artifact = plan.build_dir / syn_core.effective_params_filename("dut")
        artifact.write_text(
            "module \\dut\n  parameter \\ENABLE_ZBB 1'0\nend\n",
            encoding="utf-8",
        )

        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)

        assert outcome.forced_failure is not None
        assert "effective top-level parameter mismatch" in outcome.text
        assert "paramtype: vlogparam" in outcome.text

    def test_macro_driven_enabled_parameter_passes(self, tmp_path: Path):
        spec = dataclasses.replace(_spec(tmp_path), defines=("ENABLE_ZBB=1",))
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        artifact = plan.build_dir / syn_core.effective_params_filename("dut")
        artifact.write_text(
            "module \\dut\n  parameter \\ENABLE_ZBB 1'1\nend\n",
            encoding="utf-8",
        )

        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)

        assert outcome.forced_failure is None
        assert "effective_params_dut.il" in outcome.text

    def test_padded_nonzero_effective_parameter_passes(self, tmp_path: Path):
        spec = dataclasses.replace(_spec(tmp_path), defines=("ENABLE_ZBB=1",))
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        artifact = plan.build_dir / syn_core.effective_params_filename("dut")
        artifact.write_text(
            "module \\dut\n  parameter \\ENABLE_ZBB 32'00000000000000000000000000000001\nend\n",
            encoding="utf-8",
        )

        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)

        assert outcome.forced_failure is None

    def test_provenance_hint_on_failure(self, tmp_path: Path):
        spec = _spec(tmp_path)
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        (plan.build_dir / "sv2v_converted.v").write_text(
            "module dut(input clk);\n  wire bad = ;\nendmodule\n", encoding="utf-8"
        )
        (plan.build_dir / "yosys.log").write_text(
            "ERROR: syntax error at sv2v_converted.v:2\n", encoding="utf-8"
        )
        outcome = syn_make.boundary_output(plan, 1, is_stale=_fresh)
        assert "Source provenance" in outcome.text
        assert str(spec.sources[0]) in outcome.text


# ===========================================================================
# resolve_spec — root-anchored resolution of the parsed spec argv
# ===========================================================================


class TestResolveSpec:
    def _args(self, tmp_path: Path, extra: list[str] | None = None):
        argv = [
            "run",
            "-t",
            "dut",
            "--extra-rtl",
            "rtl/dut.sv",
            "--default-clock",
            "4000",
            "--synth-mode",
            "physical",
            *(extra or []),
        ]
        return run_yosys_syn.parse_run_argv(argv)

    def test_relative_paths_anchor_to_project_root(self, tmp_path: Path):
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        lib = tmp_path / "fake.lib"
        lib.write_text("library(fake) {}\n", encoding="utf-8")
        spec = run_yosys_syn.resolve_spec(
            self._args(tmp_path, ["--liberty", str(lib)]),
            project_root=tmp_path,
        )
        assert spec.sources == ((tmp_path / "rtl" / "dut.sv").resolve(),)
        assert spec.design_name == "dut"
        assert spec.liberty_found is True
        assert spec.timing.mode == "physical"
        assert spec.ppa_profile == "balanced"
        assert spec.abc_recipe == "balanced"
        assert spec.generic_abc_before_mapping is False
        assert spec.timing.utilization_pct == 50.0
        assert spec.timing.placement_density == 0.75

    def test_compact_profile_resolves_both_backend_recipes(self, tmp_path: Path):
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        lib = tmp_path / "fake.lib"
        lib.write_text("library(fake) {}\n", encoding="utf-8")
        spec = run_yosys_syn.resolve_spec(
            self._args(tmp_path, ["--liberty", str(lib), "--ppa-profile", "compact"]),
            project_root=tmp_path,
        )
        assert spec.ppa_profile == "compact"
        assert spec.abc_recipe is None
        assert spec.timing.utilization_pct == 40.0
        assert spec.timing.placement_density == 0.65

    def test_missing_source_exits_with_error(self, tmp_path: Path):
        with pytest.raises(SystemExit, match="Extra RTL file not found"):
            run_yosys_syn.resolve_spec(self._args(tmp_path), project_root=tmp_path)

    def test_lenient_liberty_for_other_venue(self, tmp_path: Path, monkeypatch):
        """require_liberty=False: a missing liberty resolves to a path + warning
        material instead of a hard exit (the executing venue may have it)."""
        monkeypatch.delenv("PRJ_LIB_DIR", raising=False)
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        spec = run_yosys_syn.resolve_spec(
            self._args(tmp_path, ["--liberty", str(tmp_path / "absent.lib")]),
            project_root=tmp_path,
            require_liberty=False,
        )
        assert spec.liberty == tmp_path / "absent.lib"
        assert spec.liberty_found is False

    def test_parse_run_argv_strips_module_prefix(self):
        args = run_yosys_syn.parse_run_argv(
            ["python3", "-m", "booley.yosys.run_yosys_syn", "run", "-t", "top"]
        )
        assert args.action == "run"
        assert args.top == "top"


class TestSv2vRecipeSharesTheArgvBuilder:
    """The make recipe must not hand-roll a second sv2v command line (F-31).

    ``elaborate`` now runs the same transpile; two hand-rolled argvs is exactly
    how an include path or a define quietly stops reaching one of them.
    """

    def test_recipe_matches_syn_core_sv2v_argv(self, tmp_path: Path):
        from booley.yosys import syn_core

        spec = _spec(tmp_path)
        build_dir = tmp_path / "build"
        recipe = syn_make._sv2v_recipe(spec, build_dir)
        expected = syn_core.sv2v_argv(
            [Path(syn_make._rel(f, build_dir)) for f in spec.sources],
            [Path(syn_make._rel(d, build_dir)) for d in spec.inc_dirs],
            list(spec.defines),
            syn_core.SV2V_OUTPUT_NAME,
        )
        assert recipe.split() == expected
        # And it still carries the pieces the transpile actually needs.
        assert "-DSYNTHESIS" in recipe
        assert recipe.endswith("-w sv2v_converted.v")
