"""Unit tests for run_yosys_syn.py — standalone Yosys synthesis CLI.

Tests cover high-level action dispatch, do_run standalone mode, do_clean,
and the retry wrapper delegation.
Subprocess calls are mocked — no real EDA tools invoked.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**overrides) -> argparse.Namespace:
    defaults = {
        "action": "run",
        "top": None,
        "define": [],
        "liberty": None,
        "workdir": None,
        "extra_rtl": None,
        "flatten": True,
        "param": [],
        "sdc": None,
        "tdelay": 4000,
        "abc_recipe": "balanced",
        "frontend": "sv2v",
        "timing_engine": None,
        "clock": None,
        "period_ps": None,
        "default_clock": None,
        "input_delay_pct": None,
        "output_delay_pct": None,
        "sta_sdc": None,
        "utilization_pct": None,
        "repair_timing": None,
        "retry": False,
        "no_retry": True,
        "max_attempts": 3,
        "timeout": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Argument parsing — --extra-rtl accumulation
# ---------------------------------------------------------------------------


class TestExtraRtlParsing:
    """Regression: the asic_synthesize wrapper emits one ``--extra-rtl <file>``
    per resolved source. With a plain ``nargs="+"`` argparse OVERWRITES on each
    repeated flag, keeping only the LAST file and synthesizing an all-but-empty
    design. ``action="extend"`` must make repeated flags accumulate.
    """

    def test_repeated_flags_accumulate(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(
            [
                "run",
                "-t",
                "top",
                "--extra-rtl",
                "a.sv",
                "--extra-rtl",
                "b.sv",
                "--extra-rtl",
                "c.sv",
            ]
        )
        # All three survive — not just the last (the bug we're guarding against).
        assert args.extra_rtl == ["a.sv", "b.sv", "c.sv"]

    def test_batched_values_accumulate(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(
            [
                "run",
                "-t",
                "top",
                "--extra-rtl",
                "a.sv",
                "b.sv",
                "c.sv",
            ]
        )
        assert args.extra_rtl == ["a.sv", "b.sv", "c.sv"]

    def test_no_flag_defaults_empty(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(["run", "-t", "top"])
        assert args.extra_rtl == []

    def test_frontend_defaults_sv2v(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(["run", "-t", "top"])
        assert args.frontend == "sv2v"

    def test_frontend_slang_parses(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(["run", "-t", "top", "--frontend", "slang"])
        assert args.frontend == "slang"

    def test_slang_option_joined_form_carries_flag_valued_token(self):
        """`--slang-option=--single-unit` parses and accumulates.

        Regression (ravenoc halt #2b): the canonical value is itself a flag,
        so the emitter MUST use the `=`-joined form — argparse's two-token form
        rejects a dash-leading value with "expected one argument". This pins
        the parser side of that contract.
        """
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(
            [
                "run",
                "-t",
                "top",
                "--slang-option=--single-unit",
                "--slang-option=--allow-use-before-declare",
            ]
        )
        assert args.slang_option == ["--single-unit", "--allow-use-before-declare"]

    def test_slang_option_defaults_empty(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(["run", "-t", "top"])
        assert args.slang_option == []


# ---------------------------------------------------------------------------
# Timing-engine arguments (OpenROAD)
# ---------------------------------------------------------------------------


class TestTimingArgs:
    def test_profiles_and_backend_overrides_parse(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(
            [
                "run",
                "-t",
                "top",
                "--ppa-profile",
                "compact",
                "--abc-recipe",
                "fast",
                "--placement-density",
                "0.72",
                "--repair-hold",
            ]
        )
        assert args.ppa_profile == "compact"
        assert args.abc_recipe == "fast"
        assert args.placement_density == 0.72
        assert args.repair_hold is True

    def test_conflicting_abc_controls_are_clean_config_error(self):
        from booley.yosys.run_yosys_syn import _build_parser, _resolve_ppa_settings

        args = _build_parser().parse_args(
            ["run", "-t", "top", "--abc-recipe", "fast", "--abc-script", "+strash"]
        )
        with pytest.raises(SystemExit, match="mutually exclusive"):
            _resolve_ppa_settings(args)

    def test_timing_engine_openroad_parses(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(["run", "-t", "top", "--timing-engine", "openroad"])
        assert args.timing_engine == "openroad"

    def test_utilization_and_no_repair_timing(self):
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(
            ["run", "-t", "top", "--utilization-pct", "55", "--no-repair-timing"]
        )
        assert args.utilization_pct == 55.0
        assert args.repair_timing is False

    def test_repair_timing_tristate_defaults_none(self):
        """Absent --no-repair-timing must leave repair_timing None so TOML decides."""
        from booley.yosys.run_yosys_syn import _build_parser

        args = _build_parser().parse_args(["run", "-t", "top"])
        assert args.repair_timing is None
        assert args.utilization_pct is None

    @pytest.mark.parametrize(
        ("option", "value", "field"),
        [
            ("--utilization-pct", "nan", "utilization_pct"),
            ("--placement-density", "inf", "placement_density"),
            ("--setup-margin-ns", "nan", "setup_margin_ns"),
            ("--repair-tns-percent", "-inf", "repair_tns_percent"),
        ],
    )
    def test_non_finite_openroad_override_rejected(self, option, value, field):
        from booley.yosys.run_yosys_syn import _build_parser, _resolve_ppa_settings

        args = _build_parser().parse_args(["run", "-t", "top", f"{option}={value}"])
        with pytest.raises(SystemExit, match=field):
            _resolve_ppa_settings(args)

    def test_standalone_without_profile_preserves_legacy_timing_recipe(self, monkeypatch):
        from booley.yosys import run_yosys_syn as mod

        monkeypatch.setattr(
            "booley.shared_infra._load_rtl_config",
            lambda project_root=None: {
                "flows": {"synth": {"timing": {"utilization_pct": 55, "repair_timing": False}}}
            },
        )
        monkeypatch.setattr("booley.yosys.syn_core._in_container", lambda: True)
        args = mod._build_parser().parse_args(["run", "-t", "top", "--default-clock", "4000"])
        _profile, _yosys, openroad = mod._resolve_ppa_settings(args)
        timing = mod._resolve_syn_timing(args, openroad)
        assert timing.utilization_pct == 55.0
        assert timing.repair_timing is False
        assert timing.placement_density is None

    def test_explicit_profile_replaces_legacy_timing_recipe(self, monkeypatch):
        from booley.yosys import run_yosys_syn as mod

        monkeypatch.setattr(
            "booley.shared_infra._load_rtl_config",
            lambda project_root=None: {
                "flows": {"synth": {"timing": {"utilization_pct": 55, "repair_timing": False}}}
            },
        )
        monkeypatch.setattr("booley.yosys.syn_core._in_container", lambda: True)
        args = mod._build_parser().parse_args(
            [
                "run",
                "-t",
                "top",
                "--default-clock",
                "4000",
                "--ppa-profile",
                "compact",
            ]
        )
        _profile, _yosys, openroad = mod._resolve_ppa_settings(args)
        timing = mod._resolve_syn_timing(args, openroad)
        assert timing.utilization_pct == 40.0
        assert timing.repair_timing is True
        assert timing.placement_density == 0.65


# ---------------------------------------------------------------------------
# do_clean
# ---------------------------------------------------------------------------


class TestDoClean:
    def test_removes_result_dir(self, tmp_path):
        from booley.yosys import run_yosys_syn as mod

        result_dir = tmp_path / "syn_result"
        result_dir.mkdir()
        (result_dir / "test.log").write_text("old", encoding="utf-8")
        with patch.object(mod, "SYN_RESULT_ROOT", result_dir):
            mod.do_clean()
        assert not result_dir.exists()

    def test_clean_nothing(self, tmp_path):
        from booley.yosys import run_yosys_syn as mod

        # No syn_result dir — should not crash
        with patch.object(mod, "SYN_RESULT_ROOT", tmp_path / "syn_result"):
            mod.do_clean()


# ---------------------------------------------------------------------------
# SETUP-27: result dirs land under .booley_project/.runtime, not util/syn
# ---------------------------------------------------------------------------


class TestSynResultRoot:
    def test_result_root_under_runtime_tree(self):
        """The yosys result root must live under the git-ignored runtime tree,
        never in the design repo's util/syn/ namespace (SETUP-27)."""
        from booley.yosys import run_yosys_syn as mod

        parts = mod.SYN_RESULT_ROOT.parts
        assert ".booley_project" in parts
        assert ".runtime" in parts
        assert mod.SYN_RESULT_ROOT.name == "syn_result"
        assert "util" not in parts

    def test_resolve_workdir_uses_result_root(self, tmp_path):
        from booley.yosys import run_yosys_syn as mod

        args = _make_args(workdir="core_a")
        with patch.object(mod, "SYN_RESULT_ROOT", tmp_path / "syn_result"):
            wd = mod._resolve_syn_workdir(args, "my_top", {})
        assert wd == tmp_path / "syn_result" / "core_a"

    def test_resolve_workdir_default_name(self, tmp_path):
        from booley.yosys import run_yosys_syn as mod

        args = _make_args(workdir=None)
        with patch.object(mod, "SYN_RESULT_ROOT", tmp_path / "syn_result"):
            wd = mod._resolve_syn_workdir(args, "my_top", {})
        assert wd == tmp_path / "syn_result" / "standalone.my_top"


# ---------------------------------------------------------------------------
# do_run — standalone mode
# ---------------------------------------------------------------------------


class TestDoRunStandalone:
    def test_standalone_requires_extra_rtl(self):
        from booley.yosys import run_yosys_syn as mod

        args = _make_args(config=None, extra_rtl=None, top="my_top")
        with pytest.raises(SystemExit):
            mod.do_run(args)

    def test_standalone_requires_top(self):
        from booley.yosys import run_yosys_syn as mod

        args = _make_args(config=None, extra_rtl=["file.sv"], top=None)
        with pytest.raises(SystemExit):
            mod.do_run(args)

    @patch("booley.yosys.run_yosys_syn.resolve_liberty", return_value=Path("/lib.lib"))
    def test_no_constraints_is_hard_error(self, _mock_liberty, tmp_path):
        """ADR 0031: no --sta-sdc, no --period-ps, no --default-clock → hard
        error rather than a silent DEFAULT_STA_PERIOD_PS (~250 MHz) fallback."""
        from booley.yosys import run_yosys_syn as mod

        extra = tmp_path / "m.sv"
        extra.write_text("module m; endmodule", encoding="utf-8")
        args = _make_args(config=None, extra_rtl=[str(extra)], top="m")
        with pytest.raises(SystemExit, match="no timing constraints"):
            mod.do_run(args)

    @patch("booley.yosys.run_yosys_syn.run_yosys")
    @patch("booley.yosys.run_yosys_syn.run_sv2v")
    @patch("booley.yosys.run_yosys_syn.prepare_work_dir")
    @patch("booley.yosys.run_yosys_syn.resolve_liberty")
    @patch("booley.yosys.run_yosys_syn.parse_area_from_stat")
    @patch("booley.yosys.run_yosys_syn.area_to_kge")
    def test_standalone_flow(
        self, mock_kge, mock_area, mock_liberty, mock_prep, mock_sv2v, mock_yosys, tmp_path
    ):
        from booley.yosys import run_yosys_syn as mod

        extra_file = tmp_path / "extra.sv"
        extra_file.write_text("module m; endmodule", encoding="utf-8")
        mock_liberty.return_value = Path("/lib.lib")
        mock_sv2v.return_value = tmp_path / "sv2v_converted.v"
        mock_yosys.return_value = MagicMock(
            peak_rss_mb=100,
            max_stall_s=5,
            max_stall_stage="abc",
            memory_growth_rate_mb_per_min=1.0,
            stage_timings={},
        )
        mock_area.return_value = 100.0
        mock_kge.return_value = 1.0
        with patch.object(mod, "SYN_RESULT_ROOT", tmp_path / "syn_result"):
            args = _make_args(
                config=None,
                extra_rtl=[str(extra_file)],
                top="my_mod",
                # ADR 0031: a constraint-less run must name its clock; this
                # standalone flow opts into the canned clock explicitly.
                default_clock=4000.0,
            )
            mod.do_run(args)
        # sv2v frontend (default): the transpile step ran and read_verilog fed
        # yosys the converted file.
        mock_sv2v.assert_called_once()

    @patch("booley.yosys.run_yosys_syn.run_yosys")
    @patch("booley.yosys.run_yosys_syn.run_sv2v")
    @patch("booley.yosys.run_yosys_syn.prepare_work_dir")
    @patch("booley.yosys.run_yosys_syn.resolve_liberty")
    @patch("booley.yosys.run_yosys_syn.parse_area_from_stat")
    @patch("booley.yosys.run_yosys_syn.area_to_kge")
    def test_standalone_flow_slang_skips_sv2v(
        self, mock_kge, mock_area, mock_liberty, mock_prep, mock_sv2v, mock_yosys, tmp_path
    ):
        """--frontend slang runs read_slang directly: sv2v is NOT invoked, and
        the raw sources + include dirs + defines reach run_yosys."""
        from booley.yosys import run_yosys_syn as mod

        extra_file = tmp_path / "extra.sv"
        extra_file.write_text("module m; endmodule", encoding="utf-8")
        inc_dir = tmp_path / "include"
        inc_dir.mkdir()
        mock_liberty.return_value = Path("/lib.lib")
        mock_yosys.return_value = MagicMock(
            peak_rss_mb=100,
            max_stall_s=5,
            max_stall_stage="abc",
            memory_growth_rate_mb_per_min=1.0,
            stage_timings={},
        )
        mock_area.return_value = 100.0
        mock_kge.return_value = 1.0
        with patch.object(mod, "SYN_RESULT_ROOT", tmp_path / "syn_result"):
            args = _make_args(
                config=None,
                extra_rtl=[str(extra_file)],
                inc_dir=[str(inc_dir)],
                define=["SYNTHESIS"],
                top="my_mod",
                frontend="slang",
                default_clock=4000.0,
            )
            mod.do_run(args)
        mock_sv2v.assert_not_called()
        _, kwargs = mock_yosys.call_args
        assert kwargs["frontend"] == "slang"
        # Raw sources (not a sv2v output) are the first positional arg.
        assert extra_file in mock_yosys.call_args[0][0]
        assert [str(p) for p in kwargs["inc_dirs"]] == [str(inc_dir)]
        assert kwargs["defines"] == ["SYNTHESIS"]


# ---------------------------------------------------------------------------
# SETUP-26: attribute a concatenated sv2v_converted.v error to its source core
# ---------------------------------------------------------------------------


class TestSourceProvenance:
    def _make_converted(self, work_dir: Path) -> None:
        # A flat concatenation of two modules; the "error" lands inside bar.
        (work_dir / "sv2v_converted.v").write_text(
            "module foo (input a);\nendmodule\n"  # lines 1-2
            "module bar (input b);\n"  # line 3
            "  wire bad = ;\n"  # line 4 (the offender)
            "endmodule\n",  # line 5
            encoding="utf-8",
        )

    def test_pinpoints_module_and_source_file(self, tmp_path, capsys):
        from booley.yosys import run_yosys_syn as mod

        work_dir = tmp_path / "wd"
        work_dir.mkdir()
        self._make_converted(work_dir)
        (work_dir / "yosys.log").write_text(
            "ERROR: syntax error, unexpected ';' at sv2v_converted.v:4\n",
            encoding="utf-8",
        )
        foo_src = tmp_path / "foo.sv"
        foo_src.write_text("module foo (input a); endmodule\n", encoding="utf-8")
        bar_src = tmp_path / "bar.sv"
        bar_src.write_text("module bar (input b); /*...*/ endmodule\n", encoding="utf-8")

        mod._report_source_provenance(work_dir, [foo_src, bar_src])
        out = capsys.readouterr().out
        assert "module 'bar'" in out
        assert str(bar_src) in out
        assert str(foo_src) not in out  # not the offending core

    def test_falls_back_to_candidate_list_when_unmapped(self, tmp_path, capsys):
        from booley.yosys import run_yosys_syn as mod

        work_dir = tmp_path / "wd"
        work_dir.mkdir()
        self._make_converted(work_dir)
        (work_dir / "yosys.log").write_text(
            "ERROR: something at sv2v_converted.v:4\n",
            encoding="utf-8",
        )
        # Source file does not declare 'bar' → cannot map; still names candidates.
        other = tmp_path / "other.sv"
        other.write_text("module baz; endmodule\n", encoding="utf-8")
        mod._report_source_provenance(work_dir, [other])
        out = capsys.readouterr().out
        assert "candidate cores" in out
        assert str(other) in out

    def test_noop_when_error_not_about_converted_file(self, tmp_path, capsys):
        from booley.yosys import run_yosys_syn as mod

        work_dir = tmp_path / "wd"
        work_dir.mkdir()
        (work_dir / "yosys.log").write_text(
            "ERROR: Can't open liberty file /nope.lib\n",
            encoding="utf-8",
        )
        mod._report_source_provenance(work_dir, [tmp_path / "foo.sv"])
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# _do_run_locked — runs do_run once under the Yosys EDA-tool lock (no retries)
# ---------------------------------------------------------------------------


class TestDoRunLocked:
    @patch("booley.yosys.run_yosys_syn.do_run")
    @patch("booley.eda_tool_lock.eda_tool_lock")
    def test_runs_do_run_once_under_lock(self, mock_lock, mock_do_run):
        from booley.yosys import run_yosys_syn as mod

        args = _make_args(top="my_top")
        mod._do_run_locked(args)
        mock_lock.assert_called_once_with("yosys")
        mock_do_run.assert_called_once_with(args)
