"""Unit tests for synthesis configuration, script generation, and parsing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from booley.core.boundary import BoundaryError
from booley.flows.synth import timing as synth_timing
from booley.flows.synth.backends import pipeline as syn_make
from booley.flows.synth.backends.openroad import reporting as openroad_reporting
from booley.flows.synth.backends.pipeline import SynthSpec, configure_synthesis
from booley.flows.synth.backends.yosys import core as syn_core
from booley.flows.synth.mode import SynthMode
from booley.flows.synth.timing import OpenRoadPpaSettings, StaTimingConfig
from tests.conftest import symlink_or_skip
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child

_SYNTH_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _spec(
    *,
    liberty: Path = Path("/pdk/lib.lib"),
    design_name: str = "dut",
    frontend: str = "sv2v",
    flatten: bool = False,
    **overrides,
) -> SynthSpec:
    """Build a SynthSpec with inert defaults; tests override only what matters."""
    fields = {
        "design_name": design_name,
        "sources": (),
        "inc_dirs": (),
        "defines": (),
        "params": {},
        "liberty": liberty,
        "liberty_found": True,
        "flatten": flatten,
        "abc_recipe": None,
        "frontend": frontend,
        "timing": StaTimingConfig(mode=SynthMode.LOGICAL),
    }
    fields.update(overrides)
    return SynthSpec(**fields)


def _script(tmp_path: Path, **overrides) -> str:
    """The rendered ``synth.ys`` text for a logical spec built from *overrides*."""
    configure_synthesis(_spec(**overrides), tmp_path / "build")
    return (tmp_path / "build" / "synth.ys").read_text(encoding="utf-8")


def _run_yosys(
    tmp_path: Path, liberty_text: str, rtl: str, **overrides
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """Render the real ``synth.ys`` for *rtl*, run Yosys on it, return build dir and result.

    The sv2v frontend reads ``sv2v_converted.v``, so *rtl* is written there.
    """
    liberty = tmp_path / "toy.lib"
    liberty.write_text(liberty_text, encoding="utf-8")
    build_dir = tmp_path / "build"
    configure_synthesis(_spec(liberty=liberty, **overrides), build_dir)
    (build_dir / "sv2v_converted.v").write_text(rtl, encoding="utf-8")
    with syn_make.yosys_tmpdir() as yosys_tmp:
        result = subprocess.run(
            ["yosys", "-q", "-s", "synth.ys"],
            cwd=build_dir,
            env={**os.environ, "TMPDIR": str(yosys_tmp)},
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    return build_dir, result


# ---------------------------------------------------------------------------
# resolve_liberty
# ---------------------------------------------------------------------------


class TestResolveLiberty:
    def test_cli_path_exists(self, tmp_path):
        lib = tmp_path / "test.lib"
        lib.write_text("liberty", encoding="utf-8")
        assert syn_core.resolve_liberty(str(lib)) == lib

    def test_cli_path_missing_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            syn_core.resolve_liberty(str(tmp_path / "nonexistent.lib"))

    def test_env_prj_lib_dir(self, monkeypatch, tmp_path):
        lib_dir = tmp_path / "cell" / "lib"
        lib_dir.mkdir(parents=True)
        lib_file = lib_dir / "NangateOpenCellLibrary_typical_ccs.lib"
        lib_file.write_text("liberty", encoding="utf-8")
        monkeypatch.setenv("PRJ_LIB_DIR", str(tmp_path))
        assert syn_core.resolve_liberty(None) == lib_file

    def test_no_liberty_found_exits(self, monkeypatch):
        monkeypatch.delenv("PRJ_LIB_DIR", raising=False)
        # DEFAULT_LIBERTY lives in booley.flows.synth.backends.yosys.discovery
        # (re-exported by syn_core); patch it where resolve_liberty reads it.
        monkeypatch.setattr(
            "booley.flows.synth.backends.yosys.discovery.DEFAULT_LIBERTY", Path("/nonexistent/lib")
        )
        with pytest.raises(SystemExit):
            syn_core.resolve_liberty(None)


class TestNangateAbcPolicy:
    @pytest.mark.parametrize(
        "basename",
        [
            "NangateOpenCellLibrary_typical_ccs.lib",
            "NangateOpenCellLibrary_typical.lib",
        ],
    )
    def test_supported_nangate_libraries_exclude_multi_output_cells(self, tmp_path, basename):
        script = _script(tmp_path, liberty=Path("/pdk") / basename)
        assert "abc -liberty" in script
        assert "-dont_use FA_X1 -dont_use HA_X1" in script

    def test_unrelated_library_has_no_nangate_exclusions(self, tmp_path):
        script = _script(tmp_path, liberty=Path("/pdk/other.lib"))
        assert "-dont_use FA_X1" not in script
        assert "-dont_use HA_X1" not in script


@pytest.mark.skipif(shutil.which("yosys") is None, reason="requires the pinned Yosys executable")
class TestFinalCheckImportIsolation:
    _LIBERTY = """library(test) {
  cell(BUF_X1) {
    area: 1;
    pin(A) { direction: input; }
    pin(Z) { direction: output; function: \"A\"; }
  }
}
"""

    def test_existing_library_cell_module_is_preserved(self, tmp_path):
        build_dir, result = _run_yosys(
            tmp_path,
            self._LIBERTY,
            "module BUF_X1(input A, output Z); assign Z = A; endmodule\n"
            "module dut(input A, output Z); BUF_X1 u_buf(.A(A), .Z(Z)); endmodule\n",
        )
        assert result.returncode == 0, result.stderr
        assert "module BUF_X1" in (build_dir / "synth_dut.v").read_text(encoding="utf-8")

    def test_reserved_attribute_collision_fails_before_deletion(self, tmp_path):
        _build_dir, result = _run_yosys(
            tmp_path,
            self._LIBERTY,
            "(* booley_check_library = 1 *) module marker(input A, output Z); "
            "assign Z = A; endmodule\n"
            "module dut(input A, output Z); marker u_marker(.A(A), .Z(Z)); endmodule\n",
        )
        assert result.returncode != 0
        assert "Assertion failed" in result.stderr


#: A toy Liberty that ABC can map multi-bit logic to; the buffer cell is required.
_MAPPING_LIBERTY = """library(test) {
  cell(INV_X1) {
    area: 1;
    pin(A) { direction: input; capacitance: 1; }
    pin(ZN) { direction: output; function: \"!A\"; }
  }
  cell(NAND2_X1) {
    area: 2;
    pin(A1) { direction: input; capacitance: 1; }
    pin(A2) { direction: input; capacitance: 1; }
    pin(ZN) { direction: output; function: \"!(A1 & A2)\"; }
  }
  cell(NOR2_X1) {
    area: 2;
    pin(A1) { direction: input; capacitance: 1; }
    pin(A2) { direction: input; capacitance: 1; }
    pin(ZN) { direction: output; function: \"!(A1 | A2)\"; }
  }
  cell(AND2_X1) {
    area: 3;
    pin(A1) { direction: input; capacitance: 1; }
    pin(A2) { direction: input; capacitance: 1; }
    pin(ZN) { direction: output; function: \"A1 & A2\"; }
  }
  cell(BUF_X1) {
    area: 2;
    pin(A) { direction: input; capacitance: 1; }
    pin(Z) { direction: output; function: \"A\"; }
  }
}
"""


@pytest.mark.skipif(shutil.which("yosys") is None, reason="requires the pinned Yosys executable")
class TestAbcDelayTargetReachesMapping:
    """The generated ABC script must use a placeholder spelling Yosys really expands."""

    _LIBERTY = _MAPPING_LIBERTY

    @pytest.mark.parametrize("abc_recipe", ["fast", "balanced"])
    def test_delay_target_is_passed_to_abc_map(self, tmp_path, abc_recipe):
        build_dir, result = _run_yosys(
            tmp_path,
            self._LIBERTY,
            "module dut(input a, input b, output y); assign y = a & b; endmodule\n",
            abc_recipe=abc_recipe,
            abc_delay_ps=3333,
        )
        assert result.returncode == 0, result.stderr
        abc_log = (build_dir / "log_abc_dut.txt").read_text(encoding="utf-8")
        assert "map -D 3333" in abc_log


@pytest.mark.skipif(shutil.which("yosys") is None, reason="requires the pinned Yosys executable")
def test_abc_completes_under_a_long_job_tmpdir(tmp_path):
    """The pinned Yosys's ABC hangs once TMPDIR passes 35 characters (#4730)."""
    liberty = tmp_path / "toy.lib"
    liberty.write_text(_MAPPING_LIBERTY, encoding="utf-8")
    build_dir = tmp_path / "build"
    configure_synthesis(_spec(liberty=liberty), build_dir)
    (build_dir / "sv2v_converted.v").write_text(
        "module dut(input [7:0] a, input [7:0] b, input [7:0] c, output [7:0] y);\n"
        "  assign y = a * b + c;\nendmodule\n",
        encoding="utf-8",
    )
    job_tmp = tmp_path / ("j" * 80)
    job_tmp.mkdir()

    with syn_make.yosys_tmpdir() as yosys_tmp:
        result = run_bounded_child(
            [
                "make",
                "-o",
                "sv2v",
                "-C",
                str(build_dir),
                "yosys",
                syn_make.yosys_tmpdir_assignment(yosys_tmp),
            ],
            env={**os.environ, "TMPDIR": str(job_tmp)},
        )

    assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# parse_params
# ---------------------------------------------------------------------------


class TestParseParams:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["OP_W=32", "DEPTH=4"], {"OP_W": "32", "DEPTH": "4"}),
            ([], {}),
            (["EXPR=a=b"], {"EXPR": "a=b"}),  # a value containing '=' is preserved
            (["  KEY  =  VAL  "], {"KEY": "VAL"}),
        ],
    )
    def test_valid(self, raw, expected):
        assert syn_core.parse_params(raw) == expected

    @pytest.mark.parametrize("raw", ["NO_VALUE", "=value"])
    def test_invalid_exits(self, raw):
        with pytest.raises(SystemExit):
            syn_core.parse_params([raw])


# ---------------------------------------------------------------------------
# scan_synth_logs — false-pass guard
# ---------------------------------------------------------------------------

_SCOPEINFO_LOG = (
    "2.14. Printing statistics.\n"
    "   Area for cell type $scopeinfo is unknown!\n"
    "   Chip area for top module '\\top': 12345.6\n"
)


class TestScanSynthLogs:
    @pytest.mark.parametrize(
        "files",
        [
            {"yosys.log": "All good\nDone.\n"},
            {},
            # sta.log is not scanned: timing violations are warnings, not failures.
            {"sta.log": "ERROR: setup violation\n"},
            # `$scopeinfo` is a zero-area Yosys metadata cell that survives flatten;
            # its stat "unknown area" line is noise, not a corrupted total.
            {"yosys.log": _SCOPEINFO_LOG},
        ],
        ids=["clean", "missing", "opensta-ignored", "benign-scopeinfo"],
    )
    def test_scan_passes(self, tmp_path, files):
        for name, text in files.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        assert syn_core.scan_synth_logs(tmp_path) is None

    @pytest.mark.parametrize(
        ("log_name", "text", "present", "absent"),
        [
            (
                "yosys.log",
                "Running ABC\nERROR: cannot map cell foo\nmore\n",
                ["ERROR: cannot map cell foo"],
                [],
            ),
            ("sv2v.log", "Unsupported construct at line 7\n", ["Unsupported"], []),
            # An unmapped cell (zero-area in `stat`) is a false PASS: Yosys exits 0
            # but undercounts the design. Mirrors the exact Yosys output.
            (
                "yosys.log",
                "2.14. Printing statistics.\n"
                "   Area for cell type $_DFF_PN0_ is unknown!\n"
                "   Number of cells: 1234\n",
                ["Area for cell type $_DFF_PN0_ is unknown!"],
                [],
            ),
            # A benign $scopeinfo line does not mask a real unmapped-cell failure.
            (
                "yosys.log",
                "   Area for cell type $scopeinfo is unknown!\n"
                "   Area for cell type $_DFF_PN0_ is unknown!\n",
                ["$_DFF_PN0_"],
                [],
            ),
            # A stat "unknown area" line must name its likely cause (F-30).
            (
                "yosys.log",
                "Area for cell type $check is unknown!\n",
                [
                    "Area for cell type $check is unknown!",
                    "assertion/formal cell",
                    "chformal -remove",
                    "OpenROAD",
                ],
                [],
            ),
            (
                "yosys.log",
                "Area for cell type $_DFF_PN0_ is unknown!\n",
                ["ZERO area"],
                ["assertion/formal"],
            ),
        ],
        ids=[
            "yosys-error-marker",
            "sv2v-unsupported",
            "unknown-cell-area",
            "real-unknown-area-beside-scopeinfo",
            "formal-cell-cause",
            "ordinary-cell-cause",
        ],
    )
    def test_scan_fails(self, tmp_path, log_name, text, present, absent):
        (tmp_path / log_name).write_text(text, encoding="utf-8")
        line = syn_core.scan_synth_logs(tmp_path)
        assert line is not None
        for fragment in present:
            assert fragment in line
        for fragment in absent:
            assert fragment not in line

    def test_real_slang_log_echoed_markers_pass(self, tmp_path):
        r"""Echoed source containing ERROR:/Unsupported is not a diagnostic (#3673).

        Real, unedited yosys.log from Yosys 0.70+ (git sha1 28ba3cb92),
        ``read_slang``, exit 0. The script was ``read_slang --top top top.sv;
        hierarchy -check -top top; proc; chformal -remove; synth -top top; stat``.
        ``top.sv`` holds, below the port list and one ``always_ff``::

            initial $display("\q"); // ERROR: Unsupported widths here
            ERROR: assert property (@(posedge clk) a != 4'hf) else $display("\q");
            Unsupported: assert property (@(posedge clk) a != 4'h0) else $display("\q");

        Slang echoes each of those lines (one indented, two at column 0) under
        an unknown-escape-sequence warning.
        """
        log = (_SYNTH_FIXTURES / "slang_echoed_markers_yosys_0_70.txt").read_text(encoding="utf-8")
        assert syn_core.scan_synth_text(log) is None
        (tmp_path / "yosys.log").write_text(log, encoding="utf-8")
        assert syn_core.scan_synth_logs(tmp_path) is None

    @pytest.mark.parametrize(
        "warning",
        [
            "Warning: Replacing memory \\mem with list of registers. Unsupported init.",
            "top.sv:3: Warning: Unsupported construct ignored [-Wunsupported]",
        ],
        ids=["unlocated", "located"],
    )
    def test_warning_mentioning_unsupported_passes(self, warning):
        assert syn_core.scan_synth_text(f"Running\n{warning}\nDone.\n") is None

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("top.v:3: ERROR: syntax error, unexpected TOK_ASSIGN\n", "top.v:3: ERROR: syntax"),
            (
                "/work/my proj/top.v:3:7: ERROR: bad token\n",
                "/work/my proj/top.v:3:7: ERROR: bad token",
            ),
            ("top.sv:3: Warning: something odd\nERROR: real failure\n", "ERROR: real failure"),
        ],
        ids=["located", "located-spaced-path-with-column", "warning-without-snippet-then-error"],
    )
    def test_genuine_error_still_fails(self, text, expected):
        failure = syn_core.scan_synth_text(text)
        assert failure is not None
        assert failure.startswith(expected)

    def test_genuine_error_after_echoed_snippets_still_fails(self):
        log = (_SYNTH_FIXTURES / "slang_echoed_markers_yosys_0_70.txt").read_text(encoding="utf-8")
        failure = syn_core.scan_synth_text(log + "ERROR: genuine failure after the snippets\n")
        assert failure == "ERROR: genuine failure after the snippets"


# ---------------------------------------------------------------------------
# parse_area_from_stat
# ---------------------------------------------------------------------------


class TestParseAreaFromStat:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                "Number of cells: 1234\n"
                "Chip area for module '\\sub_mod': 100.50\n"
                "Chip area for top module '\\design': 456.78\n",
                456.78,
                id="top-module-area",
            ),
            pytest.param(
                "Chip area for module '\\a': 100.0\nChip area for module '\\b': 200.0\n",
                200.0,
                id="fallback-last-match",
            ),
            pytest.param(None, None, id="missing-file"),
            pytest.param("Number of cells: 42\n", None, id="no-area-line"),
            # #1078: a non-finite or partial number is no area; the latest complete
            # statistics section is the one selected.
            *(
                pytest.param(f"Chip area for top module dut: {token}\n", None, id=token)
                for token in ("1.2.3", "1e999", "NaN", "inf", "-")
            ),
            pytest.param(
                "1. Printing statistics.\n=== dut ===\n  Number of cells: 1\n"
                "  Chip area for top module dut: 1\n"
                "2. Printing statistics.\n=== dut ===\n  2 - cells\n"
                "  Chip area for top module dut: 2e1\n"
                "3. Printing statistics.\n=== dut ===\n",
                20.0,
                id="latest-complete-section",
            ),
        ],
    )
    def test_area(self, tmp_path, text, expected):
        stat = tmp_path / "stat.txt"
        if text is not None:
            stat.write_text(text, encoding="utf-8")
        assert syn_core.parse_area_from_stat(stat) == expected


class TestParseAbcMappedDelay:
    def test_takes_slowest_positive_partition(self):
        output = (
            "ABC: netlist : i/o = 4/2 area =10.0 delay =83.15 lev = 3\n"
            "ABC: netlist : i/o = 8/4 area =20.0 delay =338.97 lev = 12\n"
        )
        assert syn_core.parse_abc_mapped_delay_ps(output) == pytest.approx(338.97)

    def test_ignores_zero_and_unrelated_delay_text(self):
        output = "ABC: netlist : area =0.0 delay =0.00 lev = 0\nYOSYS_ABC_DELAY_PS: 3333\n"
        assert syn_core.parse_abc_mapped_delay_ps(output) is None


# ---------------------------------------------------------------------------
# area_to_kge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("area_um2", "expected"),
    [
        (798.0, pytest.approx(1.0, abs=0.01)),  # 798 um^2 / (0.798 * 1000) = 1.0 kGE
        (None, None),
    ],
)
def test_area_to_kge(area_um2, expected):
    assert syn_core.area_to_kge(area_um2) == expected


# ---------------------------------------------------------------------------
# Physical STA helpers
# ---------------------------------------------------------------------------


class TestPhysicalStaHelpers:
    def test_detect_clock_port_prefers_common_input(self, tmp_path):
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
        assert openroad_reporting.parse_sta_worst_slack("STA_WORST_SLACK_NS: -0.125") == -0.125

    def test_parse_sta_worst_slack_csv_takes_min(self, tmp_path, capsys):
        csv = tmp_path / "overall.csv.rpt"
        assert openroad_reporting.parse_overall_csv_worst_slack(csv) is None  # missing file
        csv.write_text("a,b,0.100\nc,d,-0.250\n", encoding="utf-8")
        assert openroad_reporting.parse_overall_csv_worst_slack(csv) == -0.25
        # The top-N report is worst first; without a marker the worst path is line 1.
        csv.write_text("a,b,-0.400\nc,d,-0.250\ne,f,0.100\n", encoding="utf-8")
        assert openroad_reporting.parse_overall_csv_worst_slack(csv) == -0.4
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        (report_dir / "overall.csv.rpt").write_text(csv.read_text(encoding="utf-8"))
        capsys.readouterr()
        assert openroad_reporting.emit_timing_markers("", report_dir)
        emitted = capsys.readouterr().out
        assert emitted.count("STA_WORST_SLACK_NS:") == 1
        assert "STA_WORST_SLACK_NS: -0.400000" in emitted

    def test_reg2reg_tcl_is_register_scoped_and_guarded(self):
        tcl = openroad_reporting.reg2reg_timing_tcl()
        # Restricts the worst-path search to register endpoints (internal path).
        assert "-from [all_registers]" in tcl
        assert "-to [all_registers]" in tcl
        # Guarded: a combinational design / older STA degrades to no marker.
        assert "catch" in tcl
        assert "STA_REG2REG_SLACK_NS" in tcl

    def test_reg2reg_tcl_omits_report_when_no_path_given(self):
        # Default stays the arg-free slack-only block: callers that only want
        # the marker (and the golden snapshot) must not grow a stray file write.
        assert "report_checks" not in openroad_reporting.reg2reg_timing_tcl()

    def test_reg2reg_tcl_writes_path_detail_when_asked(self):
        tcl = openroad_reporting.reg2reg_timing_tcl("reports/timing/reg2reg.rpt")
        # Full gate-by-gate detail of the *reg->reg* path, not the overall one.
        assert "report_checks" in tcl
        assert "-format full" in tcl
        assert "> {reports/timing/reg2reg.rpt}" in tcl
        # Inside the same guard as the marker, under its own catch: a report
        # write that chokes must not cost us the slack number.
        assert tcl.index("STA_REG2REG_SLACK_NS") < tcl.index("report_checks")
        assert "  catch {report_checks" in tcl
        # The path's clocks are read before report_checks frees the path objects.
        assert tcl.index("STA_REG2REG_CLOCKS") < tcl.index("report_checks")

    def test_perclock_tcl_iterates_all_clocks(self):
        tcl = openroad_reporting.perclock_timing_tcl()
        # Iterates every clock and reports paths ending in that clock domain.
        assert "all_clocks" in tcl
        assert "STA_PERCLOCK" in tcl
        assert "-to $_clk" in tcl
        # The worst setup path's launch clock decides whether Fmax is derivable.
        assert "startpoint_clock" in tcl
        assert "setup_same_clock=%s" in tcl
        # Reports both setup (max) and hold (min) slack, guarded by catch.
        assert "-path_delay max" in tcl
        assert "-path_delay min" in tcl
        assert "catch" in tcl

    def test_parse_reg2reg_slack_marker(self):
        assert openroad_reporting.parse_reg2reg_slack("STA_REG2REG_SLACK_NS: -0.30") == -0.30
        assert openroad_reporting.parse_reg2reg_slack("nothing here") is None

    def test_print_reg2reg_fmax_emits_derived(self, capsys):
        # slack +0.5 ns at a 2000 ps period → crit 1500 ps → 666.667 MHz.
        stdout = (
            "STA_REG2REG_SLACK_NS: 0.500000\n"
            "STA_REG2REG_CLOCKS: same_clock=1 capture_period_ns=2.000000\n"
        )
        assert openroad_reporting.print_reg2reg_fmax(stdout) is True
        out = capsys.readouterr().out
        assert "STA_REG2REG_CRITICAL_PATH_PS: 1500.000" in out
        assert "STA_REG2REG_FMAX_MHZ: 666.667" in out

    def test_print_reg2reg_fmax_noop_when_absent(self, capsys):
        assert openroad_reporting.print_reg2reg_fmax("STA_WORST_SLACK_NS: -1.0") is False
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        "clocks",
        [
            pytest.param("", id="no-clocks-marker"),
            pytest.param(
                "STA_REG2REG_CLOCKS: same_clock=0 capture_period_ns=2.000000\n", id="cross"
            ),
            pytest.param(
                "STA_REG2REG_CLOCKS: same_clock=0 capture_period_ns=NA\n", id="unclocked"
            ),
            pytest.param(
                "STA_REG2REG_CLOCKS: same_clock=1 capture_period_ns=NA\n", id="no-period"
            ),
            pytest.param(
                "STA_REG2REG_CLOCKS: same_clock=1 capture_period_ns=0.400000\n",
                id="slack-above-period",
            ),
        ],
    )
    def test_print_reg2reg_fmax_omits_without_same_clock_period(self, capsys, clocks):
        # The slack is still reported by the Tcl marker; no critical path or Fmax is invented.
        assert (
            openroad_reporting.print_reg2reg_fmax("STA_REG2REG_SLACK_NS: 0.500000\n" + clocks)
            is True
        )
        out = capsys.readouterr().out
        assert "STA_REG2REG_FMAX_MHZ" not in out
        if clocks.endswith("capture_period_ns=0.400000\n"):
            assert "STA_REG2REG_CRITICAL_PATH_PS: -100.000" in out
        else:
            assert "STA_REG2REG_CRITICAL_PATH_PS" not in out

    def test_path_clocks_tcl_compares_clock_handles_under_catch(self):
        tcl = openroad_reporting.path_clocks_tcl("STA_X_CLOCKS", "[lindex $p 0]")
        assert tcl.startswith("catch {")
        assert "startpoint_clock" in tcl and "endpoint_clock" in tcl
        assert "$_sc eq $_ec" in tcl
        assert "STA_X_CLOCKS: same_clock=$_same capture_period_ns=$_cap" in tcl

    def test_parse_path_clocks(self):
        text = "STA_X: same_clock=1 capture_period_ns=6.400000\n"
        assert openroad_reporting.parse_path_clocks(text, "STA_X") == (True, 6.4)
        assert openroad_reporting.parse_path_clocks(
            "STA_X: same_clock=0 capture_period_ns=NA", "STA_X"
        ) == (False, None)
        assert openroad_reporting.parse_path_clocks(text, "STA_Y") is None


# ---------------------------------------------------------------------------
# emit_timing_markers — reg2reg survives a false-pathed overall worst path
# (SETUP-29)
# ---------------------------------------------------------------------------


class TestEmitTimingMarkers:
    def test_overall_and_reg2reg_both_emitted(self, tmp_path, capsys):
        stdout = (
            "STA_WORST_SLACK_NS: -0.100000\n"
            "STA_WORST_CLOCKS: same_clock=1 capture_period_ns=2.000000\n"
            "STA_REG2REG_SLACK_NS: 0.500000\n"
            "STA_REG2REG_CLOCKS: same_clock=1 capture_period_ns=2.000000\n"
        )
        assert openroad_reporting.emit_timing_markers(stdout, tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_WORST_SLACK_NS: -0.100000" in out
        assert "STA_CRITICAL_PATH_PS: 2100.000" in out
        assert "STA_REG2REG_FMAX_MHZ: 666.667" in out
        assert "STA_REPORT:" in out and "STA_CSV_REPORT:" in out

    def test_no_path_log_reports_no_overall_slack(self, tmp_path, capsys):
        """No STA paths: GPL-0028's comma-separated numbers must not become slack."""
        stdout = (
            "[INFO GPL-0028] Bin count (X, Y):           4 ,      4\n"
            "STA_PERCLOCK: name=clk period_ns=10.000000 wns_ns=NA whs_ns=NA setup_same_clock=0\n"
        )
        (tmp_path / "overall.csv.rpt").write_text("", encoding="utf-8")
        (tmp_path / "overall.rpt").write_text("No paths found.\n", encoding="utf-8")
        openroad_reporting.emit_timing_markers(stdout, tmp_path)
        out = capsys.readouterr().out
        for marker in ("STA_WORST_SLACK_NS", "STA_CRITICAL_PATH_PS", "STA_FMAX_MHZ"):
            assert marker not in out
        assert "STA_PERCLOCK: name=clk" in out

    def test_marker_wins_over_comma_log_lines(self, tmp_path, capsys):
        """A real path keeps its marker slack even beside comma-separated log lines."""
        stdout = (
            "[INFO GPL-0028] Bin count (X, Y):           4 ,      4\n"
            "STA_WORST_SLACK_NS: -0.100000\n"
            "STA_WORST_CLOCKS: same_clock=1 capture_period_ns=10.000000\n"
        )
        assert openroad_reporting.emit_timing_markers(stdout, tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_WORST_SLACK_NS: -0.100000" in out
        assert "STA_CRITICAL_PATH_PS: 10100.000" in out

    def test_csv_slack_wins_over_comma_log_lines_without_marker(self, tmp_path, capsys):
        """No marker (killed run) but real CSV rows: the CSV slack, not GPL-0028's 4."""
        stdout = "[INFO GPL-0028] Bin count (X, Y):           4 ,      4\n"
        (tmp_path / "overall.csv.rpt").write_text("a,b,-0.250\n", encoding="utf-8")
        assert openroad_reporting.emit_timing_markers(stdout, tmp_path) is True
        assert "STA_WORST_SLACK_NS: -0.250000" in capsys.readouterr().out

    def test_reg2reg_survives_when_overall_absent(self, tmp_path, capsys):
        """A false-pathed / I/O-bound overall worst path leaves no
        STA_WORST_SLACK_NS, but the internal reg->reg Fmax must still surface."""
        # No overall marker, and no overall.csv.rpt on disk — only reg->reg.
        stdout = (
            "STA_REG2REG_SLACK_NS: 0.500000\n"
            "STA_REG2REG_CLOCKS: same_clock=1 capture_period_ns=2.000000\n"
        )
        assert openroad_reporting.emit_timing_markers(stdout, tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_WORST_SLACK_NS" not in out
        assert "STA_REG2REG_CRITICAL_PATH_PS: 1500.000" in out
        assert "STA_REG2REG_FMAX_MHZ: 666.667" in out
        # Report pointers surface because timing data (reg->reg) was found.
        assert "STA_REPORT:" in out

    def test_nothing_surfaced_returns_false(self, tmp_path, capsys):
        assert openroad_reporting.emit_timing_markers("no markers here", tmp_path) is False
        assert "STA_REPORT:" not in capsys.readouterr().out

    def test_overall_from_csv_when_stdout_marker_absent(self, tmp_path, capsys):
        # Overall slack only in the CSV report; reg->reg absent.
        (tmp_path / "overall.csv.rpt").write_text("startpt,endpt,-0.250000\n", encoding="utf-8")
        assert openroad_reporting.emit_timing_markers("", tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_WORST_SLACK_NS: -0.250000" in out
        # The CSV report carries no clocks, so its slack gets no critical path or Fmax.
        assert "STA_CRITICAL_PATH_PS" not in out and "STA_FMAX_MHZ" not in out

    def test_reg2reg_report_pointer_only_when_the_file_exists(self, tmp_path, capsys):
        """The reg->reg path detail is advertised only when it was written.

        The Tcl skips the report on a register-free design and the pre-repair
        salvage path never reaches it, so an unconditional pointer would send
        the reader to a file that isn't there."""
        stdout = "STA_REG2REG_SLACK_NS: 0.500000\n"
        assert openroad_reporting.emit_timing_markers(stdout, tmp_path) is True
        assert "STA_REG2REG_REPORT:" not in capsys.readouterr().out

        (tmp_path / "reg2reg.rpt").write_text("Startpoint: ...\n", encoding="utf-8")
        assert openroad_reporting.emit_timing_markers(stdout, tmp_path) is True
        out = capsys.readouterr().out
        assert f"STA_REG2REG_REPORT: {tmp_path / 'reg2reg.rpt'}" in out

    def test_perclock_surfaces_when_overall_and_reg2reg_absent(self, tmp_path, capsys):
        # A design whose overall/reg2reg queries are empty still reports genuine
        # per-clock timing — emit_timing_markers must surface it (return True).
        stdout = _perclock_marker("clk", 2.0, 0.75, 0.1)
        assert openroad_reporting.emit_timing_markers(stdout, tmp_path) is True
        out = capsys.readouterr().out
        assert "STA_PERCLOCK: name=clk" in out
        assert "STA_REPORT:" in out


# ---------------------------------------------------------------------------
# Per-clock marker parsing / re-emission (Fmax + critical path are per-clock)
# ---------------------------------------------------------------------------


def _perclock_marker(name, period, wns, whs, same_clock=True):
    def tok(v):
        return "NA" if v is None else f"{v:.6f}"

    return (
        f"STA_PERCLOCK: name={name} period_ns={period:.6f} wns_ns={tok(wns)} whs_ns={tok(whs)} "
        f"setup_same_clock={int(same_clock)}\n"
    )


class TestPerClockMarkers:
    @pytest.mark.parametrize(
        ("markers", "expected"),
        [
            pytest.param(
                [("clk", 2.0, 0.75, 0.1)],
                {"clk": synth_timing.PerClockRow(2.0, 0.75, 0.1, True)},
                id="basic",
            ),
            pytest.param(
                [("clk", 2.0, None, None)],
                {"clk": synth_timing.PerClockRow(2.0, None, None, True)},
                id="na-becomes-none",
            ),
            # A clock reported twice: the most pessimistic (minimum) slack wins.
            pytest.param(
                [("clk", 2.0, 0.75, 0.30), ("clk", 2.0, 0.20, 0.05)],
                {"clk": synth_timing.PerClockRow(2.0, 0.20, 0.05, True)},
                id="dedup-keeps-min-slack",
            ),
            # A later NA must not overwrite an earlier real slack.
            pytest.param(
                [("clk", 2.0, 0.75, 0.30), ("clk", 2.0, None, None, False)],
                {"clk": synth_timing.PerClockRow(2.0, 0.75, 0.30, True)},
                id="na-never-displaces-real-value",
            ),
            # The same-clock flag belongs to the marker whose setup slack wins.
            pytest.param(
                [("clk", 2.0, 0.75, 0.30), ("clk", 2.0, 0.20, 0.05, False)],
                {"clk": synth_timing.PerClockRow(2.0, 0.20, 0.05, False)},
                id="cross-clock-flag-follows-min-slack",
            ),
            pytest.param(
                [("clk", 2.0, None, None, False), ("clk", 2.0, 0.20, 0.05)],
                {"clk": synth_timing.PerClockRow(2.0, 0.20, 0.05, True)},
                id="real-slack-replaces-na-flag",
            ),
            pytest.param(
                [("clk_a", 2.0, 0.5, 0.1), ("clk_b", 4.0, 1.0, 0.2, False)],
                {
                    "clk_a": synth_timing.PerClockRow(2.0, 0.5, 0.1, True),
                    "clk_b": synth_timing.PerClockRow(4.0, 1.0, 0.2, False),
                },
                id="multi-clock",
            ),
        ],
    )
    def test_parse(self, markers, expected):
        output = "".join(_perclock_marker(*marker) for marker in markers)
        assert synth_timing.parse_perclock(output) == expected

    def test_marker_without_same_clock_flag_is_ignored(self):
        old = "STA_PERCLOCK: name=clk period_ns=2.000000 wns_ns=0.5 whs_ns=0.1\n"
        assert synth_timing.parse_perclock(old) == {}

    def test_emit_perclock_roundtrip(self, capsys):
        # Re-emitting parses the input and prints canonical marker lines that
        # parse back to the same values, including the cross-clock flag.
        stdout = _perclock_marker("clk", 2.0, 0.75, 0.1) + _perclock_marker(
            "other", 4.0, 1.5, 0.2, same_clock=False
        )
        assert openroad_reporting.emit_perclock_markers(stdout) is True
        out = capsys.readouterr().out
        assert synth_timing.parse_perclock(out) == {
            "clk": synth_timing.PerClockRow(2.0, 0.75, 0.1, True),
            "other": synth_timing.PerClockRow(4.0, 1.5, 0.2, False),
        }

    def test_emit_perclock_na_roundtrips_as_na(self, capsys):
        assert (
            openroad_reporting.emit_perclock_markers(_perclock_marker("clk", 2.0, None, None))
            is True
        )
        out = capsys.readouterr().out
        assert "wns_ns=NA" in out and "whs_ns=NA" in out

    def test_emit_perclock_noop_when_absent(self, capsys):
        assert openroad_reporting.emit_perclock_markers("no markers here") is False
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# synth_timing_config — TOML boundary validation (Principle 5)
# ---------------------------------------------------------------------------


def _timing_toml(root: Path, table: str = "") -> None:
    """Write ``<root>/.booley/booley.toml`` holding ``[flows.synth.timing]`` plus *table*."""
    config_dir = root / ".booley"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "booley.toml").write_text(f"[flows.synth.timing]\n{table}\n", encoding="utf-8")


class TestSynthTimingConfigTomlBoundary:
    @pytest.mark.parametrize(
        "line",
        [
            "period_ps = 2500",
            'clock = "clk_i"',
            "input_delay_pct = 25",
            "output_delay_pct = 60",
            'sdc = "false_paths.sdc"',
        ],
    )
    def test_migrated_constraint_key_is_hard_error(self, tmp_path, line):
        # ADR 0029 decision 3 (hard cutoff): a design-constraint key still set in
        # booley.toml is a setup-time error naming the migration, not a silent
        # second source of truth.
        _timing_toml(tmp_path, line)
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config(project_root=tmp_path)
        msg = str(exc_info.value)
        assert line.split(" ")[0] in msg
        assert "file_type: SDC" in msg

    def test_cli_sdc_list_resolves_against_project_root(self, tmp_path):
        # A relative --sta-sdc path resolves against the selected worktree root
        # (sandbox /work), not the process cwd — mirroring --inc-dir/--extra-rtl.
        sdc_file = tmp_path / "constraints" / "false_paths.sdc"
        sdc_file.parent.mkdir()
        sdc_file.write_text("# custom\n", encoding="utf-8")
        cfg = syn_core.synth_timing_config(
            sdc=["constraints/false_paths.sdc"], project_root=tmp_path
        )
        assert cfg.sdc == (sdc_file.resolve(),)

    def test_cli_sdc_list_preserves_order(self, tmp_path):
        # Multiple SDCs concatenate in fileset (list) order, last-wins.
        first = tmp_path / "a.sdc"
        first.write_text("# a\n", encoding="utf-8")
        second = tmp_path / "b.sdc"
        second.write_text("# b\n", encoding="utf-8")
        cfg = syn_core.synth_timing_config(sdc=[str(first), str(second)], project_root=tmp_path)
        assert cfg.sdc == (first.resolve(), second.resolve())

    def test_sdc_path_must_stay_inside_selected_checkout(self, tmp_path):
        outside = tmp_path.parent / "outside.sdc"
        outside.write_text("create_clock -period 4 [get_ports clk]\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="escapes the selected checkout"):
            syn_core.synth_timing_config(sdc=[str(outside)], project_root=tmp_path)

    def test_sdc_follows_symlinked_booley_dir_entry(self, tmp_path):
        """A Codespace links ``/work/.booley`` elsewhere; its SDC stays named through the entry."""
        root = tmp_path / "work"
        data = tmp_path / "workspaces" / "repo"
        elsewhere = tmp_path / "elsewhere"
        for directory in (root, data / "cores", elsewhere):
            directory.mkdir(parents=True)
        symlink_or_skip(root / ".booley", data)
        symlink_or_skip(data / "linked", elsewhere)
        for sdc in (data / "cores" / "top.sdc", elsewhere / "top.sdc"):
            sdc.write_text("create_clock -period 4 [get_ports clk]\n", encoding="utf-8")

        cfg = syn_core.synth_timing_config(sdc=[".booley/cores/top.sdc"], project_root=root)
        assert cfg.sdc == (root.resolve() / ".booley" / "cores" / "top.sdc",)

        # A link below the entry still leaves the selected checkout.
        with pytest.raises(SystemExit, match="escapes the selected checkout"):
            syn_core.synth_timing_config(sdc=[".booley/linked/top.sdc"], project_root=root)

    def test_missing_cli_sdc_file_raises_clear_error(self, tmp_path):
        # A silently-ignored bad path is the worst outcome; error loudly instead.
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config(
                project_root=tmp_path, sdc=[str(tmp_path / "does_not_exist.sdc")]
            )
        assert "does_not_exist.sdc" in str(exc_info.value)

    def test_unreadable_cli_sdc_file_raises_clear_error(self, monkeypatch, tmp_path):
        sdc_file = tmp_path / "dut.sdc"
        sdc_file.write_text("create_clock -period 4 [get_ports clk]\n", encoding="utf-8")
        original_read_bytes = Path.read_bytes

        def fail_for_sdc(path):
            if path == sdc_file:
                raise OSError("permission denied")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", fail_for_sdc)
        with pytest.raises(SystemExit, match="is not readable"):
            syn_core.synth_timing_config(sdc=[str(sdc_file)], project_root=tmp_path)

    def test_unknown_timing_key_warns(self, tmp_path, capsys):
        # A typo'd knob (here ``reepair_timing``) is silently ignored by design —
        # so it must at least surface a warning, not vanish.
        _timing_toml(tmp_path, "reepair_timing = true")
        syn_core.synth_timing_config(project_root=tmp_path)
        out = capsys.readouterr().out
        assert "unknown key 'reepair_timing'" in out
        assert "valid keys" in out

    def test_known_timing_keys_do_not_warn(self, tmp_path, capsys):
        _timing_toml(tmp_path, "utilization_pct = 55\nrepair_timing = false")
        syn_core.synth_timing_config(project_root=tmp_path)
        assert "unknown key" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# synth_timing_config — synthesis mode default + physical knobs
# ---------------------------------------------------------------------------


class TestSynthTimingConfigModes:
    def test_physical_is_default_mode(self, tmp_path):
        cfg = syn_core.synth_timing_config(project_root=tmp_path)
        assert cfg.mode == "physical"
        # Defaulted OpenROAD knobs.
        assert cfg.utilization_pct == syn_core.DEFAULT_STA_UTILIZATION_PCT
        assert cfg.repair_timing is True

    def test_logical_accepted_explicitly(self, tmp_path):
        assert (
            syn_core.synth_timing_config(mode="logical", project_root=tmp_path).mode == "logical"
        )

    def test_retired_timing_engine_is_hard_error(self, tmp_path):
        _timing_toml(tmp_path, 'engine = "opensta"')
        with pytest.raises(SystemExit, match="synth_mode"):
            syn_core.synth_timing_config(project_root=tmp_path)

    def test_invalid_mode_lists_two_modes(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config(mode="bogus", project_root=tmp_path)
        msg = str(exc_info.value)
        assert "physical" in msg and "logical" in msg

    def test_cli_overrides_win(self, tmp_path):
        _timing_toml(tmp_path, "utilization_pct = 55\nrepair_timing = true")
        cfg = syn_core.synth_timing_config(
            ppa=OpenRoadPpaSettings(
                utilization_pct=70.0, placement_density=0.7, repair_setup=False
            ),
            project_root=tmp_path,
        )
        assert cfg.utilization_pct == 70.0
        assert cfg.repair_timing is False

    def test_ppa_settings_drive_openroad_knobs(self, tmp_path):
        # TOML values must lose to explicit ppa when the legacy fallbacks are off.
        _timing_toml(tmp_path, "utilization_pct = 55\nrepair_timing = true")
        ppa = OpenRoadPpaSettings(
            utilization_pct=63.0,
            placement_density=0.72,
            repair_setup=False,
            repair_hold=True,
            gate_cloning=True,
            setup_margin_ns=0.15,
            repair_tns_percent=80.0,
        )
        cfg = syn_core.synth_timing_config(
            ppa=ppa,
            legacy_utilization_fallback=False,
            legacy_repair_fallback=False,
            project_root=tmp_path,
        )
        assert cfg.utilization_pct == 63.0
        assert cfg.repair_timing is False
        assert cfg.placement_density == 0.72
        assert cfg.repair_hold is True
        assert cfg.gate_cloning is True
        assert cfg.setup_margin_ns == 0.15
        assert cfg.repair_tns_percent == 80.0

    @pytest.mark.parametrize(
        ("table", "utilization", "repair"),
        [
            ("", 40.0, True),
            ("utilization_pct = 55\nrepair_timing = false", 55.0, False),
        ],
    )
    def test_omitted_ppa_matches_all_none_call(self, tmp_path, table, utilization, repair):
        if table:
            _timing_toml(tmp_path, table)
        expected = StaTimingConfig(
            mode=syn_core.synth_timing_config(project_root=tmp_path).mode,
            sdc=(),
            utilization_pct=utilization,
            repair_timing=repair,
            placement_density=None,
            repair_hold=False,
            gate_cloning=False,
            setup_margin_ns=0.0,
            repair_tns_percent=None,
            report_paths=syn_core.DEFAULT_TIMING_REPORT_PATHS,
        )
        assert syn_core.synth_timing_config(ppa=None, project_root=tmp_path) == expected
        assert syn_core.synth_timing_config(project_root=tmp_path) == expected

    @pytest.mark.parametrize(
        ("line", "key"),
        [
            ('repair_timing = "yes"', "repair_timing"),
            ('utilization_pct = "high"', "utilization_pct"),
        ],
    )
    def test_ill_typed_timing_value_raises(self, tmp_path, line, key):
        _timing_toml(tmp_path, line)
        with pytest.raises(SystemExit) as exc_info:
            syn_core.synth_timing_config(project_root=tmp_path)
        assert key in str(exc_info.value)


class TestFormalCellRemoval:
    """`chformal -remove` on the slang path (ravenoc F-30).

    yosys-slang *lowers* SVA into `$check` cells (sv2v strips assertions
    instead). Left in the design they reach ABC and `stat` ("Area for cell type
    $check is unknown!") and, worse, `write_verilog` emits them into the STA
    netlist where OpenROAD's structural parser aborts — a synthesis that exits 0
    with an undercounted area and no timing at all.
    """

    def test_slang_script_removes_formal_cells(self, tmp_path):
        assert "chformal -remove" in _script(tmp_path, frontend="slang").splitlines()

    def test_chformal_runs_after_proc_and_before_synth(self, tmp_path):
        """Placement is load-bearing: `proc` lowers the last procedural
        assertions into cells, and the `synth` pass (which runs opt and techmap
        internally) is what chokes on them."""
        lines = _script(tmp_path, frontend="slang").splitlines()
        synth_index = next(i for i, line in enumerate(lines) if line.startswith("synth -top"))
        assert lines.index("proc") < lines.index("chformal -remove") < synth_index

    def test_sv2v_script_unchanged(self, tmp_path):
        """sv2v strips SVA and plain read_verilog never makes a formal cell —
        so the sv2v script (and every area number taken through it) is
        untouched."""
        assert "chformal" not in _script(tmp_path, frontend="sv2v")


class TestSv2vArgv:
    """`sv2v_argv` — one transpile command line for every caller (F-31)."""

    def test_shape(self):
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


class TestEffectiveParameters:
    """Post-elaboration evidence and enabled-define classification (F-13)."""

    def test_parses_yosys_module_header(self):
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
        assert syn_core.resolve_frontend({}) is None

    def test_cli_override_wins(self):
        assert syn_core.resolve_frontend({"frontend": "sv2v"}, override="slang") == "slang"

    def test_unknown_frontend_rejected(self):
        with pytest.raises(BoundaryError, match="must be one of"):
            syn_core.resolve_frontend({"frontend": "verific"})

    def test_slang_options_absent_is_empty(self):
        assert syn_core.resolve_slang_options({}) == []

    def test_slang_options_bare_string_rejected(self):
        """The TOML footgun: a bare string would iterate character-by-character."""
        with pytest.raises(BoundaryError, match="non-empty list"):
            syn_core.resolve_slang_options({"slang_options": "--single-unit"})


@pytest.mark.parametrize(
    "text",
    [
        "ERROR: unknown command\n",
        "ABC: Error recipe\n",
        "Area for cell type $_DFF_P_ is unknown!\n",
        "Area for cell type $scopeinfo is unknown!\n",
        "clean log\n",
    ],
)
def test_1095_text_scan_preserves_path_adapter(tmp_path, text):
    (tmp_path / "yosys.log").write_text(text)
    assert syn_core.scan_synth_text(text) == syn_core.scan_synth_logs(tmp_path)
