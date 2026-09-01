"""Tests for compare_vcd.py — VCD cross-simulator comparison tool."""

from __future__ import annotations

import textwrap
from pathlib import Path

from booley.dev_support.compare_vcd import (
    VCDData,
    VCDSignal,
    _leaf_from_hier_path,
    build_dut_signal_map,
    compare_signals,
    normalize_value,
    parse_vcd,
)

# ---------------------------------------------------------------------------
# Helpers — write VCD text to a temp file and parse it
# ---------------------------------------------------------------------------


def _write_vcd(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _make_simple_vcd(
    tmp_path: Path,
    name: str,
    *,
    scope: str = "tb.dut",
    signals: list[tuple[str, int, str]] | None = None,
    transitions: str = "",
) -> Path:
    """Generate a minimal valid VCD with given signals and transitions.

    signals: list of (name, width, code). Default: clk/1/!, data/8/#
    """
    if signals is None:
        signals = [("clk", 1, "!"), ("data", 8, "#")]

    parts = scope.split(".")
    header = ""
    for p in parts:
        header += f"$scope module {p} $end\n"
    for sig_name, width, code in signals:
        header += f"$var wire {width} {code} {sig_name} $end\n"
    for _ in parts:
        header += "$upscope $end\n"
    header += "$enddefinitions $end\n"

    return _write_vcd(tmp_path, name, header + transitions)


# ---------------------------------------------------------------------------
# parse_vcd tests
# ---------------------------------------------------------------------------


class TestParseVcd:
    def test_basic_signals(self, tmp_path):
        p = _make_simple_vcd(tmp_path, "a.vcd")
        vcd = parse_vcd(p)
        assert len(vcd.signals) == 2
        assert "!" in vcd.signals
        assert vcd.signals["!"].name == "clk"
        assert vcd.signals["!"].size == 1
        assert vcd.signals["#"].name == "data"
        assert vcd.signals["#"].size == 8

    def test_scope_hierarchy(self, tmp_path):
        p = _make_simple_vcd(tmp_path, "a.vcd", scope="TOP.tb.dut.core")
        vcd = parse_vcd(p)
        sig = vcd.signals["!"]
        assert sig.scope == "TOP.tb.dut.core"
        assert sig.full_name == "TOP.tb.dut.core.clk"

    def test_timestamps_and_transitions(self, tmp_path):
        trans = "#0\n0!\nb00000000 #\n#10\n1!\nb00001010 #\n#20\n0!\n"
        p = _make_simple_vcd(tmp_path, "a.vcd", transitions=trans)
        vcd = parse_vcd(p)
        assert vcd.end_time == 20
        assert len(vcd.transitions["!"]) == 3  # 0, 1, 0
        assert len(vcd.transitions["#"]) == 2  # 00000000, 00001010

    def test_bit_range_stripped(self, tmp_path):
        content = """\
        $scope module tb $end
        $scope module dut $end
        $var wire 8 # data [7:0] $end
        $upscope $end
        $upscope $end
        $enddefinitions $end
        """
        p = _write_vcd(tmp_path, "a.vcd", content)
        vcd = parse_vcd(p)
        assert vcd.signals["#"].name == "data"
        assert vcd.signals["#"].full_name == "tb.dut.data"

    def test_tab_separated_vectors(self, tmp_path):
        content = """\
        $scope module tb $end
        $var wire 8 # data $end
        $upscope $end
        $enddefinitions $end
        #0
        b00001111\t#
        #10
        b11110000\t#
        """
        p = _write_vcd(tmp_path, "a.vcd", content)
        vcd = parse_vcd(p)
        assert len(vcd.transitions["#"]) == 2
        assert vcd.transitions["#"][0] == (0, "b00001111")
        assert vcd.transitions["#"][1] == (10, "b11110000")

    def test_empty_sim_no_crash(self, tmp_path):
        content = """\
        $timescale 1ns $end
        $scope module tb $end
        $var wire 1 ! clk $end
        $upscope $end
        $enddefinitions $end
        """
        p = _write_vcd(tmp_path, "a.vcd", content)
        vcd = parse_vcd(p)
        assert vcd.end_time == 0
        assert len(vcd.transitions) == 0

    def test_dumpvars_dumpoff_dumpon_skipped(self, tmp_path):
        content = """\
        $scope module tb $end
        $var wire 1 ! clk $end
        $upscope $end
        $enddefinitions $end
        #0
        $dumpvars
        0!
        $end
        #5
        $dumpoff
        x!
        $end
        #10
        $dumpon
        1!
        $end
        """
        p = _write_vcd(tmp_path, "a.vcd", content)
        vcd = parse_vcd(p)
        # Parser should handle these sections without crashing
        assert len(vcd.signals) == 1

    def test_timescale_parsed(self, tmp_path):
        content = """\
        $timescale 1ps $end
        $scope module tb $end
        $var wire 1 ! clk $end
        $upscope $end
        $enddefinitions $end
        """
        p = _write_vcd(tmp_path, "a.vcd", content)
        vcd = parse_vcd(p)
        assert vcd.timescale == "1ps"

    def test_malformed_var_width_skipped(self, tmp_path):
        # A non-integer width field must not crash the parse (consistent with
        # the guarded timestamp parse); the bad $var is skipped, good ones stay.
        content = """\
        $scope module tb $end
        $var wire NaN @ bad $end
        $var wire 1 ! clk $end
        $upscope $end
        $enddefinitions $end
        """
        p = _write_vcd(tmp_path, "a.vcd", content)
        vcd = parse_vcd(p)
        assert "@" not in vcd.signals  # malformed width -> skipped
        assert vcd.signals["!"].name == "clk"


# ---------------------------------------------------------------------------
# normalize_value tests
# ---------------------------------------------------------------------------


class TestLeafFromHierPath:
    """Leaf extraction from a traced hierarchical signal path."""

    def test_simple_path(self):
        assert _leaf_from_hier_path("tb.dut") == "dut"

    def test_renamed_instance(self):
        assert _leaf_from_hier_path("tb.uu_alu") == "uu_alu"

    def test_generic_uut(self):
        assert _leaf_from_hier_path("tb.gen.uut") == "uut"

    def test_brackets_stripped(self):
        assert _leaf_from_hier_path("tb.gen[0].uut") == "uut"

    def test_array_leaf_stripped(self):
        assert _leaf_from_hier_path("tb.dut_a[1]") == "dut_a"

    def test_empty_falls_back_to_default(self):
        assert _leaf_from_hier_path("") == "dut"


class TestBuildDutSignalMapCustomInstance:
    """Phase 8.4: build_dut_signal_map honours custom dut_instance."""

    def _vcd_with(self, full_names: list[str]) -> VCDData:
        vcd = VCDData()
        for i, name in enumerate(full_names):
            code = chr(ord("!") + i)
            parts = name.rsplit(".", 1)
            scope = parts[0] if len(parts) > 1 else ""
            local = parts[-1]
            vcd.signals[code] = VCDSignal(
                code=code,
                size=1,
                name=local,
                scope=scope,
                full_name=name,
            )
        return vcd

    def test_uut_instance_picked_up(self):
        vcd = self._vcd_with(["tb.uut.clk", "tb.uut.q", "tb.tb_only"])
        m = build_dut_signal_map(vcd, dut_instance="uut")
        assert "clk" in m and "q" in m
        assert len(m) == 2

    def test_default_dut_still_works(self):
        vcd = self._vcd_with(["tb.dut.clk"])
        m = build_dut_signal_map(vcd)
        assert "clk" in m

    def test_wrong_instance_misses(self):
        vcd = self._vcd_with(["tb.uut.clk"])
        m = build_dut_signal_map(vcd, dut_instance="dut")
        assert m == {}


class TestNormalizeValue:
    def test_strip_b_prefix(self):
        assert normalize_value("b1010", 4) == "1010"

    def test_zero_extend(self):
        assert normalize_value("b10", 8) == "00000010"

    def test_x_extend(self):
        assert normalize_value("bx1", 4) == "xxx1"

    def test_z_extend(self):
        assert normalize_value("bz0", 4) == "zzz0"

    def test_scalar(self):
        assert normalize_value("1", 1) == "1"

    def test_uppercase_b(self):
        assert normalize_value("B1010", 4) == "1010"

    def test_bare_b_is_zero_filled(self):
        # A bare "b" (empty value after stripping the prefix) has no MSB to
        # extend from — must zero-fill, not IndexError on val[0].
        assert normalize_value("b", 4) == "0000"

    def test_empty_value_is_zero_filled(self):
        assert normalize_value("", 3) == "000"

    def test_empty_zero_width(self):
        assert normalize_value("b", 0) == ""


# ---------------------------------------------------------------------------
# build_dut_signal_map tests
# ---------------------------------------------------------------------------


class TestBuildDutSignalMap:
    def _make_vcd_with_signals(self, sigs: list[tuple[str, str]]) -> VCDData:
        """Helper: sigs = [(full_name, code), ...]"""
        vcd = VCDData()
        for full_name, code in sigs:
            parts = full_name.rsplit(".", 1)
            scope = parts[0] if len(parts) > 1 else ""
            name = parts[-1]
            vcd.signals[code] = VCDSignal(
                code=code,
                size=1,
                name=name,
                scope=scope,
                full_name=full_name,
            )
        return vcd

    def test_auto_detect_dut(self):
        vcd = self._make_vcd_with_signals(
            [
                ("tb.dut.core.clk", "!"),
                ("tb.dut.core.data", "#"),
                ("tb.tb_clk", "$"),  # outside dut — excluded
            ]
        )
        m = build_dut_signal_map(vcd)
        assert "core.clk" in m
        assert "core.data" in m
        assert len(m) == 2

    def test_explicit_scope(self):
        vcd = self._make_vcd_with_signals(
            [
                ("TOP.tb.dut.core.clk", "!"),
                ("TOP.tb.dut.core.data", "#"),
            ]
        )
        m = build_dut_signal_map(vcd, dut_scope="TOP.tb.dut")
        assert "core.clk" in m
        assert "core.data" in m

    def test_bit_range_stripped_in_key(self):
        vcd = VCDData()
        vcd.signals["#"] = VCDSignal(
            code="#",
            size=8,
            name="data",
            scope="tb.dut",
            full_name="tb.dut.data[7:0]",
        )
        m = build_dut_signal_map(vcd)
        assert "data" in m

    def test_no_dut_scope_returns_empty(self):
        vcd = self._make_vcd_with_signals(
            [
                ("tb.core.clk", "!"),  # no "dut" in path
            ]
        )
        m = build_dut_signal_map(vcd)
        assert len(m) == 0


# ---------------------------------------------------------------------------
# compare_signals tests
# ---------------------------------------------------------------------------


class TestCompareSignals:
    def test_identical_vcds_match(self, tmp_path):
        trans = "#0\n0!\nb00000000 #\n#10\n1!\nb00001010 #\n#20\n0!\n"
        a = _make_simple_vcd(tmp_path, "a.vcd", transitions=trans)
        b = _make_simple_vcd(tmp_path, "b.vcd", transitions=trans)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver)
        assert res.mismatched_signals == 0
        assert res.matched_signals == 2
        assert len(res.sim_a_only) == 0
        assert len(res.sim_b_only) == 0

    def test_single_mismatch_detected(self, tmp_path):
        trans_a = "#0\n0!\nb00000000 #\n#10\n1!\nb00001010 #\n"
        trans_b = "#0\n0!\nb00000000 #\n#10\n1!\nb11111111 #\n"
        a = _make_simple_vcd(tmp_path, "a.vcd", transitions=trans_a)
        b = _make_simple_vcd(tmp_path, "b.vcd", transitions=trans_b)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver)
        assert res.mismatched_signals == 1
        assert res.matched_signals == 1  # clk still matches
        assert len(res.mismatches) == 1
        assert res.mismatches[0]["signal"] == "data"
        assert res.mismatches[0]["time"] == 10

    def test_signal_only_in_one_simulator(self, tmp_path):
        sigs_a = [("clk", 1, "!"), ("data", 8, "#"), ("extra", 1, "$")]
        sigs_b = [("clk", 1, "!"), ("data", 8, "#")]
        trans = "#0\n0!\nb00000000 #\n"
        a = _make_simple_vcd(tmp_path, "a.vcd", signals=sigs_a, transitions=trans + "0$\n")
        b = _make_simple_vcd(tmp_path, "b.vcd", signals=sigs_b, transitions=trans)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver)
        assert "extra" in res.sim_a_only
        assert len(res.sim_b_only) == 0

    def test_time_windowing(self, tmp_path):
        # Mismatch at t=5 (before window), match at t=15 (in window)
        trans_a = "#0\n0!\n#5\n1!\n#15\n0!\n"
        trans_b = "#0\n0!\n#5\n0!\n#15\n0!\n"  # differs at t=5
        a = _make_simple_vcd(tmp_path, "a.vcd", signals=[("clk", 1, "!")], transitions=trans_a)
        b = _make_simple_vcd(tmp_path, "b.vcd", signals=[("clk", 1, "!")], transitions=trans_b)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        # Window starts at t=10 — the mismatch at t=5 should be suppressed
        res = compare_signals(viv, ver, start_time=10)
        assert res.mismatched_signals == 0

    def test_value_normalization_zero_extend(self, tmp_path):
        # Short binary in one, full-width in other — should still match
        trans_a = "#0\nb1010 #\n"
        trans_b = "#0\nb00001010 #\n"
        a = _make_simple_vcd(tmp_path, "a.vcd", signals=[("data", 8, "#")], transitions=trans_a)
        b = _make_simple_vcd(tmp_path, "b.vcd", signals=[("data", 8, "#")], transitions=trans_b)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver)
        assert res.mismatched_signals == 0

    def test_ignore_pattern_filters(self, tmp_path):
        trans = "#0\n0!\nb00000000 #\n#10\n1!\nb11111111 #\n"
        trans2 = "#0\n0!\nb00000000 #\n#10\n1!\nb00000000 #\n"  # data differs
        a = _make_simple_vcd(tmp_path, "a.vcd", transitions=trans)
        b = _make_simple_vcd(tmp_path, "b.vcd", transitions=trans2)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        # Ignore "data" — should suppress the mismatch
        res = compare_signals(viv, ver, ignore_patterns=["data"])
        assert res.mismatched_signals == 0

    def test_max_mismatches_per_signal_capped(self, tmp_path):
        # Generate many transitions that differ
        trans_a = "#0\nb00000000 #\n"
        trans_b = "#0\nb11111111 #\n"
        for t in range(1, 20):
            trans_a += f"#{t * 10}\nb{'0' * 8} #\n"
            trans_b += f"#{t * 10}\nb{'1' * 8} #\n"
        a = _make_simple_vcd(tmp_path, "a.vcd", signals=[("data", 8, "#")], transitions=trans_a)
        b = _make_simple_vcd(tmp_path, "b.vcd", signals=[("data", 8, "#")], transitions=trans_b)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver, max_mismatches_per_signal=3)
        assert len(res.mismatches) == 3

    def test_different_scopes_across_simulators(self, tmp_path):
        """Different scope prefixes (e.g. tb.dut.* vs TOP.tb.dut.*) should still match."""
        trans = "#0\n0!\n#10\n1!\n"
        a = _make_simple_vcd(
            tmp_path, "a.vcd", scope="tb.dut", signals=[("clk", 1, "!")], transitions=trans
        )
        b = _make_simple_vcd(
            tmp_path, "b.vcd", scope="TOP.tb.dut", signals=[("clk", 1, "!")], transitions=trans
        )
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver)
        assert res.matched_signals == 1
        assert res.mismatched_signals == 0

    def test_end_time_windowing(self, tmp_path):
        # Match at t=5, mismatch at t=20 (after window)
        trans_a = "#0\n0!\n#5\n1!\n#20\n0!\n"
        trans_b = "#0\n0!\n#5\n1!\n#20\n1!\n"  # differs at t=20
        a = _make_simple_vcd(tmp_path, "a.vcd", signals=[("clk", 1, "!")], transitions=trans_a)
        b = _make_simple_vcd(tmp_path, "b.vcd", signals=[("clk", 1, "!")], transitions=trans_b)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver, end_time=15)
        assert res.mismatched_signals == 0


# ---------------------------------------------------------------------------
# print_report smoke test
# ---------------------------------------------------------------------------


class TestPrintReport:
    def test_pass_verdict(self, tmp_path, capsys):
        from booley.dev_support.compare_vcd import print_report

        trans = "#0\n0!\n#10\n1!\n"
        a = _make_simple_vcd(tmp_path, "a.vcd", signals=[("clk", 1, "!")], transitions=trans)
        viv = parse_vcd(a)
        ver = parse_vcd(a)
        res = compare_signals(viv, ver)
        print_report(res)
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_fail_verdict(self, tmp_path, capsys):
        from booley.dev_support.compare_vcd import print_report

        trans_a = "#0\n0!\n#10\n1!\n"
        trans_b = "#0\n0!\n#10\n0!\n"
        a = _make_simple_vcd(tmp_path, "a.vcd", signals=[("clk", 1, "!")], transitions=trans_a)
        b = _make_simple_vcd(tmp_path, "b.vcd", signals=[("clk", 1, "!")], transitions=trans_b)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver)
        print_report(res)
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_summary_only_suppresses_details(self, tmp_path, capsys):
        from booley.dev_support.compare_vcd import print_report

        trans_a = "#0\n0!\n#10\n1!\n"
        trans_b = "#0\n0!\n#10\n0!\n"
        a = _make_simple_vcd(tmp_path, "a.vcd", signals=[("clk", 1, "!")], transitions=trans_a)
        b = _make_simple_vcd(tmp_path, "b.vcd", signals=[("clk", 1, "!")], transitions=trans_b)
        viv = parse_vcd(a)
        ver = parse_vcd(b)
        res = compare_signals(viv, ver)
        print_report(res, summary_only=True)
        out = capsys.readouterr().out
        assert "Mismatches" not in out
        assert "FAIL" in out
