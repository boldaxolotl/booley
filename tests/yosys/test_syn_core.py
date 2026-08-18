"""Unit tests for syn_core.py — core synthesis utilities.

Tests cover EDA-tool discovery, defines building, parameter parsing,
source patching, work directory derivation, area/FF parsing,
and Yosys command construction.  All subprocess calls mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make the yosys/ and src/ directories importable
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# find_eda_tool
# ---------------------------------------------------------------------------


class TestFindEdaTool:
    def test_found(self, monkeypatch):
        from booley.yosys import syn_core

        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/yosys")
        result = syn_core.find_eda_tool("yosys")
        assert result == Path("/usr/bin/yosys")

    def test_not_found(self, monkeypatch):
        from booley.yosys import syn_core

        monkeypatch.setattr("shutil.which", lambda name: None)
        assert syn_core.find_eda_tool("missing_tool") is None


# ---------------------------------------------------------------------------
# resolve_liberty
# ---------------------------------------------------------------------------


class TestResolveLiberty:
    def test_cli_path_exists(self, tmp_path):
        from booley.yosys import syn_core

        lib = tmp_path / "test.lib"
        lib.write_text("liberty", encoding="utf-8")
        result = syn_core.resolve_liberty(str(lib))
        assert result == lib

    def test_cli_path_missing_exits(self, tmp_path):
        from booley.yosys import syn_core

        with pytest.raises(SystemExit):
            syn_core.resolve_liberty(str(tmp_path / "nonexistent.lib"))

    def test_env_prj_lib_dir(self, monkeypatch, tmp_path):
        from booley.yosys import syn_core

        lib_dir = tmp_path / "cell" / "lib"
        lib_dir.mkdir(parents=True)
        lib_file = lib_dir / "NangateOpenCellLibrary_typical_ccs.lib"
        lib_file.write_text("liberty", encoding="utf-8")
        monkeypatch.setenv("PRJ_LIB_DIR", str(tmp_path))
        result = syn_core.resolve_liberty(None)
        assert result == lib_file

    def test_no_liberty_found_exits(self, monkeypatch):
        from booley.yosys import syn_core

        monkeypatch.delenv("PRJ_LIB_DIR", raising=False)
        # DEFAULT_LIBERTY moved to booley.yosys.syn_discovery (re-exported by
        # syn_core); patch it where resolve_liberty now reads it.
        monkeypatch.setattr("booley.yosys.syn_discovery.DEFAULT_LIBERTY", Path("/nonexistent/lib"))
        with pytest.raises(SystemExit):
            syn_core.resolve_liberty(None)


# ---------------------------------------------------------------------------
# parse_params
# ---------------------------------------------------------------------------


class TestParseParams:
    def test_basic(self):
        from booley.yosys import syn_core

        result = syn_core.parse_params(["OP_W=32", "DEPTH=4"])
        assert result == {"OP_W": "32", "DEPTH": "4"}

    def test_empty_list(self):
        from booley.yosys import syn_core

        assert syn_core.parse_params([]) == {}

    def test_missing_equals_exits(self):
        from booley.yosys import syn_core

        with pytest.raises(SystemExit):
            syn_core.parse_params(["NO_VALUE"])

    def test_empty_name_exits(self):
        from booley.yosys import syn_core

        with pytest.raises(SystemExit):
            syn_core.parse_params(["=value"])

    def test_value_with_equals(self):
        """Value containing '=' should be preserved."""
        from booley.yosys import syn_core

        result = syn_core.parse_params(["EXPR=a=b"])
        assert result == {"EXPR": "a=b"}

    def test_whitespace_stripped(self):
        from booley.yosys import syn_core

        result = syn_core.parse_params(["  KEY  =  VAL  "])
        assert result == {"KEY": "VAL"}


# ---------------------------------------------------------------------------
# scan_synth_logs — false-pass guard
# ---------------------------------------------------------------------------


class TestScanSynthLogs:
    def test_clean_logs_return_none(self, tmp_path):
        from booley.yosys import syn_core

        (tmp_path / "yosys.log").write_text("All good\nDone.\n", encoding="utf-8")
        assert syn_core.scan_synth_logs(tmp_path) is None

    def test_missing_logs_return_none(self, tmp_path):
        from booley.yosys import syn_core

        assert syn_core.scan_synth_logs(tmp_path) is None

    def test_detects_yosys_error_marker(self, tmp_path):
        from booley.yosys import syn_core

        (tmp_path / "yosys.log").write_text(
            "Running ABC\nERROR: cannot map cell foo\nmore\n", encoding="utf-8"
        )
        line = syn_core.scan_synth_logs(tmp_path)
        assert line is not None
        assert "ERROR: cannot map cell foo" in line

    def test_detects_sv2v_unsupported(self, tmp_path):
        from booley.yosys import syn_core

        (tmp_path / "sv2v.log").write_text("Unsupported construct at line 7\n", encoding="utf-8")
        assert "Unsupported" in syn_core.scan_synth_logs(tmp_path)

    def test_opensta_log_ignored(self, tmp_path):
        """sta.log is not scanned — timing violations are warnings, not failures."""
        from booley.yosys import syn_core

        (tmp_path / "sta.log").write_text("ERROR: setup violation\n", encoding="utf-8")
        assert syn_core.scan_synth_logs(tmp_path) is None

    def test_detects_unknown_cell_area(self, tmp_path):
        """An unmapped cell (zero-area in `stat`) is a false PASS — hard-fail it.

        Yosys exits 0 but `stat` prints this line and scores the cell as zero
        area, silently undercounting the design. Mirrors the exact Yosys output
        (leading whitespace, real internal DFF type).
        """
        from booley.yosys import syn_core

        (tmp_path / "yosys.log").write_text(
            "2.14. Printing statistics.\n"
            "   Area for cell type $_DFF_PN0_ is unknown!\n"
            "   Number of cells: 1234\n",
            encoding="utf-8",
        )
        line = syn_core.scan_synth_logs(tmp_path)
        assert line is not None
        assert "Area for cell type $_DFF_PN0_ is unknown!" in line

    def test_benign_scopeinfo_unknown_area_ignored(self, tmp_path):
        """`$scopeinfo` is a zero-area Yosys metadata cell (survives flatten); a
        stat 'unknown area' for it is noise, not a corrupted total — must NOT fail."""
        from booley.yosys import syn_core

        (tmp_path / "yosys.log").write_text(
            "2.14. Printing statistics.\n"
            "   Area for cell type $scopeinfo is unknown!\n"
            "   Chip area for top module '\\top': 12345.6\n",
            encoding="utf-8",
        )
        assert syn_core.scan_synth_logs(tmp_path) is None

    def test_real_unknown_area_still_fails_alongside_scopeinfo(self, tmp_path):
        """A benign $scopeinfo line does not mask a real unmapped-cell failure."""
        from booley.yosys import syn_core

        (tmp_path / "yosys.log").write_text(
            "   Area for cell type $scopeinfo is unknown!\n"
            "   Area for cell type $_DFF_PN0_ is unknown!\n",
            encoding="utf-8",
        )
        line = syn_core.scan_synth_logs(tmp_path)
        assert line is not None
        assert "$_DFF_PN0_" in line


# ---------------------------------------------------------------------------
# prepare_work_dir
# ---------------------------------------------------------------------------


class TestPrepareWorkDir:
    def test_creates_fresh(self, tmp_path):
        from booley.yosys import syn_core

        work = tmp_path / "new_work"
        syn_core.prepare_work_dir(work)
        assert work.is_dir()

    def test_removes_existing(self, tmp_path):
        from booley.yosys import syn_core

        work = tmp_path / "existing"
        work.mkdir()
        (work / "old.log").write_text("stale", encoding="utf-8")
        syn_core.prepare_work_dir(work)
        assert work.is_dir()
        assert not (work / "old.log").exists()


# ---------------------------------------------------------------------------
# parse_area_from_stat
# ---------------------------------------------------------------------------


class TestParseAreaFromStat:
    def test_top_module_area(self, tmp_path):
        from booley.yosys import syn_core

        stat = tmp_path / "stat_design.txt"
        stat.write_text(
            "Number of cells: 1234\n"
            "Chip area for module '\\sub_mod': 100.50\n"
            "Chip area for top module '\\design': 456.78\n",
            encoding="utf-8",
        )
        assert syn_core.parse_area_from_stat(stat) == 456.78

    def test_fallback_last_match(self, tmp_path):
        from booley.yosys import syn_core

        stat = tmp_path / "stat.txt"
        stat.write_text(
            "Chip area for module '\\a': 100.0\nChip area for module '\\b': 200.0\n",
            encoding="utf-8",
        )
        assert syn_core.parse_area_from_stat(stat) == 200.0

    def test_missing_file(self, tmp_path):
        from booley.yosys import syn_core

        assert syn_core.parse_area_from_stat(tmp_path / "missing.txt") is None

    def test_no_area_line(self, tmp_path):
        from booley.yosys import syn_core

        stat = tmp_path / "stat.txt"
        stat.write_text("Number of cells: 42\n", encoding="utf-8")
        assert syn_core.parse_area_from_stat(stat) is None

    def test_malformed_area_degrades(self, tmp_path):
        # ([\d.]+) can capture "1.2.3" — EDA-tool-output drift must not crash.
        from booley.yosys import syn_core

        stat = tmp_path / "stat.txt"
        stat.write_text(
            "Chip area for top module '\\design': 1.2.3\n",
            encoding="utf-8",
        )
        assert syn_core.parse_area_from_stat(stat) is None


# ---------------------------------------------------------------------------
# area_to_kge
# ---------------------------------------------------------------------------


class TestAreaToKge:
    def test_conversion(self):
        from booley.yosys import syn_core

        # NAND2_AREA_UM2 = 0.798
        # 798 um^2 / (0.798 * 1000) = 1.0 kGE
        kge = syn_core.area_to_kge(798.0)
        assert abs(kge - 1.0) < 0.01

    def test_none(self):
        from booley.yosys import syn_core

        assert syn_core.area_to_kge(None) is None


# ---------------------------------------------------------------------------
# OpenSTA helpers
# ---------------------------------------------------------------------------


class TestOpenStaHelpers:
    def test_detect_clock_port_prefers_common_input(self, tmp_path):
        from booley.yosys import syn_core

        netlist = tmp_path / "sta_top.v"
        netlist.write_text(
            "module top(clk_i, rst_ni, y);\n"
            "  input clk_i;\n"
            "  input rst_ni;\n"
            "  output y;\n"
            "endmodule\n",
            encoding="utf-8",
        )
        assert syn_core.detect_clock_port(netlist) == "clk_i"

    def test_parse_sta_worst_slack_marker(self):
        from booley.yosys import syn_core

        assert syn_core.parse_sta_worst_slack("STA_WORST_SLACK_NS: -0.125") == -0.125

    def test_parse_sta_worst_slack_csv_takes_min(self, tmp_path):
        from booley.yosys import syn_core

        csv = tmp_path / "overall.csv.rpt"
        csv.write_text("a,b,0.100\nc,d,-0.250\n", encoding="utf-8")
        assert syn_core.parse_sta_worst_slack(csv) == -0.25

    def test_parse_sta_worst_slack_malformed_marker_degrades(self, monkeypatch):
        # Simulate OpenSTA marker-format drift: the regex matches but the
        # captured group is not a valid float.  Must fall back to the CSV
        # scan (here yielding None) rather than crashing synthesis.
        import re

        from booley.yosys import syn_core

        drifted = re.compile(r"STA_WORST_SLACK_NS:\s*(\S+)")
        monkeypatch.setattr(syn_core, "_STA_SLACK_RE", drifted)
        assert syn_core.parse_sta_worst_slack("STA_WORST_SLACK_NS: n/a") is None

    def test_write_sta_sdc_uses_config(self, tmp_path):
        from booley.yosys import syn_core

        cfg = syn_core.StaTimingConfig(
            engine="opensta",
            clock="clk_i",
            period_ps=2000.0,
            input_delay_pct=25.0,
            output_delay_pct=60.0,
            sdc=(),
        )
        path = syn_core.write_sta_sdc(cfg, "clk_i", tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "create_clock -name clk_i -period 2.000000" in text
        assert "set_input_delay -clock clk_i 0.500000" in text
        assert "set_output_delay -clock clk_i 1.200000" in text
        # remove_from_collection is unavailable in some OpenSTA builds, so it must
        # be guarded with a catch + all_inputs fallback rather than emitted bare.
        assert "catch { set input_ports [remove_from_collection" in text
        assert "set input_ports [all_inputs] }" in text

    def test_reg2reg_tcl_is_register_scoped_and_guarded(self):
        from booley.yosys import syn_core

        tcl = syn_core.reg2reg_timing_tcl()
        # Restricts the worst-path search to register endpoints (internal path).
        assert "-from [all_registers]" in tcl
        assert "-to [all_registers]" in tcl
        # Guarded: a combinational design / older STA degrades to no marker.
        assert "catch" in tcl
        assert "STA_REG2REG_SLACK_NS" in tcl

    def test_reg2reg_tcl_omits_report_when_no_path_given(self):
        from booley.yosys import syn_core

        # Default stays the arg-free slack-only block: callers that only want
        # the marker (and the golden snapshot) must not grow a stray file write.
        assert "report_checks" not in syn_core.reg2reg_timing_tcl()

    def test_reg2reg_tcl_writes_path_detail_when_asked(self):
        from booley.yosys import syn_core

        tcl = syn_core.reg2reg_timing_tcl("reports/timing/reg2reg.rpt")
        # Full gate-by-gate detail of the *reg->reg* path, not the overall one.
        assert "report_checks" in tcl
        assert "-format full" in tcl
        assert "> {reports/timing/reg2reg.rpt}" in tcl
        # Inside the same guard as the marker, under its own catch: a report
        # write that chokes must not cost us the slack number.
        assert tcl.index("STA_REG2REG_SLACK_NS") < tcl.index("report_checks")
        assert "  catch {report_checks" in tcl

    def test_write_sta_script_embeds_reg2reg_block(self, tmp_path):
        from booley.yosys import syn_core

        path = syn_core.write_sta_script(
            "top",
            tmp_path / "lib.lib",
            tmp_path / "sta_top.v",
            tmp_path / "c.sdc",
            tmp_path,
            tmp_path,
        )
        text = path.read_text(encoding="utf-8")
        assert "STA_REG2REG_SLACK_NS" in text
        assert (tmp_path / "reg2reg.rpt").as_posix() in text

    def test_perclock_tcl_iterates_all_clocks(self):
        from booley.yosys import syn_core

        tcl = syn_core.perclock_timing_tcl()
        # Iterates every clock and reports paths ending in that clock domain.
        assert "all_clocks" in tcl
        assert "STA_PERCLOCK" in tcl
        assert "-to $_clk" in tcl
        # Reports both setup (max) and hold (min) slack, guarded by catch.
        assert "-path_delay max" in tcl
        assert "-path_delay min" in tcl
        assert "catch" in tcl

    def test_write_sta_script_embeds_perclock_block(self, tmp_path):
        from booley.yosys import syn_core

        text = syn_core.write_sta_script(
            "top",
            tmp_path / "lib.lib",
            tmp_path / "sta_top.v",
            tmp_path / "c.sdc",
            tmp_path,
            tmp_path,
        ).read_text(encoding="utf-8")
        assert "STA_PERCLOCK" in text
        assert "-to $_clk" in text
        # Per-clock block sits between the CSV close and the reg->reg block.
        assert text.index("close $csv_out") < text.index("STA_PERCLOCK")
        assert text.index("STA_PERCLOCK") < text.index("STA_REG2REG_SLACK_NS")

    def test_parse_reg2reg_slack_marker(self):
        from booley.yosys import syn_core

        assert syn_core.parse_reg2reg_slack("STA_REG2REG_SLACK_NS: -0.30") == -0.30
        assert syn_core.parse_reg2reg_slack("nothing here") is None

    def test_print_reg2reg_fmax_emits_derived(self, capsys):
        from booley.yosys import syn_core

        # slack +0.5 ns at a 2000 ps period → crit 1500 ps → 666.667 MHz.
        assert syn_core.print_reg2reg_fmax("STA_REG2REG_SLACK_NS: 0.500000", 2000.0) is True
        out = capsys.readouterr().out
        assert "STA_REG2REG_CRITICAL_PATH_PS: 1500.000" in out
        assert "STA_REG2REG_FMAX_MHZ: 666.667" in out

    def test_print_reg2reg_fmax_noop_when_absent(self, capsys):
        from booley.yosys import syn_core

        assert syn_core.print_reg2reg_fmax("STA_WORST_SLACK_NS: -1.0", 2000.0) is False
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# emit_timing_markers — reg2reg survives a false-pathed overall worst path
# (SETUP-29)
# ---------------------------------------------------------------------------


class TestEmitTimingMarkers:
    @staticmethod
    def _config(period_ps=2000.0):
        from booley.yosys.syn_core import StaTimingConfig

        return StaTimingConfig(
            engine="opensta",
            clock="clk_i",
            period_ps=period_ps,
            input_delay_pct=30.0,
            output_delay_pct=70.0,
            sdc=(),
        )

    def test_overall_and_reg2reg_both_emitted(self, tmp_path, capsys):
        from booley.yosys import syn_core

        stdout = "STA_WORST_SLACK_NS: -0.100000\nSTA_REG2REG_SLACK_NS: 0.500000\n"
        assert syn_core.emit_timing_markers(stdout, self._config(), tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_WORST_SLACK_NS: -0.100000" in out
        assert "STA_CRITICAL_PATH_PS: 2100.000" in out
        assert "STA_REG2REG_FMAX_MHZ: 666.667" in out
        assert "STA_REPORT:" in out and "STA_CSV_REPORT:" in out

    def test_reg2reg_survives_when_overall_absent(self, tmp_path, capsys):
        """A false-pathed / I/O-bound overall worst path leaves no
        STA_WORST_SLACK_NS, but the internal reg->reg Fmax must still surface."""
        from booley.yosys import syn_core

        # No overall marker, and no overall.csv.rpt on disk — only reg->reg.
        stdout = "STA_REG2REG_SLACK_NS: 0.500000\n"
        assert syn_core.emit_timing_markers(stdout, self._config(), tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_WORST_SLACK_NS" not in out
        assert "STA_REG2REG_CRITICAL_PATH_PS: 1500.000" in out
        assert "STA_REG2REG_FMAX_MHZ: 666.667" in out
        # Report pointers surface because timing data (reg->reg) was found.
        assert "STA_REPORT:" in out

    def test_nothing_surfaced_returns_false(self, tmp_path, capsys):
        from booley.yosys import syn_core

        assert syn_core.emit_timing_markers("no markers here", self._config(), tmp_path) is False
        out = capsys.readouterr().out
        assert "STA_REPORT:" not in out

    def test_overall_from_csv_when_stdout_marker_absent(self, tmp_path, capsys):
        from booley.yosys import syn_core

        # Overall slack only in the CSV report; reg->reg absent.
        (tmp_path / "overall.csv.rpt").write_text(
            "startpt,endpt,-0.250000\n",
            encoding="utf-8",
        )
        assert syn_core.emit_timing_markers("", self._config(), tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_WORST_SLACK_NS: -0.250000" in out

    def test_reg2reg_report_pointer_only_when_the_file_exists(self, tmp_path, capsys):
        """The reg->reg path detail is advertised only when it was written.

        The Tcl skips the report on a register-free design and the pre-repair
        salvage path never reaches it, so an unconditional pointer would send
        the reader to a file that isn't there."""
        from booley.yosys import syn_core

        stdout = "STA_REG2REG_SLACK_NS: 0.500000\n"
        assert syn_core.emit_timing_markers(stdout, self._config(), tmp_path) is True
        assert "STA_REG2REG_REPORT:" not in capsys.readouterr().out

        (tmp_path / "reg2reg.rpt").write_text("Startpoint: ...\n", encoding="utf-8")
        assert syn_core.emit_timing_markers(stdout, self._config(), tmp_path) is True
        out = capsys.readouterr().out
        assert f"STA_REG2REG_REPORT: {tmp_path / 'reg2reg.rpt'}" in out

    def test_perclock_surfaces_when_overall_and_reg2reg_absent(self, tmp_path, capsys):
        # A design whose overall/reg2reg queries are empty still reports genuine
        # per-clock timing — emit_timing_markers must surface it (return True).
        from booley.yosys import syn_core

        stdout = "STA_PERCLOCK: name=clk period_ns=2.000000 wns_ns=0.750000 whs_ns=0.100000\n"
        assert syn_core.emit_timing_markers(stdout, self._config(), tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_PERCLOCK: name=clk" in out
        assert "STA_REPORT:" in out


# ---------------------------------------------------------------------------
# Per-clock marker parsing / re-emission (Fmax + critical path are per-clock)
# ---------------------------------------------------------------------------


class TestPerClockMarkers:
    @staticmethod
    def _marker(name, period, wns, whs):
        def tok(v):
            return "NA" if v is None else f"{v:.6f}"

        return (
            f"STA_PERCLOCK: name={name} period_ns={period:.6f} "
            f"wns_ns={tok(wns)} whs_ns={tok(whs)}\n"
        )

    def test_parse_basic(self):
        from booley.yosys import syn_core

        rows = syn_core.parse_perclock(self._marker("clk", 2.0, 0.75, 0.1))
        assert rows == {"clk": {"period_ns": 2.0, "wns_ns": 0.75, "whs_ns": 0.1}}

    def test_parse_na_becomes_none(self):
        from booley.yosys import syn_core

        row = syn_core.parse_perclock(self._marker("clk", 2.0, None, None))["clk"]
        assert row["period_ns"] == 2.0
        assert row["wns_ns"] is None
        assert row["whs_ns"] is None

    def test_parse_dedup_keeps_min_slack(self):
        # A clock reported twice: the most pessimistic (minimum) slack wins.
        from booley.yosys import syn_core

        output = self._marker("clk", 2.0, 0.75, 0.30) + self._marker("clk", 2.0, 0.20, 0.05)
        row = syn_core.parse_perclock(output)["clk"]
        assert row["wns_ns"] == 0.20
        assert row["whs_ns"] == 0.05

    def test_parse_na_never_displaces_real_value(self):
        # A later NA must not overwrite an earlier real slack.
        from booley.yosys import syn_core

        output = self._marker("clk", 2.0, 0.75, 0.30) + self._marker("clk", 2.0, None, None)
        row = syn_core.parse_perclock(output)["clk"]
        assert row["wns_ns"] == 0.75
        assert row["whs_ns"] == 0.30

    def test_parse_multi_clock(self):
        from booley.yosys import syn_core

        output = self._marker("clk_a", 2.0, 0.5, 0.1) + self._marker("clk_b", 4.0, 1.0, 0.2)
        rows = syn_core.parse_perclock(output)
        assert set(rows) == {"clk_a", "clk_b"}
        assert rows["clk_b"]["period_ns"] == 4.0

    def test_emit_perclock_roundtrip(self, capsys):
        # Re-emitting parses the input and prints canonical marker lines that
        # parse back to the same values.
        from booley.yosys import syn_core

        stdout = self._marker("clk", 2.0, 0.75, 0.1)
        assert syn_core.emit_perclock_markers(stdout) is True
        out = capsys.readouterr().out
        assert syn_core.parse_perclock(out) == {
            "clk": {"period_ns": 2.0, "wns_ns": 0.75, "whs_ns": 0.1},
        }

    def test_emit_perclock_na_roundtrips_as_na(self, capsys):
        from booley.yosys import syn_core

        assert syn_core.emit_perclock_markers(self._marker("clk", 2.0, None, None)) is True
        out = capsys.readouterr().out
        assert "wns_ns=NA" in out and "whs_ns=NA" in out

    def test_emit_perclock_noop_when_absent(self, capsys):
        from booley.yosys import syn_core

        assert syn_core.emit_perclock_markers("no markers here") is False
        assert capsys.readouterr().out == ""

    def test_parse_sdc_clock_names_in_order(self):
        from booley.yosys import syn_core

        text = (
            "create_clock -name clk_core -period 2.0 [get_ports clk]\n"
            "# create_clock -name commented_out -period 1.0\n"
            "create_clock -name clk_io -period 5.0 [get_ports pclk]\n"
        )
        assert syn_core.parse_sdc_clock_names(text) == ["clk_core", "clk_io"]


# ---------------------------------------------------------------------------
# run_sv2v — subprocess mocked
# ---------------------------------------------------------------------------


class TestRunSv2v:
    @patch("booley.yosys.syn_core.run_cmd")
    @patch("booley.yosys.syn_core.find_eda_tool")
    def test_basic_command(self, mock_find, mock_run, tmp_path):
        from booley.yosys import syn_core

        mock_find.return_value = Path("/usr/bin/sv2v")
        mock_run.return_value = MagicMock(returncode=0)

        result = syn_core.run_sv2v(
            files=[Path("a.sv"), Path("b.sv")],
            inc_dirs=[Path("/inc")],
            defines=["SIM"],
            work_dir=tmp_path,
        )
        assert result == tmp_path / "sv2v_converted.v"
        cmd = mock_run.call_args[0][0]
        assert "sv2v" in cmd[0]
        assert any("-I" in str(c) for c in cmd)
        assert any("-DSIM" in str(c) for c in cmd)

    @patch("booley.yosys.syn_core.find_eda_tool")
    def test_sv2v_not_found_exits(self, mock_find):
        from booley.yosys import syn_core

        mock_find.return_value = None
        with pytest.raises(SystemExit):
            syn_core.run_sv2v([], [], [], Path("/tmp"))


# ---------------------------------------------------------------------------
# run_cmd (non-watched)
# ---------------------------------------------------------------------------


class TestSynCoreRunCmd:
    @patch("booley.yosys.syn_core.subprocess.run")
    def test_success(self, mock_run, tmp_path):
        from booley.yosys import syn_core

        mock_run.return_value = MagicMock(returncode=0)
        result = syn_core.run_cmd(["echo"], "test", work_dir=tmp_path)
        assert result.returncode == 0

    @patch("booley.yosys.syn_core.subprocess.run")
    def test_failure_exits(self, mock_run, tmp_path):
        from booley.yosys import syn_core

        mock_run.return_value = MagicMock(returncode=1)
        with pytest.raises(SystemExit):
            syn_core.run_cmd(["fail"], "test fail", work_dir=tmp_path)

    @patch("booley.yosys.syn_core.subprocess.run")
    def test_log_file(self, mock_run, tmp_path):
        from booley.yosys import syn_core

        mock_run.return_value = MagicMock(returncode=0)
        syn_core.run_cmd(["echo"], "test", work_dir=tmp_path, log_file="test.log")
        # Should have used stdout=file
        # Verify the call happened with a file handle for stdout
        assert mock_run.called

    @patch("booley.yosys.syn_core.subprocess.run")
    def test_rc_clamped_to_255(self, mock_run, tmp_path):
        """Return codes > 255 should be clamped to 1 on POSIX."""
        from booley.yosys import syn_core

        mock_run.return_value = MagicMock(returncode=256)
        with pytest.raises(SystemExit) as exc_info:
            syn_core.run_cmd(["bad"], "test", work_dir=tmp_path)
        # Should clamp to 1, not 256 (which wraps to 0 on POSIX)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# synth_timing_config — TOML boundary validation (Principle 5)
# ---------------------------------------------------------------------------


class TestSynthTimingConfigTomlBoundary:
    @staticmethod
    def _with_timing(monkeypatch, timing: dict) -> None:
        """Make _load_rtl_config return a config carrying the given timing dict."""
        cfg = {"flows": {"synth": {"timing": timing}}}
        # Accepts the optional project_root the ADR 0037 in-process configure
        # half threads through (None on the legacy CLI path exercised here).
        monkeypatch.setattr(
            "booley.runtime.shared_infra._load_rtl_config",
            lambda project_root=None: cfg,
        )

    def test_absent_keys_use_defaults(self, monkeypatch):
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {"engine": "opensta"})
        cfg = syn_core.synth_timing_config()
        assert cfg.period_ps == syn_core.DEFAULT_STA_PERIOD_PS
        assert cfg.input_delay_pct == syn_core.DEFAULT_STA_INPUT_DELAY_PCT
        assert cfg.output_delay_pct == syn_core.DEFAULT_STA_OUTPUT_DELAY_PCT

    def test_cli_scalars_used(self, monkeypatch):
        # Design-constraint scalars (period / I-O delays) now arrive only via
        # the trusted argparse-typed CLI (standalone run_yosys_syn use); there
        # is no booley.toml source for them anymore (ADR 0029).
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {"engine": "opensta"})
        cfg = syn_core.synth_timing_config(
            period_ps=2500.0,
            input_delay_pct=25.0,
            output_delay_pct=60.0,
        )
        assert cfg.period_ps == 2500.0
        assert cfg.input_delay_pct == 25.0
        assert cfg.output_delay_pct == 60.0

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("period_ps", 2500),
            ("clock", "clk_i"),
            ("input_delay_pct", 25),
            ("output_delay_pct", 60),
            ("sdc", "false_paths.sdc"),
        ],
    )
    def test_migrated_constraint_key_is_hard_error(self, monkeypatch, key, value):
        # ADR 0029 decision 3 (hard cutoff): a design-constraint key still set in
        # booley.toml is a setup-time error naming the migration, not a silent
        # second source of truth.
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {key: value})
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config()
        msg = str(exc_info.value)
        assert key in msg
        assert "file_type: SDC" in msg

    def test_cli_sdc_list_resolves_against_project_root(self, monkeypatch, tmp_path):
        from booley.yosys import syn_core

        # A relative --sta-sdc path resolves against PROJECT_ROOT (the worktree /
        # sandbox /work), not the process cwd — mirroring --inc-dir/--extra-rtl.
        sdc_file = tmp_path / "constraints" / "false_paths.sdc"
        sdc_file.parent.mkdir()
        sdc_file.write_text("# custom\n", encoding="utf-8")
        monkeypatch.setattr(syn_core, "PROJECT_ROOT", tmp_path)
        self._with_timing(monkeypatch, {})
        cfg = syn_core.synth_timing_config(sdc=["constraints/false_paths.sdc"])
        assert cfg.sdc == (sdc_file.resolve(),)

    def test_cli_sdc_list_preserves_order(self, monkeypatch, tmp_path):
        from booley.yosys import syn_core

        # Multiple SDCs concatenate in fileset (list) order, last-wins.
        first = tmp_path / "a.sdc"
        first.write_text("# a\n", encoding="utf-8")
        second = tmp_path / "b.sdc"
        second.write_text("# b\n", encoding="utf-8")
        self._with_timing(monkeypatch, {})
        cfg = syn_core.synth_timing_config(sdc=[str(first), str(second)])
        assert cfg.sdc == (first.resolve(), second.resolve())

    def test_missing_cli_sdc_file_raises_clear_error(self, monkeypatch):
        from booley.yosys import syn_core

        # A silently-ignored bad path is the worst outcome; error loudly instead.
        self._with_timing(monkeypatch, {})
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config(sdc=["/nope/does_not_exist.sdc"])
        assert "does_not_exist.sdc" in str(exc_info.value)

    def test_unknown_timing_key_warns(self, monkeypatch, capsys):
        from booley.yosys import syn_core

        # A typo'd knob (here ``reepair_timing``) is silently ignored by design —
        # so it must at least surface a warning, not vanish.
        self._with_timing(
            monkeypatch,
            {"engine": "opensta", "reepair_timing": True},
        )
        syn_core.synth_timing_config()
        out = capsys.readouterr().out
        assert "unknown key 'reepair_timing'" in out
        assert "valid keys" in out

    def test_known_timing_keys_do_not_warn(self, monkeypatch, capsys):
        from booley.yosys import syn_core

        self._with_timing(
            monkeypatch,
            {"engine": "opensta", "utilization_pct": 55, "repair_timing": False},
        )
        syn_core.synth_timing_config()
        assert "unknown key" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# synth_timing_config — OpenROAD engine default + knobs + sandbox gating
# ---------------------------------------------------------------------------


class TestSynthTimingConfigOpenRoad:
    @staticmethod
    def _with_timing(monkeypatch, timing: dict) -> None:
        cfg = {"flows": {"synth": {"timing": timing}}}
        # Accepts the optional project_root the ADR 0037 in-process configure
        # half threads through (None on the legacy CLI path exercised here).
        monkeypatch.setattr(
            "booley.runtime.shared_infra._load_rtl_config",
            lambda project_root=None: cfg,
        )

    @staticmethod
    def _in_sandbox(monkeypatch) -> None:
        """Pretend we're inside the container so OpenROAD isn't host-gated away."""
        from booley.yosys import syn_core

        monkeypatch.setattr(syn_core, "_in_container", lambda: True)

    def test_openroad_is_default_engine_in_sandbox(self, monkeypatch):
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {})
        self._in_sandbox(monkeypatch)
        cfg = syn_core.synth_timing_config()
        assert cfg.engine == "openroad"
        # Defaulted OpenROAD knobs.
        assert cfg.utilization_pct == syn_core.DEFAULT_STA_UTILIZATION_PCT
        assert cfg.repair_timing is True

    def test_openroad_accepted_explicitly(self, monkeypatch):
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {"engine": "openroad"})
        self._in_sandbox(monkeypatch)
        assert syn_core.synth_timing_config().engine == "openroad"

    def test_host_falls_back_to_opensta(self, monkeypatch, capsys):
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {})
        monkeypatch.setattr(syn_core, "_in_container", lambda: False)
        cfg = syn_core.synth_timing_config()
        assert cfg.engine == "opensta"
        assert "sandbox-only" in capsys.readouterr().out

    def test_invalid_engine_lists_three_engines(self, monkeypatch):
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {"engine": "bogus"})
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config()
        msg = str(exc_info.value)
        assert "openroad" in msg and "opensta" in msg and "none" in msg

    def test_utilization_and_repair_timing_from_toml(self, monkeypatch):
        from booley.yosys import syn_core

        self._with_timing(
            monkeypatch,
            {"utilization_pct": 55, "repair_timing": False},
        )
        self._in_sandbox(monkeypatch)
        cfg = syn_core.synth_timing_config()
        assert cfg.utilization_pct == 55.0
        assert cfg.repair_timing is False

    def test_cli_overrides_win(self, monkeypatch):
        from booley.yosys import syn_core

        self._with_timing(
            monkeypatch,
            {"utilization_pct": 55, "repair_timing": True},
        )
        self._in_sandbox(monkeypatch)
        cfg = syn_core.synth_timing_config(utilization_pct=70.0, repair_timing=False)
        assert cfg.utilization_pct == 70.0
        assert cfg.repair_timing is False

    def test_non_bool_repair_timing_raises(self, monkeypatch):
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {"repair_timing": "yes"})
        self._in_sandbox(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config()
        assert "repair_timing" in str(exc_info.value)

    def test_non_numeric_utilization_raises(self, monkeypatch):
        from booley.yosys import syn_core

        self._with_timing(monkeypatch, {"utilization_pct": "high"})
        self._in_sandbox(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config()
        assert "utilization_pct" in str(exc_info.value)


# ---------------------------------------------------------------------------
# run_yosys dispatch — OpenROAD with OpenSTA fallback
# ---------------------------------------------------------------------------


class TestRunYosysDispatch:
    @staticmethod
    def _config(engine: str):
        from booley.yosys import syn_core

        return syn_core.StaTimingConfig(
            engine=engine,
            clock="clk_i",
            period_ps=4000.0,
            input_delay_pct=30.0,
            output_delay_pct=70.0,
            sdc=(),
        )

    @patch("booley.yosys.syn_core.run_opensta")
    @patch("booley.yosys.openroad_timing.run_openroad_timing")
    @patch("booley.yosys.syn_core.run_cmd_watched")
    @patch("booley.yosys.syn_core.find_eda_tool")
    def test_openroad_success_no_fallback(
        self,
        mock_find,
        mock_run,
        mock_openroad,
        mock_opensta,
        tmp_path,
    ):
        from booley.yosys import syn_core

        mock_find.return_value = Path("/usr/bin/yosys")
        mock_run.return_value = MagicMock(watchdog_result="wd")
        mock_openroad.return_value = True
        syn_core.run_yosys(
            [tmp_path / "in.v"],
            "top",
            Path("/lib.lib"),
            tmp_path,
            timing_config=self._config("openroad"),
        )
        mock_openroad.assert_called_once()
        mock_opensta.assert_not_called()

    @patch("booley.yosys.syn_core.run_opensta")
    @patch("booley.yosys.openroad_timing.run_openroad_timing")
    @patch("booley.yosys.syn_core.run_cmd_watched")
    @patch("booley.yosys.syn_core.find_eda_tool")
    def test_openroad_failure_falls_back_to_opensta(
        self,
        mock_find,
        mock_run,
        mock_openroad,
        mock_opensta,
        tmp_path,
    ):
        from booley.yosys import syn_core

        mock_find.return_value = Path("/usr/bin/yosys")
        mock_run.return_value = MagicMock(watchdog_result="wd")
        mock_openroad.return_value = False  # unavailable / failed
        syn_core.run_yosys(
            [tmp_path / "in.v"],
            "top",
            Path("/lib.lib"),
            tmp_path,
            timing_config=self._config("openroad"),
        )
        mock_openroad.assert_called_once()
        mock_opensta.assert_called_once()

    @patch("booley.yosys.syn_core.run_opensta")
    @patch("booley.yosys.openroad_timing.run_openroad_timing")
    @patch("booley.yosys.syn_core.run_cmd_watched")
    @patch("booley.yosys.syn_core.find_eda_tool")
    def test_opensta_engine_skips_openroad(
        self,
        mock_find,
        mock_run,
        mock_openroad,
        mock_opensta,
        tmp_path,
    ):
        from booley.yosys import syn_core

        mock_find.return_value = Path("/usr/bin/yosys")
        mock_run.return_value = MagicMock(watchdog_result="wd")
        syn_core.run_yosys(
            [tmp_path / "in.v"],
            "top",
            Path("/lib.lib"),
            tmp_path,
            timing_config=self._config("opensta"),
        )
        mock_openroad.assert_not_called()
        mock_opensta.assert_called_once()


class TestSlangReadCommandOptions:
    """Target flow_options.slang_options tokens reach the read_slang line."""

    def test_slang_options_appended_verbatim_before_files(self):
        """Extra tokens (e.g. --single-unit) land after the generated options.

        Regression (ravenoc): slang's per-file compilation units break the
        "defines header included once, macros leak across the filelist"
        convention; without an option passthrough the slang frontend was
        unusable on such repos.
        """
        from booley.yosys import syn_core

        cmd = syn_core._slang_read_command(
            [Path("/work/a.sv"), Path("/work/b.sv")],
            "dut",
            [Path("/work/inc")],
            ["SYNTHESIS"],
            None,
            ["--single-unit", "--allow-use-before-declare"],
        )
        assert cmd.startswith("read_slang --top dut ")
        assert "-I /work/inc -D SYNTHESIS --single-unit --allow-use-before-declare" in cmd
        # tokens precede the source files
        assert cmd.index("--single-unit") < cmd.index("/work/a.sv")

    def test_no_slang_options_leaves_command_unchanged(self):
        """Default (None) produces the exact pre-knob command shape."""
        from booley.yosys import syn_core

        cmd = syn_core._slang_read_command([Path("/work/a.sv")], "dut", [], [], None)
        assert cmd == "read_slang --top dut /work/a.sv"


class TestSlangFrontendGuard:
    """--frontend slang preflight: fail fast on a Yosys without read_slang."""

    def test_has_read_slang_true(self):
        from unittest.mock import MagicMock

        from booley.yosys import syn_core

        with patch("booley.yosys.syn_core.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="\n    read_slang [options] [filename]\n", stderr=""
            )
            assert syn_core._yosys_has_read_slang("/usr/local/bin/yosys") is True

    def test_has_read_slang_false_on_no_such_command(self):
        from unittest.mock import MagicMock

        from booley.yosys import syn_core

        with patch("booley.yosys.syn_core.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="", stderr="No such command or cell type: read_slang\n"
            )
            assert syn_core._yosys_has_read_slang("/usr/bin/yosys") is False

    def test_has_read_slang_false_on_probe_error(self):
        from booley.yosys import syn_core

        with patch("booley.yosys.syn_core.subprocess.run", side_effect=OSError):
            assert syn_core._yosys_has_read_slang("/usr/bin/yosys") is False

    @patch("booley.yosys.syn_core._yosys_has_read_slang", return_value=False)
    @patch("booley.yosys.syn_core.find_eda_tool", return_value=Path("/usr/bin/yosys"))
    def test_slang_without_read_slang_hard_errors(self, _mock_find, _mock_probe, tmp_path):
        from booley.yosys import syn_core

        with pytest.raises(SystemExit, match=r"--frontend slang needs Yosys >= 0.67"):
            syn_core.run_yosys(
                [tmp_path / "a.sv"],
                "top",
                Path("/lib.lib"),
                tmp_path,
                frontend="slang",
            )

    @patch("booley.yosys.syn_core.run_opensta")
    @patch("booley.yosys.openroad_timing.run_openroad_timing")
    @patch("booley.yosys.syn_core.run_cmd_watched")
    @patch("booley.yosys.syn_core._yosys_has_read_slang", return_value=True)
    @patch("booley.yosys.syn_core.find_eda_tool", return_value=Path("/usr/bin/yosys"))
    def test_slang_with_read_slang_proceeds(
        self, _mock_find, _mock_probe, mock_run, _mock_or, _mock_sta, tmp_path
    ):
        from booley.yosys import syn_core

        mock_run.return_value = MagicMock(watchdog_result="wd")
        # No SystemExit: the guard passes and synthesis is dispatched.
        syn_core.run_yosys(
            [tmp_path / "a.sv"],
            "top",
            Path("/lib.lib"),
            tmp_path,
            frontend="slang",
        )
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# ADR 0029 — Target-SDC period recovery + generated-default suppression
# ---------------------------------------------------------------------------


def _sdc_config(tmp_path, *sdc_texts, period_ps=4000.0):
    """A StaTimingConfig whose SDC fileset is the given text(s), on disk."""
    from booley.yosys import syn_core

    paths = []
    for i, text in enumerate(sdc_texts):
        p = tmp_path / f"user_{i}.sdc"
        p.write_text(text, encoding="utf-8")
        paths.append(p)
    return syn_core.StaTimingConfig(
        engine="opensta",
        clock="clk_i",
        period_ps=period_ps,
        input_delay_pct=30.0,
        output_delay_pct=70.0,
        sdc=tuple(paths),
    )


class TestSdcClockPeriodParsing:
    def test_single_clock_ns_to_ps(self):
        from booley.yosys import syn_core

        assert syn_core.parse_sdc_clock_periods_ps(
            "create_clock -name clk -period 25 [get_ports clk]"
        ) == [25000.0]

    def test_decimal_period(self):
        from booley.yosys import syn_core

        assert syn_core.parse_sdc_clock_periods_ps("create_clock -period 3.5 [get_ports clk]") == [
            3500.0
        ]

    def test_multiple_clocks_in_order(self):
        from booley.yosys import syn_core

        text = (
            "create_clock -name a -period 10 [get_ports a]\n"
            "create_clock -name b -period 4 [get_ports b]\n"
        )
        assert syn_core.parse_sdc_clock_periods_ps(text) == [10000.0, 4000.0]

    def test_commented_line_ignored(self):
        from booley.yosys import syn_core

        text = (
            "# create_clock -name old -period 999 [get_ports x]\n"
            "create_clock -period 8 [get_ports clk]\n"
        )
        assert syn_core.parse_sdc_clock_periods_ps(text) == [8000.0]

    def test_no_period_yields_empty(self):
        from booley.yosys import syn_core

        assert syn_core.parse_sdc_clock_periods_ps("set_false_path -from x") == []

    def test_sta_clock_period_marker(self):
        from booley.yosys import syn_core

        assert syn_core.parse_sta_clock_period_ps("STA_CLOCK_PERIOD_NS: 4.0") == 4000.0
        assert syn_core.parse_sta_clock_period_ps("nope") is None


class TestEffectivePeriod:
    def test_no_sdc_uses_config_period(self, tmp_path):
        # Bit-identical to pre-0029: no authored clock → the config scalar.
        from booley.yosys import syn_core

        cfg = syn_core.StaTimingConfig(
            engine="opensta",
            clock="clk_i",
            period_ps=4000.0,
            input_delay_pct=30.0,
            output_delay_pct=70.0,
            sdc=(),
        )
        assert syn_core.effective_period_ps(cfg, "irrelevant stdout") == 4000.0

    def test_sdc_owned_clock_wins(self, tmp_path):
        from booley.yosys import syn_core

        cfg = _sdc_config(
            tmp_path,
            "create_clock -period 25 [get_ports clk_i]",
            period_ps=4000.0,
        )
        # SDC declares 25 ns; the config scalar (4000 ps) must be ignored.
        assert syn_core.effective_period_ps(cfg, "") == 25000.0

    def test_tightest_of_multiple_clocks(self, tmp_path):
        from booley.yosys import syn_core

        cfg = _sdc_config(
            tmp_path,
            "create_clock -name a -period 10 [get_ports a]\n"
            "create_clock -name b -period 4 [get_ports b]\n",
        )
        assert syn_core.effective_period_ps(cfg, "") == 4000.0

    def test_falls_back_to_sta_marker_when_no_period(self, tmp_path):
        # Authored clock but no parseable -period → STA-reported period (prio 2).
        from booley.yosys import syn_core

        cfg = _sdc_config(tmp_path, "create_clock -name clk_i [get_ports clk_i]")
        assert syn_core.effective_period_ps(cfg, "STA_CLOCK_PERIOD_NS: 6.0") == 6000.0

    def test_concatenates_sdc_files_in_order(self, tmp_path):
        from booley.yosys import syn_core

        cfg = _sdc_config(
            tmp_path,
            "set_false_path -from x\n",
            "create_clock -period 12 [get_ports clk_i]\n",
        )
        text = syn_core.read_user_sdc_text(cfg)
        assert "set_false_path" in text and "create_clock" in text
        assert syn_core.effective_period_ps(cfg, "") == 12000.0


class TestWriteStaSdcSuppression:
    def test_no_sdc_emits_full_generated_block(self, tmp_path):
        from booley.yosys import syn_core

        cfg = syn_core.StaTimingConfig(
            engine="opensta",
            clock="clk_i",
            period_ps=2000.0,
            input_delay_pct=25.0,
            output_delay_pct=60.0,
            sdc=(),
        )
        text = syn_core.write_sta_sdc(cfg, "clk_i", tmp_path).read_text()
        assert "create_clock -name clk_i" in text
        assert "set_input_delay" in text
        assert "set_output_delay" in text

    def test_owned_clock_suppresses_generated_clock(self, tmp_path):
        from booley.yosys import syn_core

        cfg = _sdc_config(tmp_path, "create_clock -period 25 [get_ports clk_i]\n")
        text = syn_core.write_sta_sdc(cfg, "clk_i", tmp_path).read_text()
        # The authored clock is present; no generated -name default is added.
        assert "create_clock -period 25" in text
        assert "create_clock -name clk_i" not in text
        # I/O delays are still generated (the SDC owns only the clock).
        assert "set_input_delay" in text
        assert "set_output_delay" in text

    def test_owned_io_suppresses_generated_delays(self, tmp_path):
        from booley.yosys import syn_core

        cfg = _sdc_config(
            tmp_path,
            "set_input_delay -clock clk_i 0.1 [all_inputs]\n"
            "set_output_delay -clock clk_i 0.2 [all_outputs]\n",
        )
        text = syn_core.write_sta_sdc(cfg, "clk_i", tmp_path).read_text()
        # No generated delay lines beyond the authored ones; the $input_ports
        # helper + set_driving_cell (which needs it) are suppressed too.
        assert "$input_ports" not in text
        assert "set_driving_cell" not in text
        # The clock was not authored, so it is still generated.
        assert "create_clock -name clk_i" in text

    def test_fully_owned_suppresses_all_generated(self, tmp_path):
        from booley.yosys import syn_core

        cfg = _sdc_config(
            tmp_path,
            "create_clock -period 25 [get_ports clk_i]\n"
            "set_input_delay -clock clk_i 0 [all_inputs]\n"
            "set_output_delay -clock clk_i 0 [all_outputs]\n",
        )
        text = syn_core.write_sta_sdc(cfg, "clk_i", tmp_path).read_text()
        assert "create_clock -name clk_i" not in text
        assert "$input_ports" not in text
        assert "set_driving_cell" not in text


class TestFormalCellRemoval:
    """`chformal -remove` on the slang path (ravenoc F-30).

    yosys-slang *lowers* SVA into `$check` cells (sv2v strips assertions
    instead). Left in the design they reach ABC and `stat` ("Area for cell type
    $check is unknown!") and, worse, `write_verilog` emits them into the STA
    netlist where OpenSTA's structural parser aborts — a synthesis that exits 0
    with an undercounted area and no timing at all.
    """

    @staticmethod
    def _script(frontend: str, **kw) -> str:
        from booley.yosys import syn_core

        return syn_core._build_yosys_script(
            [Path("/work/a.sv")],
            "dut",
            Path("/pdk/lib.lib"),
            Path("/work/out"),
            False,
            None,
            None,
            frontend=frontend,
            **kw,
        )

    def test_slang_script_removes_formal_cells(self):
        script = self._script("slang")
        assert "chformal -remove" in script

    def test_chformal_runs_after_proc_and_before_synth(self):
        """Placement is load-bearing: `proc` lowers the last procedural
        assertions into cells, and the `synth` pass (which runs opt and techmap
        internally) is what chokes on them."""
        script = self._script("slang")
        assert script.index("; proc") < script.index("chformal -remove")
        assert script.index("chformal -remove") < script.index("synth -top")

    def test_sv2v_script_unchanged(self):
        """sv2v strips SVA and plain read_verilog never makes a formal cell —
        so the sv2v script (and every area number taken through it) is
        untouched."""
        assert "chformal" not in self._script("sv2v")


class TestUnknownAreaCauseNaming:
    """A stat "unknown area" line must name its likely cause (F-30)."""

    @staticmethod
    def _yosys_log(tmp_path, text: str) -> Path:
        (tmp_path / "yosys.log").write_text(text, encoding="utf-8")
        return tmp_path

    def test_formal_cell_names_the_assertion_cause(self, tmp_path):
        from booley.yosys import syn_core

        work = self._yosys_log(tmp_path, "Area for cell type $check is unknown!\n")
        line = syn_core.scan_synth_logs(work)
        assert line is not None
        assert "Area for cell type $check is unknown!" in line
        assert "assertion/formal cell" in line
        assert "chformal -remove" in line
        # The downstream symptom the user actually saw is named too.
        assert "OpenSTA" in line

    def test_ordinary_cell_names_the_mapping_cause(self, tmp_path):
        from booley.yosys import syn_core

        work = self._yosys_log(tmp_path, "Area for cell type $_DFF_PN0_ is unknown!\n")
        line = syn_core.scan_synth_logs(work)
        assert line is not None
        assert "ZERO area" in line
        assert "assertion/formal" not in line

    def test_benign_metadata_cell_still_passes(self, tmp_path):
        from booley.yosys import syn_core

        work = self._yosys_log(tmp_path, "Area for cell type $scopeinfo is unknown!\n")
        assert syn_core.scan_synth_logs(work) is None


class TestStaParseAbortHint:
    """An OpenSTA netlist parse abort must name the cause, not just go quiet."""

    def test_syntax_error_line_is_quoted_with_a_cause(self):
        from booley.yosys import syn_core

        hint = syn_core.sta_parse_abort_hint(
            "Warning: liberty stuff\nError: sta_ravenoc.v line 250778, syntax error\n",
            "ravenoc",
        )
        assert hint is not None
        assert "sta_ravenoc.v" in hint
        assert "line 250778, syntax error" in hint
        assert "$check" in hint  # the usual culprit is named

    def test_clean_log_yields_no_hint(self):
        """No parse error means the empty timing has some other cause — don't
        fabricate a diagnosis."""
        from booley.yosys import syn_core

        assert syn_core.sta_parse_abort_hint("all good\n", "dut") is None


class TestElaborateScript:
    """`build_elaborate_script` — elaborate's ASIC path, both frontends (F-31)."""

    def test_slang_reuses_the_synthesis_read_line_and_stops_before_techmap(self):
        from booley.yosys import syn_core

        script = syn_core.build_elaborate_script(
            [Path("rtl/a.sv"), Path("rtl/b.sv")],
            "dut",
            frontend="slang",
            inc_dirs=[Path("rtl/inc")],
            defines=["NO_ASSERTIONS"],
            params={"N": "4"},
            slang_options=["--single-unit"],
        )
        assert script.startswith("read_slang --top dut ")
        assert "-I rtl/inc" in script
        assert "-D NO_ASSERTIONS" in script
        assert "-G N=4" in script
        assert "--single-unit" in script
        assert "hierarchy -check -top dut" in script
        # Elaboration only: no tech-mapping, no netlist, no liberty.
        for forbidden in ("techmap", "dfflibmap", "abc ", "write_verilog", "stat"):
            assert forbidden not in script

    def test_sv2v_reads_the_transpiled_file_and_applies_params(self):
        """The sv2v frontend elaborates one already-transpiled Verilog file,
        with parameter overrides applied Yosys-side (chparam), exactly as the
        synthesis script does."""
        from booley.yosys import syn_core

        script = syn_core.build_elaborate_script(
            [Path("build/sv2v_converted.v")],
            "dut",
            frontend="sv2v",
            params={"N": "4"},
        )
        assert script.startswith("read_verilog build/sv2v_converted.v;")
        assert "chparam -set N 4 dut" in script
        assert "hierarchy -libdir ./ -check -top dut" in script
        for forbidden in ("techmap", "dfflibmap", "read_slang", "write_verilog"):
            assert forbidden not in script

    def test_sv2v_is_the_default_frontend(self):
        from booley.yosys import syn_core

        assert syn_core.build_elaborate_script([Path("x.v")], "dut").startswith("read_verilog ")

    def test_defaults_need_no_optional_inputs(self):
        from booley.yosys import syn_core

        script = syn_core.build_elaborate_script([Path("a.sv")], "dut", frontend="slang")
        assert script == (
            "read_slang --top dut a.sv; hierarchy -check -top dut; "
            "proc; chformal -remove; check -noinit"
        )

    def test_prefix_is_shared_with_the_synthesis_script(self):
        """The anti-drift guarantee: the elaborate script is literally the head
        of the synthesis script (minus the trailing `check -noinit`)."""
        from booley.yosys import syn_core

        args = ([Path("rtl/a.sv")], "dut")
        kwargs = {"inc_dirs": [Path("rtl/inc")], "defines": ["SYNTHESIS"], "params": {"N": "4"}}
        elab = syn_core.build_elaborate_script(*args, frontend="slang", **kwargs)
        synth = syn_core._build_yosys_script(
            *args,
            Path("/pdk/lib.lib"),
            Path("/work/out"),
            False,
            kwargs["params"],
            None,
            frontend="slang",
            inc_dirs=kwargs["inc_dirs"],
            defines=kwargs["defines"],
        )
        assert synth.startswith(elab.removesuffix("; check -noinit"))


class TestSv2vArgv:
    """`sv2v_argv` — one transpile command line for every caller (F-31)."""

    def test_shape(self):
        from booley.yosys import syn_core

        argv = syn_core.sv2v_argv(
            [Path("rtl/pkg.sv"), Path("rtl/dut.sv")],
            [Path("rtl/inc")],
            ["SYNTHESIS", "WIDTH=8"],
            Path("build/sv2v_converted.v"),
        )
        assert argv == [
            "sv2v",
            "-Irtl/inc",
            "-DSYNTHESIS",
            "-DWIDTH=8",
            "rtl/pkg.sv",
            "rtl/dut.sv",
            "-w",
            "build/sv2v_converted.v",
        ]

    def test_params_are_not_passed_to_sv2v(self):
        """Parameter overrides belong to Yosys (chparam) — passing them here
        too would double-apply them."""
        from booley.yosys import syn_core

        argv = syn_core.sv2v_argv([Path("a.sv")], [], [], "out.v")
        assert not [a for a in argv if a.startswith("-P") or a.startswith("-G")]


class TestEffectiveParameters:
    """Post-elaboration evidence and enabled-define classification (F-13)."""

    def test_parses_yosys_module_header(self):
        from booley.yosys import syn_core

        text = """\
module \\dut
  parameter \\ENABLE_ZBB 1'0
  parameter \\WIDTH 32'00000000000000000000000000001000
end
"""
        assert syn_core.parse_effective_parameters(text) == {
            "ENABLE_ZBB": "1'0",
            "WIDTH": "32'00000000000000000000000000001000",
        }

    def test_only_integral_nonzero_or_bare_defines_are_enabled(self):
        from booley.yosys import syn_core

        assert syn_core.enabled_define_names(
            [
                "BARE",
                "ONE=1",
                "HEX=4'hA",
                "FALSE=0",
                "ZERO=8'b0000_0000",
                "UNKNOWN=4'bx001",
                'TEXT="fast"',
            ]
        ) == ("BARE", "ONE", "HEX")


class TestFrontendKnobResolution:
    """`resolve_frontend` / `resolve_slang_options` — one reader for both Flows."""

    def test_absent_knob_is_none(self):
        from booley.yosys import syn_core

        assert syn_core.resolve_frontend({}) is None

    def test_cli_override_wins(self):
        from booley.yosys import syn_core

        assert syn_core.resolve_frontend({"frontend": "sv2v"}, override="slang") == "slang"

    def test_unknown_frontend_rejected(self):
        import pytest

        from booley.core.boundary import BoundaryError
        from booley.yosys import syn_core

        with pytest.raises(BoundaryError, match="must be one of"):
            syn_core.resolve_frontend({"frontend": "verific"})

    def test_slang_options_absent_is_empty(self):
        from booley.yosys import syn_core

        assert syn_core.resolve_slang_options({}) == []

    def test_slang_options_bare_string_rejected(self):
        """The TOML footgun: a bare string would iterate character-by-character."""
        import pytest

        from booley.core.boundary import BoundaryError
        from booley.yosys import syn_core

        with pytest.raises(BoundaryError, match="non-empty list"):
            syn_core.resolve_slang_options({"slang_options": "--single-unit"})
