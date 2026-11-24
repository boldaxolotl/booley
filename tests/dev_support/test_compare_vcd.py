"""Tests for compare_vcd.py — VCD cross-simulator comparison tool."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from booley.dev_support.compare_vcd import (
    VCDData,
    VCDSignal,
    _leaf_from_hier_path,  # private: see test_empty_falls_back_to_default
    build_dut_signal_map,
    compare_signals,
    main,
    normalize_value,
    parse_vcd,
    print_report,
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

    @pytest.mark.parametrize("timescale", ["1ns", "1ps"])
    def test_empty_sim_no_crash_and_timescale_parsed(self, tmp_path, timescale):
        content = f"""\
        $timescale {timescale} $end
        $scope module tb $end
        $var wire 1 ! clk $end
        $upscope $end
        $enddefinitions $end
        """
        vcd = parse_vcd(_write_vcd(tmp_path, "a.vcd", content))
        assert vcd.end_time == 0
        assert len(vcd.transitions) == 0
        assert vcd.timescale == timescale

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
# --dut-hier-path leaf extraction, through main()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hier_path", "dut_scope"),
    [
        ("tb.dut", "tb.dut"),
        ("tb.uu_alu", "tb.uu_alu"),
        ("tb.gen.uut", "tb.gen.uut"),
        ("tb.gen[0].uut", "tb.gen.uut"),  # brackets stripped from the path
        ("tb.dut_a[1]", "tb.dut_a"),  # array leaf stripped to its base name
        ("tb.", "tb.dut"),  # empty leaf falls back to the default instance
    ],
)
def test_main_dut_hier_path_selects_leaf(tmp_path, monkeypatch, capsys, hier_path, dut_scope):
    """The leaf of --dut-hier-path picks the DUT instance whose signals are compared."""
    trans = "#0\n0!\nb00000000 #\n#10\n1!\n"
    vcd_a = _make_simple_vcd(tmp_path, "a.vcd", scope=dut_scope, transitions=trans)
    vcd_b = _make_simple_vcd(tmp_path, "b.vcd", scope=dut_scope, transitions=trans)
    monkeypatch.setattr(
        sys, "argv", ["compare_vcd", str(vcd_a), str(vcd_b), "--dut-hier-path", hier_path]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 0
    assert "Total DUT signals compared: 2" in capsys.readouterr().out


def test_empty_falls_back_to_default():
    # private: main() skips the helper when --dut-hier-path is empty
    # (_parse_and_compare), so its `if not hier_path` branch has no public entry.
    assert _leaf_from_hier_path("") == "dut"


def _vcd_with_names(full_names: list[str]) -> VCDData:
    """Build a VCDData of 1-bit signals, one per dotted full name, coded ``!``, ``"``, ..."""
    vcd = VCDData()
    for i, full_name in enumerate(full_names):
        code = chr(ord("!") + i)
        scope, _, name = full_name.rpartition(".")
        vcd.signals[code] = VCDSignal(
            code=code, size=1, name=name, scope=scope, full_name=full_name
        )
    return vcd


@pytest.mark.parametrize(
    ("value", "width", "expected"),
    [
        ("b1010", 4, "1010"),  # b prefix stripped
        ("b10", 8, "00000010"),  # zero-extended
        ("bx1", 4, "xxx1"),  # x extends from the MSB
        ("bz0", 4, "zzz0"),  # z extends from the MSB
        ("1", 1, "1"),  # scalar
        ("B1010", 4, "1010"),  # uppercase prefix
        # A bare "b" (empty value after stripping the prefix) has no MSB to
        # extend from: must zero-fill, not IndexError on val[0].
        ("b", 4, "0000"),
        ("", 3, "000"),
        ("b", 0, ""),
    ],
)
def test_normalize_value(value, width, expected):
    assert normalize_value(value, width) == expected


class TestBuildDutSignalMap:
    def test_custom_instance_picked_up(self):
        vcd = _vcd_with_names(["tb.uut.clk", "tb.uut.q", "tb.tb_only"])
        m = build_dut_signal_map(vcd, dut_instance="uut")
        assert set(m) == {"clk", "q"}

    def test_wrong_instance_misses(self):
        vcd = _vcd_with_names(["tb.uut.clk"])
        assert build_dut_signal_map(vcd, dut_instance="dut") == {}

    def test_auto_detect_dut(self):
        # "tb.tb_clk" sits outside the dut and is excluded.
        vcd = _vcd_with_names(["tb.dut.core.clk", "tb.dut.core.data", "tb.tb_clk"])
        assert set(build_dut_signal_map(vcd)) == {"core.clk", "core.data"}

    def test_explicit_scope(self):
        vcd = _vcd_with_names(["TOP.tb.dut.core.clk", "TOP.tb.dut.core.data"])
        m = build_dut_signal_map(vcd, dut_scope="TOP.tb.dut")
        assert set(m) == {"core.clk", "core.data"}

    def test_bit_range_stripped_in_key(self):
        vcd = _vcd_with_names(["tb.dut.data[7:0]"])
        assert "data" in build_dut_signal_map(vcd)

    def test_no_dut_scope_returns_empty(self):
        assert build_dut_signal_map(_vcd_with_names(["tb.core.clk"])) == {}


# ---------------------------------------------------------------------------
# compare_signals tests
# ---------------------------------------------------------------------------

_CLK = [("clk", 1, "!")]
_DATA = [("data", 8, "#")]


def _compare(tmp_path, trans_a, trans_b, *, signals=None, scope_b="tb.dut", **options):
    """Write two VCDs with the given transitions and run compare_signals on them."""
    a = _make_simple_vcd(tmp_path, "a.vcd", signals=signals, transitions=trans_a)
    b = _make_simple_vcd(tmp_path, "b.vcd", scope=scope_b, signals=signals, transitions=trans_b)
    return compare_signals(parse_vcd(a), parse_vcd(b), **options)


class TestCompareSignals:
    def test_identical_vcds_match(self, tmp_path):
        trans = "#0\n0!\nb00000000 #\n#10\n1!\nb00001010 #\n#20\n0!\n"
        res = _compare(tmp_path, trans, trans)
        assert res.mismatched_signals == 0
        assert res.matched_signals == 2
        assert len(res.sim_a_only) == 0
        assert len(res.sim_b_only) == 0

    def test_single_mismatch_detected(self, tmp_path):
        trans_a = "#0\n0!\nb00000000 #\n#10\n1!\nb00001010 #\n"
        trans_b = "#0\n0!\nb00000000 #\n#10\n1!\nb11111111 #\n"
        res = _compare(tmp_path, trans_a, trans_b)
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
        res = compare_signals(parse_vcd(a), parse_vcd(b))
        assert "extra" in res.sim_a_only
        assert len(res.sim_b_only) == 0

    @pytest.mark.parametrize(
        ("signals", "trans_a", "trans_b", "options"),
        [
            # time windowing: the mismatch at t=5 lies before the t=10 window start
            (_CLK, "#0\n0!\n#5\n1!\n#15\n0!\n", "#0\n0!\n#5\n0!\n#15\n0!\n", {"start_time": 10}),
            # end-time windowing: the mismatch at t=20 lies after the t=15 window end
            (_CLK, "#0\n0!\n#5\n1!\n#20\n0!\n", "#0\n0!\n#5\n1!\n#20\n1!\n", {"end_time": 15}),
            # a short binary value still matches the same value at full width
            (_DATA, "#0\nb1010 #\n", "#0\nb00001010 #\n", {}),
            # an ignore pattern suppresses the differing "data" signal
            (
                [("clk", 1, "!"), ("data", 8, "#")],
                "#0\n0!\nb00000000 #\n#10\n1!\nb11111111 #\n",
                "#0\n0!\nb00000000 #\n#10\n1!\nb00000000 #\n",
                {"ignore_patterns": ["data"]},
            ),
        ],
        ids=["start-time", "end-time", "zero-extend", "ignore-pattern"],
    )
    def test_mismatch_suppressed(self, tmp_path, signals, trans_a, trans_b, options):
        res = _compare(tmp_path, trans_a, trans_b, signals=signals, **options)
        assert res.mismatched_signals == 0

    def test_max_mismatches_per_signal_capped(self, tmp_path):
        trans_a = "#0\nb00000000 #\n"
        trans_b = "#0\nb11111111 #\n"
        for t in range(1, 20):
            trans_a += f"#{t * 10}\nb{'0' * 8} #\n"
            trans_b += f"#{t * 10}\nb{'1' * 8} #\n"
        res = _compare(tmp_path, trans_a, trans_b, signals=_DATA, max_mismatches_per_signal=3)
        assert len(res.mismatches) == 3

    def test_different_scopes_across_simulators(self, tmp_path):
        """Different scope prefixes (e.g. tb.dut.* vs TOP.tb.dut.*) should still match."""
        trans = "#0\n0!\n#10\n1!\n"
        res = _compare(tmp_path, trans, trans, signals=_CLK, scope_b="TOP.tb.dut")
        assert res.matched_signals == 1
        assert res.mismatched_signals == 0


# ---------------------------------------------------------------------------
# print_report smoke test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("differs", "summary_only", "expected", "unexpected"),
    [
        (False, False, "PASS", "FAIL"),
        (True, False, "FAIL", "PASS"),
        (True, True, "FAIL", "Mismatches"),  # summary-only suppresses the details
    ],
    ids=["pass", "fail", "summary-only"],
)
def test_print_report_verdict(tmp_path, capsys, differs, summary_only, expected, unexpected):
    trans_a = "#0\n0!\n#10\n1!\n"
    trans_b = "#0\n0!\n#10\n0!\n" if differs else trans_a
    res = _compare(tmp_path, trans_a, trans_b, signals=_CLK)
    print_report(res, summary_only=summary_only)
    out = capsys.readouterr().out
    assert expected in out
    assert unexpected not in out
