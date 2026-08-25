"""Unit tests for booley.flows.fpga.edam — Edalize vivado-flow helpers (ADR 0019).

Covers the three Booley-side halves of the fpga_impl
conversion: the vivado EDAM build, the resolved run command, and the report
post-processor (validated against a captured Vivado impl log). The actual
live Vivado run is exercised separately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from booley.flows import edam as edam_layer
from booley.flows.fpga import edam as fpga_edam

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "vivado_reports"


class TestParseFpgaReports:
    def test_fractional_bram_tiles_are_preserved(self):
        metrics = fpga_edam.parse_fpga_reports("Block RAM Tile | 0.5")

        assert metrics["bram_count"] == 0.5

    """The thin post-processor over raw Vivado log/report text."""

    def test_parses_real_impl_log(self):
        text = (_FIXTURES / "vivado_impl_pass.log").read_text(encoding="utf-8")
        m = fpga_edam.parse_fpga_reports(text)
        assert m["lut_count"] == 2853
        assert m["ff_count"] == 1523
        # The fixture has no Block RAM / DSP utilization rows -> left None.
        assert m["bram_count"] is None
        assert m["dsp_count"] is None
        assert m["wns_ns"] == pytest.approx(0.892)
        assert m["whs_ns"] == pytest.approx(0.031)
        assert m["latch_count"] == 0
        assert m["comb_loop_count"] == 0
        assert m["multi_driven_count"] == 0
        assert m["status"] == "pass"

    def test_parses_real_2025_routed_reports(self):
        """Real Vivado 2025.2 (edalize project-mode) routed reports.

        Regression for the metric-fidelity drift the migration flagged: the real
        report_timing_summary emits a *table* (worst-slack row), not the
        ``WNS(ns) : <v>`` colon form an earlier synthetic fixture assumed, and
        the success marker is ``route_design completed successfully`` (the QoR
        flow stops at route — no bitstream on a boardless core).
        """
        text = (_FIXTURES / "vivado_routed_real.rpt").read_text(encoding="utf-8")
        m = fpga_edam.parse_fpga_reports(text)
        assert m["lut_count"] == 14284
        assert m["ff_count"] == 6475
        assert m["bram_count"] == 0
        assert m["dsp_count"] is None  # excerpt trims the DSP row -> absent
        # Table-form worst-slack row: WNS negative (timing not met), WHS positive.
        assert m["wns_ns"] == pytest.approx(-10.431)
        assert m["whs_ns"] == pytest.approx(0.048)
        assert m["latch_count"] == 0
        # "There are 0 combinational loops in the design." must NOT be counted as
        # a violation — the explicit count is authoritative (regression guard).
        assert m["comb_loop_count"] == 0
        assert m["multi_driven_count"] == 0
        assert m["status"] == "pass"  # route_design completed successfully

    def test_parses_fresh_real_2025_2_routed_reports(self):
        """Genuinely real, from-scratch Vivado 2025.2 routed reports.

        Captured from an independent synth+place+route (a generic DSP+BRAM+FF
        ``top`` on xc7a200tfbg484-1, the pilot's part family) — the definitive
        metric-fidelity check the migration gated on a real host. Unlike
        ``vivado_routed_real.rpt`` this exercises **all four** utilization
        metrics with real BRAM/DSP rows and a **negative** worst-slack row, so it
        guards the table regex against authentic numeric (incl. signed) output.
        """
        text = (_FIXTURES / "vivado_routed_real_2025_2.rpt").read_text(encoding="utf-8")
        m = fpga_edam.parse_fpga_reports(text)
        assert m["lut_count"] == 18
        assert m["ff_count"] == 36
        assert m["bram_count"] == 1
        assert m["dsp_count"] == 1
        # Real signed worst-slack row: WNS negative (timing not met), WHS positive.
        assert m["wns_ns"] == pytest.approx(-8.949)
        assert m["whs_ns"] == pytest.approx(0.195)
        assert m["latch_count"] == 0
        assert m["comb_loop_count"] == 0  # "0 combinational loops" not miscounted
        assert m["multi_driven_count"] == 0
        assert m["status"] == "pass"  # route_design completed successfully

    def test_timing_report_only(self):
        text = (_FIXTURES / "vivado_timing.rpt").read_text(encoding="utf-8")
        m = fpga_edam.parse_fpga_reports(text)
        # The standalone timing report carries WNS but no utilization table and
        # no success marker -> primary counts absent, status unresolved.
        assert m["wns_ns"] == pytest.approx(0.892)
        assert m["lut_count"] is None
        assert m["status"] is None

    def test_empty_output_is_all_unset(self):
        m = fpga_edam.parse_fpga_reports("")
        assert m["lut_count"] is None and m["ff_count"] is None
        assert m["wns_ns"] is None and m["whs_ns"] is None
        assert m["latch_count"] == 0
        assert m["comb_loop_count"] == 0
        assert m["multi_driven_count"] == 0
        assert m["status"] is None

    def test_counts_critical_conditions(self):
        text = (
            "| Register as Latch | 3 |\n"
            "ERROR: [Route 35-7] LUTLP-1 combinational loop\n"
            "MDRV-1 multi-driven net foo\n"
        )
        m = fpga_edam.parse_fpga_reports(text)
        assert m["latch_count"] == 3
        # Both the code and the prose mention combinational loop -> at least one.
        assert m["comb_loop_count"] >= 1
        assert m["multi_driven_count"] >= 1
        assert m["status"] is None  # no success marker

    def test_malformed_colon_timing_degrades_to_none(self):
        # The `-?[\d.]+` capture can match a malformed token ("1.2.3") on
        # Vivado log-format drift — float() would raise. Degrade to None like
        # the integer metrics do, rather than crashing the post-processor.
        text = "WNS(ns) : 1.2.3\nWHS(ns) : ..\n"
        m = fpga_edam.parse_fpga_reports(text)
        assert m["wns_ns"] is None
        assert m["whs_ns"] is None


# A realistic two-clock report: the "Clock Summary" carries each clock's
# constrained Period(ns); the "Intra Clock Table" carries each clock's own worst
# setup/hold slack. _parse_per_clock joins them by name and derives cp/fmax.
_TWO_CLOCK_REPORT = """\
Clock Summary
Clock  Waveform(ns)  Period(ns)  Frequency(MHz)
-----  ------------  ----------  --------------
clk    {0.000 5.000} 10.000      100.000
clk2   {0.000 2.500} 5.000       200.000

Intra Clock Table
Clock  WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)  THS(ns)
-----  -------  -------  ---------------------  -------------------  -------  -------
clk    0.250    0.000    0                      1234                 0.100    0.000
clk2   -0.100   0.500    3                      200                  0.050    0.000
"""


class TestParsePerClock:
    """Per-clock timing: join Clock Summary periods with Intra Clock slacks.

    Fmax/critical-path are inherently per-clock (there is no single "Fmax for
    *what* clock?" for a multi-clock design), so the parser returns a
    ``{clk: {period_ns, wns_ns, whs_ns, critical_path_ps, fmax_mhz}}`` map with
    cp/fmax derived (``cp = (period - wns) * 1000``; ``fmax = 1e6 / cp``).
    """

    def test_joins_two_clocks_and_derives_cp_fmax(self):
        per_clock = fpga_edam._parse_per_clock(_TWO_CLOCK_REPORT)
        assert set(per_clock) == {"clk", "clk2"}

        # clk: period 10, wns +0.25 -> cp (10-0.25)*1000 = 9750 ps, fmax ~102.56.
        clk = per_clock["clk"]
        assert clk["period_ns"] == pytest.approx(10.0)
        assert clk["wns_ns"] == pytest.approx(0.250)
        assert clk["whs_ns"] == pytest.approx(0.100)
        assert clk["critical_path_ps"] == pytest.approx(9750.0)
        assert clk["fmax_mhz"] == pytest.approx(102.5641, rel=1e-4)

        # clk2: period 5, wns -0.1 (timing missed) -> cp 5100 ps, fmax ~196.08.
        clk2 = per_clock["clk2"]
        assert clk2["period_ns"] == pytest.approx(5.0)
        assert clk2["wns_ns"] == pytest.approx(-0.100)
        assert clk2["whs_ns"] == pytest.approx(0.050)
        assert clk2["critical_path_ps"] == pytest.approx(5100.0)
        assert clk2["fmax_mhz"] == pytest.approx(196.0784, rel=1e-4)

    def test_clock_in_only_one_table_still_yields_a_row(self):
        """A clock in Clock Summary but not the Intra table (and vice versa)
        still gets a row; the missing half stays None and cp/fmax can't derive."""
        text = (
            "Clock Summary\n"
            "Clock  Waveform(ns)  Period(ns)  Frequency(MHz)\n"
            "-----  ------------  ----------  --------------\n"
            "clk    {0.000 5.000} 10.000      100.000\n"
            "\n"
            "Intra Clock Table\n"
            "Clock  WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)  THS(ns)\n"
            "-----  -------  -------  ---------------------  -------------------  -------  -------\n"
            "clk2   -0.100   0.500    3                      200                  0.050    0.000\n"
        )
        per_clock = fpga_edam._parse_per_clock(text)
        assert set(per_clock) == {"clk", "clk2"}
        # Period-only clock: slack absent -> cp/fmax None (period alone is not enough).
        assert per_clock["clk"]["period_ns"] == pytest.approx(10.0)
        assert per_clock["clk"]["wns_ns"] is None
        assert per_clock["clk"]["critical_path_ps"] is None
        assert per_clock["clk"]["fmax_mhz"] is None
        # Slack-only clock: period absent -> cp/fmax None (delay is period - slack).
        assert per_clock["clk2"]["wns_ns"] == pytest.approx(-0.100)
        assert per_clock["clk2"]["period_ns"] is None
        assert per_clock["clk2"]["critical_path_ps"] is None
        assert per_clock["clk2"]["fmax_mhz"] is None

    def test_no_clock_tables_yields_empty_map(self):
        assert fpga_edam._parse_per_clock("") == {}

    def test_parse_fpga_reports_returns_per_clock(self):
        """The top-level post-processor surfaces the per_clock map (and carries
        no flat critical_path_ps/fmax_mhz — those were removed as ill-defined)."""
        m = fpga_edam.parse_fpga_reports(_TWO_CLOCK_REPORT)
        assert "per_clock" in m
        assert set(m["per_clock"]) == {"clk", "clk2"}
        assert m["per_clock"]["clk"]["critical_path_ps"] == pytest.approx(9750.0)
        assert "critical_path_ps" not in m
        assert "fmax_mhz" not in m


class TestExtractTimingSafety:
    """_safe_float / _extract_timing guard EDA-tool-output float parsing."""

    def test_safe_float_valid(self):
        assert fpga_edam._safe_float("-8.949") == pytest.approx(-8.949)

    def test_safe_float_none(self):
        assert fpga_edam._safe_float(None) is None

    def test_safe_float_malformed(self):
        assert fpga_edam._safe_float("1.2.3") is None
        assert fpga_edam._safe_float(".") is None
        assert fpga_edam._safe_float("-") is None


class TestBuildFpgaEdam:
    def _edam(self, root: Path, **overrides):
        kwargs = {
            "name": "fpga_cfgA",
            "toplevel": "dut_top",
            "part": "xcvu9p-flga2104-2-i",
            "sv_files": [root / "rtl" / "a.sv", root / "rtl" / "b.sv"],
            "v_files": [root / "rtl" / "legacy.v"],
            "include_dirs": [root / "rtl" / "include"],
            "xdc_files": [root / "constr" / "pins.xdc"],
            "defines": ["SYNTH", "WIDTH=32"],
            "vlogparams": {"DEPTH": 16},
            "workspace_root": root,
            "work_root": root / ".booley_project" / ".runtime" / "edalize" / "fpga" / "cfgA",
        }
        kwargs.update(overrides)
        return fpga_edam.build_fpga_edam(**kwargs)

    def test_flow_options_carry_eda_tool_and_part(self, tmp_path: Path):
        edam = self._edam(tmp_path)
        assert edam["flow_options"] == {"tool": "vivado", "part": "xcvu9p-flga2104-2-i"}
        assert edam["toplevel"] == "dut_top"

    def test_xdc_is_typed_constraint_file(self, tmp_path: Path):
        edam = self._edam(tmp_path)
        xdc = [f for f in edam["files"] if f["name"].endswith("pins.xdc")]
        assert len(xdc) == 1
        assert xdc[0]["file_type"] == "xdc"

    def test_sources_typed_by_suffix(self, tmp_path: Path):
        edam = self._edam(tmp_path)
        types = {Path(f["name"]).name: f["file_type"] for f in edam["files"]}
        assert types["a.sv"] == "systemVerilogSource"
        assert types["legacy.v"] == "verilogSource"

    def test_defines_become_params(self, tmp_path: Path):
        edam = self._edam(tmp_path)
        params = edam["parameters"]
        assert params["SYNTH"]["paramtype"] == "vlogdefine"
        assert params["SYNTH"]["default"] is True
        assert params["WIDTH"]["default"] == 32
        assert params["DEPTH"] == {
            "datatype": "int",
            "paramtype": "vlogparam",
            "default": 16,
        }

    def test_names_relative_to_work_root(self, tmp_path: Path):
        edam = self._edam(tmp_path)
        # relocatable: file names are relative (no leading workspace path).
        assert all(not Path(f["name"]).is_absolute() for f in edam["files"])

    def test_extra_eda_tool_options_whitelisted(self, tmp_path: Path):
        edam = self._edam(tmp_path, extra_eda_tool_options={"jobs": "4"})
        assert edam["flow_options"]["jobs"] == "4"

    def test_extra_eda_tool_options_reject_unknown(self, tmp_path: Path):
        with pytest.raises(edam_layer.EdamSecurityError):
            self._edam(tmp_path, extra_eda_tool_options={"source": "/etc/evil.tcl"})

    def test_file_outside_workspace_rejected(self, tmp_path: Path):
        with pytest.raises(edam_layer.EdamSecurityError):
            self._edam(tmp_path, sv_files=[Path("/etc/passwd")])


class TestRunCommand:
    def test_make_command_relative_to_work_dir(self, tmp_path: Path):
        work_dir = tmp_path
        work_root = tmp_path / ".booley_project" / ".runtime" / "edalize" / "fpga" / "cfgA"
        cmd = fpga_edam.fpga_run_command(work_root, work_dir)
        assert cmd[0] == "make" and cmd[1] == "-C"
        # Relative so it stays independent of the Runtime workspace location.
        assert not Path(cmd[2]).is_absolute()
        assert cmd[2] == ".booley_project/.runtime/edalize/fpga/cfgA"


class TestConfigure:
    """End-to-end file-gen — needs edalize, skipped where absent."""

    def test_configure_writes_project_files(self, tmp_path: Path):
        pytest.importorskip("edalize")
        root = tmp_path
        (root / "rtl").mkdir()
        (root / "rtl" / "a.sv").write_text("module dut_top; endmodule\n")
        (root / "constr").mkdir()
        (root / "constr" / "pins.xdc").write_text("# constraints\n")
        work_root = root / ".booley_project" / ".runtime" / "edalize" / "fpga" / "cfgA"
        edam = fpga_edam.build_fpga_edam(
            name="fpga_cfgA",
            toplevel="dut_top",
            part="xcvu9p-flga2104-2-i",
            sv_files=[root / "rtl" / "a.sv"],
            v_files=[],
            include_dirs=[],
            xdc_files=[root / "constr" / "pins.xdc"],
            defines=["SYNTH"],
            vlogparams={"WIDTH": 8, "FLAG": True, "MODE": "quick run"},
            workspace_root=root,
            work_root=work_root,
        )
        edam_layer.configure("vivado", edam, work_root)
        # configure() is pure file-gen: a Makefile + project tcl must appear.
        assert (work_root / "Makefile").is_file()
        assert any(work_root.glob("*.tcl"))
        project_tcl = work_root / "fpga_cfgA.tcl"
        assert "WIDTH=8" in project_tcl.read_text(encoding="utf-8")
        fpga_edam.validate_vivado_parameter_contract(
            work_root,
            "fpga_cfgA",
            {"WIDTH": 8, "FLAG": True, "MODE": "quick run"},
        )

    def test_parameter_contract_rejects_dropped_override(self, tmp_path: Path):
        (tmp_path / "fpga_cfgA.tcl").write_text("create_project x\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match=r"dropped.*WIDTH"):
            fpga_edam.validate_vivado_parameter_contract(
                tmp_path,
                "fpga_cfgA",
                {"WIDTH": 8},
            )

    def test_parameter_contract_rejects_changed_value(self, tmp_path: Path):
        (tmp_path / "fpga_cfgA.tcl").write_text(
            "set_property generic {WIDTH=99} [get_filesets sources_1]\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match=r"WIDTH=8.*rendered 99"):
            fpga_edam.validate_vivado_parameter_contract(
                tmp_path,
                "fpga_cfgA",
                {"WIDTH": 8},
            )
