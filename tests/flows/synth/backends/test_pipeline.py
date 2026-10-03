"""Tests for the make-driven boundary split of the synthesis flow (ADR 0037 §8).

Covers the two in-sandbox halves in :mod:`booley.flows.synth.backends.pipeline`:

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
import time
from pathlib import Path

import pytest

from booley.core.boundary import BoundaryError
from booley.flows.synth.backends import configure as run_yosys_syn
from booley.flows.synth.backends import pipeline as syn_make
from booley.flows.synth.backends.yosys import core as syn_core
from booley.flows.synth.timing import StaTimingConfig

# Boundary Command Contract parameter regex (ADR 0037 §5).
_BOUNDARY_COMMAND_RE = re.compile(r"^make [^;&|<>$()\n\r\t\f\v\\\x60]*$")


def _spec(
    tmp_path: Path,
    *,
    mode: str = "physical",
    frontend: str = "sv2v",
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
    if mode == "physical" and not sdc:
        authored_sdc = tmp_path / "constraints" / "dut.sdc"
        authored_sdc.parent.mkdir(exist_ok=True)
        authored_sdc.write_text(
            "create_clock -name clk -period 4.0 [get_ports clk]\n",
            encoding="utf-8",
        )
        sdc = (authored_sdc,)
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

        Regression (ravenoc halt #2b tail): the knob was dropped by the
        configure path (resolve_spec -> SynthSpec -> _write_yosys_script), so
        the rendered script silently lacked --single-unit.
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
        assert not (bd / "sta_constraints.sdc").exists()
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

    def test_final_mapped_netlist_gets_a_dedicated_structural_check(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        script = (plan.build_dir / "synth.ys").read_text(encoding="utf-8")

        lines = script.splitlines()
        abc_index = next(i for i, line in enumerate(lines) if "abc -liberty" in line)
        assert lines[abc_index + 1 : abc_index + 7] == [
            "opt",
            "select -assert-none =A:booley_check_library=1",
            "read_liberty -lib -nooverwrite -setattr booley_check_library "
            "/opt/pdk/cell/lib/NangateOpenCellLibrary_typical_ccs.lib",
            "tee -q -o ./check_dut.txt check",
            "delete =A:booley_check_library=1",
            "write_verilog ./synth_dut.v",
        ]
        assert not any(
            token in script
            for token in (
                "design -save",
                "design -load",
                "design -delete",
                "design -push",
                "design -pop",
            )
        )

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
        """Script-internal paths must be relative to the boundary build dir."""
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        makefile = (plan.build_dir / "Makefile").read_text(encoding="utf-8")
        sta_tcl = (plan.build_dir / "run_openroad.tcl").read_text(encoding="utf-8")
        assert str(tmp_path) not in makefile  # sources referenced relatively
        assert "../" in makefile  # ... via an upward relative path
        assert str(tmp_path) not in sta_tcl
        assert "read_verilog {sta_dut.v}" in sta_tcl
        assert "read_sdc {" in sta_tcl
        assert "constraints/dut.sdc}" in sta_tcl

    def test_boundary_command_passes_contract_regex(self, tmp_path: Path):
        import os

        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        rel = os.path.relpath(plan.build_dir, tmp_path).replace("\\", "/")
        assert _BOUNDARY_COMMAND_RE.fullmatch(f"make -C {rel}")

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
        assert "test -f reports/timing/overall.csv.rpt" in text

    @pytest.mark.skipif(os.name == "nt", reason="generated Makefile requires a POSIX shell")
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
        assert "kind=missing_eda_tool" in result.stdout
        assert "stage=openroad" in result.stdout
        assert "subject=openroad" in result.stdout

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


class TestGeneratedMakefile:
    @pytest.mark.skipif(os.name == "nt", reason="generated Makefile requires POSIX shell")
    def test_sv2v_success_without_output_stops_before_yosys(self, tmp_path: Path) -> None:
        plan = syn_make.configure_synthesis(_spec(tmp_path, mode="logical"), _build_dir(tmp_path))
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "sv2v").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "yosys").write_text(
            f"#!/bin/sh\ntouch {tmp_path / 'yosys-ran'}\nexit 0\n", encoding="utf-8"
        )
        (fake_bin / "sv2v").chmod(0o755)
        (fake_bin / "yosys").chmod(0o755)

        result = subprocess.run(
            ["make", "-C", str(plan.build_dir)],
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "kind=missing_output" in result.stdout
        assert "stage=sv2v" in result.stdout
        assert not (tmp_path / "yosys-ran").exists()

    def test_missing_liberty_is_a_warning_not_an_error(self, tmp_path: Path):
        import dataclasses

        spec = dataclasses.replace(_spec(tmp_path), liberty_found=False)
        plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
        assert any("liberty" in w for w in plan.warnings)


class TestBoundarySdc:
    def test_physical_requires_target_owned_sdc(self, tmp_path: Path):
        spec = _spec(tmp_path, mode="logical")
        spec = dataclasses.replace(
            spec,
            timing=StaTimingConfig(mode="physical", sdc=()),
        )
        with pytest.raises(BoundaryError, match="Target-owned SDC"):
            syn_make.configure_synthesis(spec, _build_dir(tmp_path))

    def test_static_when_authored_sdc_names_its_clock(self, tmp_path: Path):
        authored = tmp_path / "dut.sdc"
        authored.write_text(
            "create_clock -name sys_clk -period 2.0 [get_ports clk]\n",
            encoding="utf-8",
        )
        plan = syn_make.configure_synthesis(_spec(tmp_path, sdc=(authored,)), _build_dir(tmp_path))
        script = (plan.build_dir / "run_openroad.tcl").read_text(encoding="utf-8")
        assert authored.read_text(encoding="utf-8") == (
            "create_clock -name sys_clk -period 2.0 [get_ports clk]\n"
        )
        assert "read_sdc {" in script
        assert "dut.sdc}" in script
        assert "\ncreate_clock" not in script
        assert "set_input_delay" not in script
        assert "set_output_delay" not in script
        assert "[llength [all_clocks]] == 0" in script

    def test_multiple_sdc_files_are_loaded_in_target_order(self, tmp_path: Path):
        first = tmp_path / "first.sdc"
        second = tmp_path / "second.sdc"
        first.write_bytes(b"set_false_path -from [get_ports rst_n]\n")
        second.write_bytes(b"create_clock -period 4 [get_ports clk]\n")

        plan = syn_make.configure_synthesis(
            _spec(tmp_path, sdc=(first, second)), _build_dir(tmp_path)
        )
        script = (plan.build_dir / "run_openroad.tcl").read_text(encoding="utf-8")

        assert script.index("first.sdc}") < script.index("second.sdc}")
        assert first.read_bytes() == b"set_false_path -from [get_ports rst_n]\n"
        assert second.read_bytes() == b"create_clock -period 4 [get_ports clk]\n"


# ===========================================================================
# Interpret half — file-based report reconstruction
# ===========================================================================


def _fresh(_path: Path) -> bool:
    return False


class TestBoundaryOutput:
    def test_collects_warning_summary_and_preserves_early_loop_evidence(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        (plan.build_dir / "yosys.log").write_text(
            "Warning: found logic loop in module dut:\n    wire \\feedback\n",
            encoding="utf-8",
        )
        (plan.build_dir / "check_dut.txt").write_text(
            "Warning: multiple conflicting drivers for dut.sig:\n"
            "    port Q[0] of cell $procdff$1 ($dff)\n"
            "Found and reported 1 problem.\n",
            encoding="utf-8",
        )
        (plan.build_dir / "openroad.log").write_text(
            "[WARNING STA-0441] set_input_delay relative to a clock defined "
            "on the same port/pin not allowed.\n",
            encoding="utf-8",
        )

        outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)

        assert outcome.diagnostics.warnings.total_warnings == 3
        assert outcome.diagnostics.structural.complete is True
        assert outcome.diagnostics.structural.comb_loops == 1
        assert outcome.diagnostics.structural.multi_driven == 1
        assert "--- check_dut.txt ---" in outcome.text

    def test_stale_final_check_is_not_complete(self, tmp_path: Path):
        plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
        check = plan.build_dir / "check_dut.txt"
        check.write_text("Found and reported 0 problems.\n", encoding="utf-8")

        outcome = syn_make.boundary_output(plan, 0, is_stale=lambda path: path == check)

        assert outcome.diagnostics.structural.complete is False

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
        assert "kind=missing_output" in outcome.text
        assert "subject=sv2v_converted.v" in outcome.text

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
        sdc = tmp_path / "constraints" / "dut.sdc"
        sdc.parent.mkdir(parents=True, exist_ok=True)
        sdc.write_text("create_clock -period 4 [get_ports clk]\n", encoding="utf-8")
        argv = [
            "configure",
            "-t",
            "dut",
            "--extra-rtl",
            "rtl/dut.sv",
            "--sta-sdc",
            "constraints/dut.sdc",
            "--synth-mode",
            "physical",
            *(extra or []),
        ]
        return run_yosys_syn.parse_configure_argv(argv)

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
        assert spec.timing.placement_density == 0.80

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

    def test_lenient_liberty_for_runtime_diagnostic(self, tmp_path: Path, monkeypatch):
        """require_liberty=False: a missing liberty resolves to a path + warning
        material instead of a hard exit so the boundary run can diagnose it."""
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

    def test_parse_configure_argv_strips_module_prefix(self):
        args = run_yosys_syn.parse_configure_argv(
            ["python3", "-m", "booley.flows.synth.backends.configure", "configure", "-t", "top"]
        )
        assert args.action == "configure"
        assert args.top == "top"


class TestSv2vRecipeSharesTheArgvBuilder:
    """The make recipe must not hand-roll a second sv2v command line (F-31).

    ``elaborate`` now runs the same transpile; two hand-rolled argvs is exactly
    how an include path or a define quietly stops reaching one of them.
    """

    def test_recipe_matches_syn_core_sv2v_argv(self, tmp_path: Path):
        from booley.flows.synth.backends.yosys import core as syn_core

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


@pytest.mark.parametrize("empty", ["stat_dut.txt", "check_dut.txt", "synth_dut.v"])
def test_authenticated_empty_required_yosys_output_is_missing(tmp_path, empty):
    plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
    for name in ["stat_dut.txt", "check_dut.txt", "synth_dut.v", "sv2v_converted.v"]:
        (plan.build_dir / name).write_text("Found and reported 0 problems.\n")
    (plan.build_dir / empty).write_text("")
    outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh)
    assert f"kind=missing_output stage=yosys subject={empty}" in outcome.text
    assert not outcome.yosys_complete


@pytest.mark.parametrize(
    "subject,stage",
    [
        ("sv2v_converted.v", "sv2v"),
        ("stat_dut.txt", "yosys"),
        ("check_dut.txt", "yosys"),
        ("synth_dut.v", "yosys"),
        ("reports/timing/overall.rpt", "openroad"),
        ("reports/timing/overall.csv.rpt", "openroad"),
        ("openroad_dut.v", "openroad"),
    ],
)
@pytest.mark.parametrize("state", ["missing", "stale", "empty"])
def test_all_authenticated_required_artifacts(tmp_path, subject, stage, state):
    plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
    names = [
        "sv2v_converted.v",
        "stat_dut.txt",
        "check_dut.txt",
        "synth_dut.v",
        "reports/timing/overall.rpt",
        "reports/timing/overall.csv.rpt",
        "openroad_dut.v",
    ]
    for name in names:
        path = plan.build_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Found and reported 0 problems.\n")
    path = plan.build_dir / subject
    if state == "missing":
        path.unlink()
    elif state == "empty":
        path.write_text("")
    outcome = syn_make.boundary_output(plan, 0, is_stale=lambda p: state == "stale" and p == path)
    if state == "empty" and subject.endswith(".csv.rpt"):
        assert "kind=missing_output" not in outcome.text
    else:
        assert f"kind=missing_output stage={stage} subject={subject}" in outcome.text
    assert outcome.yosys_complete is (stage != "yosys")


@pytest.mark.parametrize("failing_stage", ["yosys", "openroad"])
def test_generated_fake_synthesis_missing_outputs_are_infrastructure(
    tmp_path, monkeypatch, failing_stage
):
    from booley.flows.base import SubprocessResult
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.flows.synth.implementation_report import _run

    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(cells) {}")
    monkeypatch.setattr(
        syn_make.openroad_timing,
        "openroad_pdk_paths",
        lambda: syn_make.openroad_timing.OpenRoadPdk(liberty, liberty, liberty),
    )
    spec = dataclasses.replace(
        _spec(tmp_path, mode="physical" if failing_stage == "openroad" else "logical"),
        liberty=liberty,
        params={},
    )
    plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scripts = {
        "sv2v": '#!/bin/sh\necho "module dut; endmodule" > sv2v_converted.v\n',
        "yosys": "#!/bin/sh\nexit 0\n"
        if failing_stage == "yosys"
        else '#!/bin/sh\necho "Chip area for module dut: 1000.0" > stat_dut.txt\necho "Found and reported 0 problems." > check_dut.txt\necho "module dut; endmodule" > synth_dut.v\n',
        "openroad": "#!/bin/sh\nexit 0\n",
    }
    for name, script in scripts.items():
        path = fake_bin / name
        path.write_text(script)
        path.chmod(0o755)
    started = time.time()
    result = subprocess.run(
        ["make", "-C", str(plan.build_dir)],
        env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert f"kind=missing_output stage={failing_stage}" in result.stdout
    flow = AsicSynthesizeFlow()
    flow.parse_args(["--target", "dut", "--work-dir", str(tmp_path)])
    metrics, _ = flow._interpret_boundary_run(
        "dut",
        plan,
        SubprocessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            dispatched_unix=started,
        ),
        0.1,
    )
    assert metrics.returncode == 2
    assert failing_stage in metrics.infra_error
    flags = _run(metrics, fatal_timing=True).completion
    assert flags["yosys"] is (failing_stage == "openroad")
    assert flags["timing"] is False
    assert flags["ppa"] is False


@pytest.mark.parametrize("stage", ["sv2v", "yosys", "openroad"])
def test_startup_stage_requires_fresh_stage_log(tmp_path, stage):
    plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
    for name in ["sv2v.log", "yosys.log", "openroad.log"]:
        (plan.build_dir / name).write_text("diagnostic")
    selected = plan.build_dir / f"{stage}.log"
    outcome = syn_make.boundary_output(plan, 1, is_stale=lambda path: path != selected)
    assert outcome.stage == stage
    outcome = syn_make.boundary_output(plan, 1, is_stale=lambda path: True)
    assert outcome.stage is None


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("mode", ["logical", "physical"])
def test_openroad_generic_master_diagnostic_is_fresh_stage_owned(tmp_path, stale, mode):
    plan = syn_make.configure_synthesis(_spec(tmp_path, mode=mode), _build_dir(tmp_path))
    text = "[ERROR ORD-2013] instance latch LEF master $_DLATCH_P_ not found.\n"
    (plan.build_dir / "openroad.log").write_text(text)
    (plan.build_dir / "yosys.log").write_text(text)
    outcome = syn_make.boundary_output(
        plan, 2, is_stale=lambda p: stale and p.name == "openroad.log"
    )
    assert outcome.openroad_diagnostic == (None if stale or mode == "logical" else text.rstrip())


@pytest.mark.skipif(os.name == "nt", reason="generated Makefile requires POSIX shell")
@pytest.mark.parametrize("frontend", ["sv2v", "slang"])
def test_generated_fake_generic_master_latch_design_failure(tmp_path, monkeypatch, frontend):
    from booley.flows.base import SubprocessResult
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.flows.synth.implementation_report import _run

    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(cells) {}")
    monkeypatch.setattr(
        syn_make.openroad_timing,
        "openroad_pdk_paths",
        lambda: syn_make.openroad_timing.OpenRoadPdk(liberty, liberty, liberty),
    )
    spec = dataclasses.replace(_spec(tmp_path, frontend=frontend), liberty=liberty, params={})
    plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
    fake_bin = _fake_generic_master_bin(tmp_path)
    started = time.time()
    result = subprocess.run(
        ["make", "-C", str(plan.build_dir)],
        env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert "ORD-2013" in result.stdout
    flow = AsicSynthesizeFlow()
    flow.parse_args(["--target", "dut", "--work-dir", str(tmp_path)])
    metrics, _ = flow._interpret_boundary_run(
        "dut",
        plan,
        SubprocessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            dispatched_unix=started,
        ),
        0.1,
    )
    run = _run(metrics, fatal_timing=True)
    assert metrics.returncode == 1
    assert metrics.termination == "design_failure"
    assert metrics.infra_error == ""
    assert metrics.latches == metrics.unexpected_latches == 8
    assert "$_DLATCH_P_" in run.diagnostic_excerpt
    assert "actual=8, expected=0, excess=8" in run.diagnostic_excerpt
    assert run.completion["yosys"] is True
    assert run.completion["timing"] is False
    assert run.completion["ppa"] is False


def _fake_generic_master_bin(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scripts = {
        "sv2v": '#!/bin/sh\necho "module dut; endmodule" > sv2v_converted.v\n',
        "yosys": "#!/bin/sh\ncat > stat_dut.txt <<'STAT'\n13. Printing statistics.\n=== dut ===\n  16 36.176 cells\n  8 - $_DLATCH_P_\n  Area for cell type $_DLATCH_P_ is unknown!\nSTAT\ncat stat_dut.txt\necho 'Found and reported 0 problems.' > check_dut.txt\necho 'module dut; endmodule' > synth_dut.v\n",
        "openroad": "#!/bin/sh\necho '[ERROR ORD-2013] instance latch LEF master $_DLATCH_P_ not found.'\nexit 1\n",
    }
    for name, script in scripts.items():
        executable = fake_bin / name
        executable.write_text(script)
        executable.chmod(0o755)
    return fake_bin


@pytest.mark.parametrize(
    ("tool", "diagnostic", "design"),
    [
        ("sv2v", "dut.sv:2:3: Parse error: unexpected token 'killed'", True),
        ("sv2v", "sv2v: unrecognized option --bogus", False),
        ("yosys", "dut.v:2: ERROR: syntax error, unexpected TOK_END", True),
        ("yosys", "ERROR: syntax error at sv2v_converted.v:2", True),
        ("yosys", "dut.sv:2:3: error: expected expression", True),
        (
            "yosys",
            "ERROR: Module `\\missing' referenced in module `\\dut' in cell `\\u' is not part of the design.",
            True,
        ),
        ("yosys", "ERROR: unknown command read_slang", False),
        ("yosys", "ABC: Error loading recipe", False),
        ("yosys", "Unsupported command abc", False),
        ("yosys", "Unsupported RTL construct at dut.sv:2", True),
    ],
)
def test_1095_fresh_stage_design_evidence(tmp_path, tool, diagnostic, design):
    plan = syn_make.configure_synthesis(_spec(tmp_path, mode="logical"), _build_dir(tmp_path))
    (plan.build_dir / f"{tool}.log").write_text(diagnostic)
    outcome = syn_make.boundary_output(plan, 2, is_stale=_fresh, stdout=f"BOOLEY_STAGE: {tool}\n")
    assert outcome.stage == tool
    assert bool(outcome.design_diagnostic) is design
    assert outcome.stage_log == diagnostic


@pytest.mark.parametrize(
    "marker",
    [
        None,
        "floorplan",
        "global_placement",
        "place_pins",
        "repair_design",
        "detailed_placement",
        "sta_report_pre_repair",
        "repair_timing",
        "sta_report",
    ],
)
def test_1095_openroad_owner_and_internal_marker(tmp_path, marker):
    plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
    log = (
        (f"BOOLEY_STAGE: {marker}\n" if marker else "")
        + "\n".join(f"progress {i}" for i in range(60))
        + "\n[ERROR STA-9999] unknown timing failure\n"
    )
    (plan.build_dir / "yosys.log").write_text("dut.v:2: ERROR: syntax error\n")
    (plan.build_dir / "openroad.log").write_text(log)
    outcome = syn_make.boundary_output(
        plan,
        2,
        is_stale=_fresh,
        stdout="BOOLEY_STAGE: yosys\nBOOLEY_STAGE: sta\nBOOLEY_STAGE: repair_design\n",
    )
    assert outcome.stage == "openroad"
    assert outcome.stage_marker == (marker or "sta")
    assert outcome.stage_log == log
    assert not outcome.design_diagnostic


@pytest.mark.parametrize("stale", [True, False])
def test_1095_unconfigured_sv2v_cannot_force_slang_failure(tmp_path, stale):
    plan = syn_make.configure_synthesis(
        _spec(tmp_path, mode="logical", frontend="slang"), _build_dir(tmp_path)
    )
    old = plan.build_dir / "sv2v.log"
    old.write_text("ERROR: syntax error\n")
    (plan.build_dir / "yosys.log").write_text("clean frontend\n")
    outcome = syn_make.boundary_output(plan, 0, is_stale=lambda path: stale and path == old)
    assert outcome.forced_failure is None


@pytest.mark.parametrize("tool", ["sv2v", "yosys", "sta"])
def test_1095_preflight_without_log_keeps_tool_owner(tmp_path, tool):
    plan = syn_make.configure_synthesis(_spec(tmp_path), _build_dir(tmp_path))
    outcome = syn_make.boundary_output(
        plan, 2, is_stale=_fresh, stdout=f"BOOLEY_STAGE: {tool}\nBOOLEY_STAGE: floorplan\n"
    )
    assert outcome.stage == ("openroad" if tool == "sta" else tool)
    assert outcome.stage_log == ""
    assert not outcome.design_diagnostic


@pytest.mark.skipif(os.name == "nt", reason="generated Makefile requires POSIX shell")
@pytest.mark.parametrize("tool", ["sv2v", "yosys", "openroad"])
def test_1095_generated_make_usage_failure(tmp_path, monkeypatch, tool):
    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(cells) {}\n")
    monkeypatch.setattr(
        syn_make.openroad_timing,
        "openroad_pdk_paths",
        lambda: syn_make.openroad_timing.OpenRoadPdk(liberty, liberty, liberty),
    )
    mode = "physical" if tool == "openroad" else "logical"
    spec = dataclasses.replace(_spec(tmp_path, mode=mode), liberty=liberty)
    plan = syn_make.configure_synthesis(spec, _build_dir(tmp_path))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, script in {
        "sv2v": "echo 'module dut; endmodule' > sv2v_converted.v",
        "yosys": "echo 'Number of cells: 2' > stat_dut.txt; echo 'Found and reported 0 problems.' > check_dut.txt; echo 'module dut; endmodule' > synth_dut.v",
        "openroad": "exit 0",
    }.items():
        shim = fake_bin / name
        shim.write_text(
            "#!/bin/sh\n"
            + (
                f"echo '{name}: unrecognized option --bogus' >&2\nexit 1\n"
                if name == tool
                else script + "\n"
            )
        )
        shim.chmod(0o755)
    proc = subprocess.run(
        ["make", "-C", str(plan.build_dir)],
        env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 2
    outcome = syn_make.boundary_output(plan, proc.returncode, is_stale=_fresh, stdout=proc.stdout)
    assert outcome.stage == tool
    assert f"{tool}: unrecognized option --bogus" in outcome.stage_log
    assert not outcome.design_diagnostic


def test_1095_false_pass_uses_offending_frontend_log(tmp_path):
    plan = syn_make.configure_synthesis(_spec(tmp_path, mode="logical"), _build_dir(tmp_path))
    diagnostic = "dut.sv:2:3: Parse error: unexpected token"
    (plan.build_dir / "yosys.log").write_text("clean yosys\n")
    (plan.build_dir / "sv2v.log").write_text("ERROR: " + diagnostic)
    outcome = syn_make.boundary_output(plan, 0, is_stale=_fresh, stdout="BOOLEY_STAGE: yosys\n")
    assert outcome.stage == "sv2v"
    assert outcome.design_diagnostic == "ERROR: " + diagnostic
    assert "clean yosys" not in outcome.stage_log
