"""Unit tests for OpenROAD timing-script generation and area parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.synth.backends.openroad import timing as openroad_timing
from booley.flows.synth.timing import StaTimingConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides):
    base = {"mode": "physical", "sdc": (), "utilization_pct": 40.0, "repair_timing": True}
    base.update(overrides)
    return StaTimingConfig(**base)


def _make_pdk(tmp_path: Path) -> Path:
    """Create fake setup-managed Nangate45 data under tmp_path; return its root."""
    pdk_dir = tmp_path / "nangate45"
    pdk_dir.mkdir(parents=True, exist_ok=True)
    for name in ("Nangate45_tech.lef", "Nangate45_stdcell.lef", "Nangate45.rc"):
        (pdk_dir / name).write_text("# stub\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# resolve_openroad_pdk
# ---------------------------------------------------------------------------


class TestResolvePdk:
    def test_found_via_prj_lib_dir(self, monkeypatch, tmp_path):
        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))
        pdk = openroad_timing.resolve_openroad_pdk()
        assert pdk is not None
        assert pdk.tech_lef == root / "nangate45" / "Nangate45_tech.lef"
        assert pdk.stdcell_lef.exists() and pdk.layer_rc.exists()

    def test_missing_returns_none_with_warning(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("PRJ_LIB_DIR", str(tmp_path))  # no nangate45/ subdir
        assert openroad_timing.resolve_openroad_pdk() is None
        assert "PDK files not found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# write_openroad_script
# ---------------------------------------------------------------------------


class TestWriteScript:
    def _write(self, tmp_path, **cfg_overrides):
        root = _make_pdk(tmp_path)
        pdk = openroad_timing.OpenRoadPdk(
            tech_lef=root / "nangate45" / "Nangate45_tech.lef",
            stdcell_lef=root / "nangate45" / "Nangate45_stdcell.lef",
            layer_rc=root / "nangate45" / "Nangate45.rc",
        )
        report_dir = tmp_path / "reports"
        report_dir.mkdir(exist_ok=True)
        path = openroad_timing.write_openroad_script(
            "top",
            Path("/lib.lib"),
            tmp_path / "sta_top.v",
            (tmp_path / "c.sdc",),
            pdk,
            report_dir,
            tmp_path,
            _config(**cfg_overrides),
            target="synth_top",
            source_sdc_paths=(tmp_path / "c.sdc",),
        )
        return path.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("overrides", "present", "absent"),
        [
            (
                {"utilization_pct": 45.0},
                [
                    "read_lef",
                    "Nangate45_tech.lef",
                    "Nangate45_stdcell.lef",
                    "initialize_floorplan -utilization 45.000",
                    "FreePDK45_38x28_10R_NP_162NW_34O",
                    "-core_space {2.8 2.8 2.09 2.09}",
                    # make_tracks is mandatory: the vendored tech LEF ships no TRACKS, so
                    # without it place_pins dies with PPL-0021 (regression guard).
                    "make_tracks",
                    "\nremove_buffers\n",  # exact line, not the `[get_cells *]` form
                ],
                ["-core_space 2.0", "remove_buffers [get_cells *]"],
            ),
            (
                {},
                [
                    "set_wire_rc -signal -layer metal3",
                    "set_wire_rc -clock -layer metal6",
                    "estimate_parasitics -placement",
                ],
                [],
            ),
            ({"repair_timing": True}, ["repair_timing -setup -skip_gate_cloning"], []),
            (
                {"repair_timing": False},
                ['puts "BOOLEY_STAGE: repair_design"', "repair_design"],
                ["repair_timing -setup", 'puts "BOOLEY_STAGE: repair_timing"'],
            ),
            # density = min(0.80, util/100 + 0.25): 0.65 at util=40, clamped at util=70.
            ({"utilization_pct": 40.0}, ["-density 0.650"], []),
            ({"utilization_pct": 70.0}, ["-density 0.800"], []),
            ({"utilization_pct": 40.0, "placement_density": 0.72}, ["-density 0.720"], []),
            (
                {
                    "setup_margin_ns": 0.2,
                    "repair_tns_percent": 75.0,
                    "gate_cloning": True,
                    "repair_hold": True,
                },
                ["repair_timing -setup -setup_margin 0.2 -repair_tns 75", "repair_timing -hold"],
                ["-skip_gate_cloning"],
            ),
        ],
    )
    def test_script_fragments(self, tmp_path, overrides, present, absent):
        text = self._write(tmp_path, **overrides)
        for fragment in present:
            assert fragment in text, fragment
        for fragment in absent:
            assert fragment not in text, fragment

    def test_fanout_uses_native_defaults_and_authored_sdc(self, tmp_path):
        sdc = tmp_path / "c.sdc"
        authored = (
            "create_clock -period 10 [get_ports clk]\nset_max_fanout 1000000 [current_design]\n"
        )
        sdc.write_text(authored)
        text = self._write(tmp_path)
        assert "set_max_fanout" not in text
        assert "booley_fanout" not in text
        assert text.index(f"read_sdc {{{sdc.as_posix()}}}") < text.index("\nrepair_design\n")
        assert sdc.read_text() == authored

    def test_clockless_sdc_is_runtime_input_error(self, tmp_path):
        text = self._write(tmp_path)
        assert "[llength [all_clocks]] == 0" in text
        assert "BOOLEY_INPUT_ERROR: synth Target 'synth_top'" in text
        assert "c.sdc" in text
        assert "add create_clock" in text

    def test_reg2reg_block_embedded(self, tmp_path):
        # The internal reg->reg worst-path query must be emitted so the I/O
        # delay budget can't hide the true logic Fmax.
        text = self._write(tmp_path)
        assert "STA_REG2REG_SLACK_NS" in text
        assert "-from [all_registers]" in text

    def test_reg2reg_path_detail_report_written(self, tmp_path):
        # overall.rpt holds only the worst *overall* path — a pad-to-pad
        # feed-through on an I/O-bound design. The reg->reg path behind the
        # reported Fmax needs its own artifact, beside it in report_dir.
        text = self._write(tmp_path)
        # Beside overall.rpt, in whatever report_dir the caller passed.
        assert "reports/overall.rpt" in text.replace(str(tmp_path) + "/", "")
        assert "reports/reg2reg.rpt" in text.replace(str(tmp_path) + "/", "")
        assert "-format full" in text

    def test_perclock_block_embedded(self, tmp_path):
        # Per-clock timing (Fmax/critical-path are per-clock) iterates every
        # clock and reports the worst setup/hold path ending in that domain.
        text = self._write(tmp_path)
        assert "all_clocks" in text
        assert "STA_PERCLOCK" in text
        assert "-to $_clk" in text
        # The per-clock block precedes the reg->reg block (before, per source).
        assert text.index("STA_PERCLOCK") < text.index("STA_REG2REG_SLACK_NS")

    @staticmethod
    def _lines(text, needle):
        return [line.strip() for line in text.splitlines() if needle in line]

    def test_default_reports_list_top_ten_paths_worst_first(self, tmp_path):
        text = self._write(tmp_path)
        # Final report: query, CSV loop over the worst-first list (collecting
        # plain slacks, because report_checks frees the path objects), then the
        # text report capped at the last kept path's slack.
        query = "set paths [find_timing_paths -path_delay max -sort_by_slack -group_path_count 10]"
        capped = (
            "report_checks -path_delay max -sort_by_slack -group_path_count 10 "
            "-slack_max [expr {[lindex $slacks end] + 1e-6}] > "
        )
        assert query in text
        assert capped in text
        loop = "foreach path [lrange $paths 0 9] {"
        assert text.index(query) < text.index("set csv_out [open") < text.index(loop)
        assert text.index(loop) < text.index("close $csv_out") < text.index(capped)
        csv_loop = text[text.index(loop) : text.index("close $csv_out")]
        assert "break" not in csv_loop
        assert "lappend slacks $slack" in csv_loop
        assert "report_design_area" in text

    def test_custom_report_paths_apply_to_every_diagnostic_report(self, tmp_path):
        text = self._write(tmp_path, report_paths=3)
        reports = self._lines(text, "report_checks")
        # overall and pre-repair: capped plus the "No paths" fallback; reg2reg: capped.
        assert len(reports) == 5
        assert all("-group_path_count 3 " in line for line in reports)
        # The cap carries the rounding margin (get_property slack is rounded).
        cap = "-slack_max [expr {[lindex"
        assert sum(cap in line and "+ 1e-6}]" in line for line in reports) == 3
        assert sum("reg2reg.rpt" in line for line in reports) == 1
        assert sum("pre_repair.rpt" in line for line in reports) == 2
        assert sum("overall.rpt" in line for line in reports) == 2
        queries = self._lines(text, "find_timing_paths")
        # overall, pre-repair and reg2reg take N; the per-clock marker queries stay at 1.
        assert sum("-group_path_count 3" in line for line in queries) == 3
        assert sum("-group_path_count 1" in line for line in queries) == 2
        assert len(queries) == 5
        assert "foreach path [lrange $paths 0 2] {" in text
        assert "foreach _pp [lrange $_pre_paths 0 2] {" in text
        assert "foreach _p [lrange $_r2r 0 2] {" in text
        assert "-group_path_count 10" not in text
        assert "-group_count" not in text

    def test_worst_slack_markers_stay_single_path(self, tmp_path):
        text = self._write(tmp_path, report_paths=4)
        for marker, slacks in (
            ("STA_WORST_SLACK_NS", "$slacks"),
            ("STA_PRE_REPAIR_WORST_SLACK_NS", "$_pre_slacks"),
            ("STA_REG2REG_SLACK_NS", "$_r2r_slacks"),
        ):
            assert text.count(f"{marker}: %.6f") == 1, marker
            line = next(row for row in text.splitlines() if f"{marker}: %.6f" in row)
            assert f"[lindex {slacks} 0]" in line, marker
        # No marker sits inside a path loop, so none can print more than once.
        assert text.count("break") == 0
        # The overall worst path's clocks are read once, before the final report frees the paths.
        assert text.count("STA_WORST_CLOCKS") == 1
        assert text.index("STA_WORST_CLOCKS") < text.index("overall.rpt}")
        assert "STA_CLOCK_PERIOD_NS" not in text
        perclock = self._lines(text, "-to $_clk")
        assert perclock
        assert all("-group_path_count 1" in line for line in perclock)

    def test_no_bare_exit(self, tmp_path):
        # The -exit flag preserves nonzero-on-error; a bare `exit` would mask it.
        text = self._write(tmp_path)
        assert "\nexit" not in text
        assert not text.rstrip().endswith("exit")

    def test_stage_markers_cover_the_generated_pipeline(self, tmp_path):
        text = self._write(tmp_path, repair_timing=True)
        for name in (
            "floorplan",
            "place_pins",
            "global_placement",
            "repair_design",
            "detailed_placement",
            "sta_report_pre_repair",
            "repair_timing",
            "sta_report",
        ):
            assert f'puts "BOOLEY_STAGE: {name}"' in text, name


# ---------------------------------------------------------------------------
# parse_openroad_area
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Design area 235 u^2 33% utilization.", (235.0, 33.0)),
        ("Design area 1234.56 u^2 41.7% utilization.", (1234.56, 41.7)),
        # 26Q3 spells the unit um^2.
        ("Design area 1234.56 um^2 41.7% utilization.", (1234.56, 41.7)),
        ("no area here", (None, None)),
    ],
)
def test_parse_openroad_area(text, expected):
    assert openroad_timing.parse_openroad_area(text) == expected
