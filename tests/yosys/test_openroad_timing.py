"""Unit tests for openroad_timing.py — the OpenROAD timing engine.

Pure tests, no live tools: PDK resolution, Tcl script content, area parsing,
and the warn-and-degrade / marker-emission paths of run_openroad_timing with
run_cmd_watched mocked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides):
    from booley.yosys.syn_core import StaTimingConfig

    base = {
        "engine": "openroad",
        "clock": "clk_i",
        "period_ps": 4000.0,
        "input_delay_pct": 30.0,
        "output_delay_pct": 70.0,
        "sdc": (),
        "utilization_pct": 40.0,
        "repair_timing": True,
    }
    base.update(overrides)
    return StaTimingConfig(**base)


def _make_pdk(tmp_path: Path) -> Path:
    """Create fake setup-managed Nangate45 data under tmp_path; return its root."""
    pdk_dir = tmp_path / "nangate45"
    pdk_dir.mkdir(parents=True, exist_ok=True)
    for name in ("Nangate45_tech.lef", "Nangate45_stdcell.lef", "Nangate45.rc"):
        (pdk_dir / name).write_text("# stub\n", encoding="utf-8")
    return tmp_path


def _make_netlist(work_dir: Path, design: str = "top") -> Path:
    netlist = work_dir / f"sta_{design}.v"
    netlist.write_text(
        "module top(clk_i, y);\n  input clk_i;\n  output y;\nendmodule\n",
        encoding="utf-8",
    )
    return netlist


# ---------------------------------------------------------------------------
# resolve_openroad_pdk
# ---------------------------------------------------------------------------


class TestResolvePdk:
    def test_found_via_prj_lib_dir(self, monkeypatch, tmp_path):
        from booley.yosys import openroad_timing

        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))
        pdk = openroad_timing.resolve_openroad_pdk()
        assert pdk is not None
        assert pdk.tech_lef == root / "nangate45" / "Nangate45_tech.lef"
        assert pdk.stdcell_lef.exists() and pdk.layer_rc.exists()

    def test_missing_returns_none_with_warning(self, monkeypatch, tmp_path, capsys):
        from booley.yosys import openroad_timing

        monkeypatch.setenv("PRJ_LIB_DIR", str(tmp_path))  # no nangate45/ subdir
        assert openroad_timing.resolve_openroad_pdk() is None
        assert "PDK files not found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# write_openroad_script
# ---------------------------------------------------------------------------


class TestWriteScript:
    def _write(self, tmp_path, **cfg_overrides):
        from booley.yosys import openroad_timing

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
            tmp_path / "c.sdc",
            pdk,
            report_dir,
            tmp_path,
            _config(**cfg_overrides),
        )
        return path.read_text(encoding="utf-8")

    def test_lefs_and_floorplan(self, tmp_path):
        text = self._write(tmp_path, utilization_pct=45.0)
        assert "read_lef" in text and "Nangate45_tech.lef" in text
        assert "Nangate45_stdcell.lef" in text
        assert "initialize_floorplan -utilization 45.000" in text
        assert "FreePDK45_38x28_10R_NP_162NW_34O" in text
        # make_tracks is mandatory — the vendored tech LEF ships no TRACKS, so
        # without it place_pins dies with PPL-0021 (regression guard).
        assert "make_tracks" in text

    def test_wire_rc_layers(self, tmp_path):
        text = self._write(tmp_path)
        assert "set_wire_rc -signal -layer metal3" in text
        assert "set_wire_rc -clock -layer metal6" in text
        assert "estimate_parasitics -placement" in text

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

    def test_repair_timing_present_when_enabled(self, tmp_path):
        text = self._write(tmp_path, repair_timing=True)
        assert "repair_timing -setup -skip_gate_cloning" in text

    def test_repair_timing_absent_when_disabled(self, tmp_path):
        text = self._write(tmp_path, repair_timing=False)
        assert "repair_timing -setup" not in text
        # repair_design still runs — only the setup timing pass is gated.
        assert "repair_design" in text

    def test_marker_and_csv_block(self, tmp_path):
        text = self._write(tmp_path)
        assert "STA_WORST_SLACK_NS: %.6f" in text
        assert "find_timing_paths" in text
        assert "report_design_area" in text

    def test_no_bare_exit(self, tmp_path):
        # The -exit flag preserves nonzero-on-error; a bare `exit` would mask it.
        text = self._write(tmp_path)
        assert "\nexit" not in text
        assert not text.rstrip().endswith("exit")

    def test_stage_markers_match_watchdog(self, tmp_path):
        # Every stage the watchdog knows about must be announced by the script
        # (repair_timing only when enabled) — this is the sync contract between
        # write_openroad_script and synthesis_watchdog.OPENROAD_STAGE_NAMES.
        from booley.yosys.synthesis_watchdog import OPENROAD_STAGE_NAMES

        text = self._write(tmp_path, repair_timing=True)
        for name in OPENROAD_STAGE_NAMES:
            assert f'puts "BOOLEY_STAGE: {name}"' in text, name

    def test_stage_markers_skip_repair_timing_when_disabled(self, tmp_path):
        text = self._write(tmp_path, repair_timing=False)
        assert 'puts "BOOLEY_STAGE: repair_timing"' not in text
        assert 'puts "BOOLEY_STAGE: repair_design"' in text

    def test_density_tracks_utilization(self, tmp_path):
        # density = min(0.80, util/100 + 0.25) -> 0.40 + 0.25 = 0.65 at util=40.
        text = self._write(tmp_path, utilization_pct=40.0)
        assert "-density 0.650" in text
        # Clamp: util=70 -> 0.95 clamps to 0.80.
        text2 = self._write(tmp_path, utilization_pct=70.0)
        assert "-density 0.800" in text2

    def test_explicit_density_overrides_derived_value(self, tmp_path):
        text = self._write(tmp_path, utilization_pct=40.0, placement_density=0.72)
        assert "-density 0.720" in text

    def test_expert_timing_repair_controls(self, tmp_path):
        text = self._write(
            tmp_path,
            setup_margin_ns=0.2,
            repair_tns_percent=75.0,
            gate_cloning=True,
            repair_hold=True,
        )
        assert "repair_timing -setup -setup_margin 0.2 -repair_tns 75" in text
        assert "-skip_gate_cloning" not in text
        assert "repair_timing -hold" in text


# ---------------------------------------------------------------------------
# parse_openroad_area
# ---------------------------------------------------------------------------


class TestParseArea:
    def test_happy(self):
        from booley.yosys import openroad_timing

        area, util = openroad_timing.parse_openroad_area("Design area 235 u^2 33% utilization.")
        assert area == 235.0
        assert util == 33.0

    def test_float_values(self):
        from booley.yosys import openroad_timing

        area, util = openroad_timing.parse_openroad_area(
            "Design area 1234.56 u^2 41.7% utilization."
        )
        assert area == 1234.56
        assert util == 41.7

    def test_garbage_returns_none(self):
        from booley.yosys import openroad_timing

        assert openroad_timing.parse_openroad_area("no area here") == (None, None)


# ---------------------------------------------------------------------------
# run_openroad_timing — degrade paths return False
# ---------------------------------------------------------------------------


class TestRunOpenroadDegrade:
    def test_no_binary(self, monkeypatch, tmp_path, capsys):
        from booley.yosys import openroad_timing

        monkeypatch.setattr(openroad_timing, "find_eda_tool", lambda name: None)
        assert not openroad_timing.run_openroad_timing(
            "top", Path("/lib.lib"), tmp_path, _config()
        )
        assert "not on PATH" in capsys.readouterr().out

    def test_missing_netlist(self, monkeypatch, tmp_path, capsys):
        from booley.yosys import openroad_timing

        monkeypatch.setattr(openroad_timing, "find_eda_tool", lambda name: Path("/bin/openroad"))
        assert not openroad_timing.run_openroad_timing(
            "top", Path("/lib.lib"), tmp_path, _config()
        )
        assert "netlist missing" in capsys.readouterr().out

    def test_no_clock(self, monkeypatch, tmp_path, capsys):
        from booley.yosys import openroad_timing

        monkeypatch.setattr(openroad_timing, "find_eda_tool", lambda name: Path("/bin/openroad"))
        # Netlist with no recognizable clock port + no configured clock.
        (tmp_path / "sta_top.v").write_text(
            "module top(a, y); input a; output y; endmodule\n",
            encoding="utf-8",
        )
        cfg = _config(clock=None)
        assert not openroad_timing.run_openroad_timing("top", Path("/lib.lib"), tmp_path, cfg)
        assert "no clock port" in capsys.readouterr().out

    def test_missing_pdk(self, monkeypatch, tmp_path, capsys):
        from booley.yosys import openroad_timing

        monkeypatch.setattr(openroad_timing, "find_eda_tool", lambda name: Path("/bin/openroad"))
        _make_netlist(tmp_path)
        monkeypatch.setattr(openroad_timing, "resolve_openroad_pdk", lambda: None)
        assert not openroad_timing.run_openroad_timing(
            "top", Path("/lib.lib"), tmp_path, _config()
        )

    def test_nonzero_exit_returns_false(self, monkeypatch, tmp_path, capsys):
        from booley.yosys import openroad_timing

        monkeypatch.setattr(openroad_timing, "find_eda_tool", lambda name: Path("/bin/openroad"))
        _make_netlist(tmp_path)
        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))

        def _boom(*a, **k):
            raise subprocess.CalledProcessError(1, ["openroad"])

        monkeypatch.setattr(openroad_timing, "run_cmd_watched", _boom)
        assert not openroad_timing.run_openroad_timing(
            "top", Path("/lib.lib"), tmp_path, _config()
        )
        assert "failed with code 1" in capsys.readouterr().out

    def test_unparseable_slack_returns_false(self, monkeypatch, tmp_path, capsys):
        from booley.yosys import openroad_timing

        monkeypatch.setattr(openroad_timing, "find_eda_tool", lambda name: Path("/bin/openroad"))
        _make_netlist(tmp_path)
        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))
        monkeypatch.setattr(
            openroad_timing,
            "run_cmd_watched",
            lambda *a, **k: MagicMock(returncode=0, stdout="no slack here"),
        )
        assert not openroad_timing.run_openroad_timing(
            "top", Path("/lib.lib"), tmp_path, _config()
        )
        assert "no timing path slack" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_openroad_timing — ADR 0029 D2 pre-repair salvage
# ---------------------------------------------------------------------------


class TestPreRepairSalvage:
    def test_salvage_on_nonzero_exit(self, monkeypatch, tmp_path, capsys):
        """A repair-stage failure still salvages the pre-repair placed STA from
        the captured stdout marker, emitting STA_REPAIR_INCOMPLETE."""
        from booley.yosys import openroad_timing

        monkeypatch.setattr(
            openroad_timing,
            "find_eda_tool",
            lambda name: Path("/bin/openroad"),
        )
        _make_netlist(tmp_path)
        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))

        def _boom(*a, **k):
            raise subprocess.CalledProcessError(
                1,
                ["openroad"],
                output="BOOLEY_STAGE: repair_timing\nSTA_PRE_REPAIR_WORST_SLACK_NS: -1.000000\n",
            )

        monkeypatch.setattr(openroad_timing, "run_cmd_watched", _boom)
        ok = openroad_timing.run_openroad_timing(
            "top",
            Path("/lib.lib"),
            tmp_path,
            _config(period_ps=4000.0),
        )
        assert ok is True
        out = capsys.readouterr().out
        assert "STA_REPAIR_INCOMPLETE:" in out
        # Salvaged from -1.0 ns pre-repair slack: crit = 4000 - (-1*1000) = 5000 ps.
        assert "STA_WORST_SLACK_NS: -1.000000" in out
        assert "STA_CRITICAL_PATH_PS: 5000.000" in out

    def test_salvage_from_csv_when_marker_missing(self, monkeypatch, tmp_path, capsys):
        """When the stdout marker is lost, salvage reads pre_repair.csv.rpt."""
        from booley.yosys import openroad_timing

        monkeypatch.setattr(
            openroad_timing,
            "find_eda_tool",
            lambda name: Path("/bin/openroad"),
        )
        _make_netlist(tmp_path)
        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))
        report_dir = tmp_path / "reports" / "timing"
        report_dir.mkdir(parents=True)
        (report_dir / "pre_repair.csv.rpt").write_text(
            "u/a,u/b,-0.500000\n",
            encoding="utf-8",
        )

        def _boom(*a, **k):
            raise subprocess.CalledProcessError(1, ["openroad"], output="")

        monkeypatch.setattr(openroad_timing, "run_cmd_watched", _boom)
        ok = openroad_timing.run_openroad_timing(
            "top",
            Path("/lib.lib"),
            tmp_path,
            _config(period_ps=4000.0),
        )
        assert ok is True
        out = capsys.readouterr().out
        assert "STA_REPAIR_INCOMPLETE:" in out
        assert "STA_WORST_SLACK_NS: -0.500000" in out

    def test_no_salvage_when_repair_disabled(self, monkeypatch, tmp_path, capsys):
        """repair_timing off → no pre-repair snapshot exists, so a failure just
        degrades to False (no salvage attempted)."""
        from booley.yosys import openroad_timing

        monkeypatch.setattr(
            openroad_timing,
            "find_eda_tool",
            lambda name: Path("/bin/openroad"),
        )
        _make_netlist(tmp_path)
        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))

        def _boom(*a, **k):
            raise subprocess.CalledProcessError(
                1,
                ["openroad"],
                output="STA_PRE_REPAIR_WORST_SLACK_NS: -1.0\n",
            )

        monkeypatch.setattr(openroad_timing, "run_cmd_watched", _boom)
        ok = openroad_timing.run_openroad_timing(
            "top",
            Path("/lib.lib"),
            tmp_path,
            _config(repair_timing=False),
        )
        assert ok is False
        assert "STA_REPAIR_INCOMPLETE:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_openroad_timing — happy path emits markers
# ---------------------------------------------------------------------------


class TestRunOpenroadMarkers:
    def test_markers_emitted(self, monkeypatch, tmp_path, capsys):
        from booley.yosys import openroad_timing

        monkeypatch.setattr(openroad_timing, "find_eda_tool", lambda name: Path("/bin/openroad"))
        _make_netlist(tmp_path)
        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))
        stdout = "STA_WORST_SLACK_NS: 0.500000\nDesign area 235 u^2 33% utilization.\n"
        monkeypatch.setattr(
            openroad_timing,
            "run_cmd_watched",
            lambda *a, **k: MagicMock(returncode=0, stdout=stdout),
        )
        ok = openroad_timing.run_openroad_timing(
            "top",
            Path("/lib.lib"),
            tmp_path,
            _config(period_ps=4000.0),
        )
        assert ok is True
        out = capsys.readouterr().out
        # critical_path = 4000 - 0.5*1000 = 3500 ps; fmax = 1e6/3500 ≈ 285.7 MHz.
        assert "STA_WORST_SLACK_NS: 0.500000" in out
        assert "STA_CRITICAL_PATH_PS: 3500.000" in out
        assert "STA_FMAX_MHZ: 285.714" in out
        assert "STA_REPORT:" in out and "STA_CSV_REPORT:" in out
        assert "OPENROAD_DESIGN_AREA_UM2: 235.000" in out
        assert "OPENROAD_UTILIZATION_PCT: 33.000" in out

    def test_reg2reg_survives_false_pathed_overall(self, monkeypatch, tmp_path, capsys):
        """When the overall worst path is false-pathed (no STA_WORST_SLACK_NS),
        the internal reg->reg Fmax still surfaces and the run succeeds — no
        needless OpenSTA fallback, no area-only QoR (SETUP-29)."""
        from booley.yosys import openroad_timing

        monkeypatch.setattr(openroad_timing, "find_eda_tool", lambda name: Path("/bin/openroad"))
        _make_netlist(tmp_path)
        root = _make_pdk(tmp_path)
        monkeypatch.setenv("PRJ_LIB_DIR", str(root))
        stdout = "STA_REG2REG_SLACK_NS: 0.500000\nDesign area 235 u^2 33% utilization.\n"
        monkeypatch.setattr(
            openroad_timing,
            "run_cmd_watched",
            lambda *a, **k: MagicMock(returncode=0, stdout=stdout),
        )
        ok = openroad_timing.run_openroad_timing(
            "top",
            Path("/lib.lib"),
            tmp_path,
            _config(period_ps=2000.0),
        )
        assert ok is True
        out = capsys.readouterr().out
        assert "STA_WORST_SLACK_NS" not in out
        assert "STA_REG2REG_FMAX_MHZ: 666.667" in out
        assert "no timing path slack" not in out
