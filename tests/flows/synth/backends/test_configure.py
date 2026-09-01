"""Unit tests for the non-executing Yosys synthesis configuration surface."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**overrides) -> argparse.Namespace:
    defaults = {
        "action": "configure",
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
        "synth_mode": "physical",
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
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(
            [
                "configure",
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
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(
            [
                "configure",
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
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(["configure", "-t", "top"])
        assert args.extra_rtl == []

    def test_frontend_defaults_sv2v(self):
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(["configure", "-t", "top"])
        assert args.frontend == "sv2v"

    def test_frontend_slang_parses(self):
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(["configure", "-t", "top", "--frontend", "slang"])
        assert args.frontend == "slang"

    def test_slang_option_joined_form_carries_flag_valued_token(self):
        """`--slang-option=--single-unit` parses and accumulates.

        Regression (ravenoc halt #2b): the canonical value is itself a flag,
        so the emitter MUST use the `=`-joined form — argparse's two-token form
        rejects a dash-leading value with "expected one argument". This pins
        the parser side of that contract.
        """
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(
            [
                "configure",
                "-t",
                "top",
                "--slang-option=--single-unit",
                "--slang-option=--allow-use-before-declare",
            ]
        )
        assert args.slang_option == ["--single-unit", "--allow-use-before-declare"]

    def test_slang_option_defaults_empty(self):
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(["configure", "-t", "top"])
        assert args.slang_option == []


# ---------------------------------------------------------------------------
# Synthesis-mode arguments
# ---------------------------------------------------------------------------


class TestTimingArgs:
    def test_profiles_and_backend_overrides_parse(self):
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(
            [
                "configure",
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
        from booley.flows.synth.backends.configure import _build_parser, _resolve_ppa_settings

        args = _build_parser().parse_args(
            ["configure", "-t", "top", "--abc-recipe", "fast", "--abc-script", "+strash"]
        )
        with pytest.raises(SystemExit, match="mutually exclusive"):
            _resolve_ppa_settings(args)

    @pytest.mark.parametrize("mode", ["physical", "logical"])
    def test_synth_mode_parses(self, mode):
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(["configure", "-t", "top", "--synth-mode", mode])
        assert args.synth_mode == mode

    def test_utilization_and_no_repair_timing(self):
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(
            ["configure", "-t", "top", "--utilization-pct", "55", "--no-repair-timing"]
        )
        assert args.utilization_pct == 55.0
        assert args.repair_timing is False

    def test_repair_timing_tristate_defaults_none(self):
        """Absent --no-repair-timing must leave repair_timing None so TOML decides."""
        from booley.flows.synth.backends.configure import _build_parser

        args = _build_parser().parse_args(["configure", "-t", "top"])
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
        from booley.flows.synth.backends.configure import _build_parser, _resolve_ppa_settings

        args = _build_parser().parse_args(["configure", "-t", "top", f"{option}={value}"])
        with pytest.raises(SystemExit, match=field):
            _resolve_ppa_settings(args)

    def test_explicit_source_without_profile_preserves_legacy_timing_recipe(self, monkeypatch):
        from booley.flows.synth.backends import configure as mod

        monkeypatch.setattr(
            "booley.runtime.shared_infra._load_rtl_config",
            lambda project_root=None: {
                "flows": {"synth": {"timing": {"utilization_pct": 55, "repair_timing": False}}}
            },
        )
        args = mod._build_parser().parse_args(
            ["configure", "-t", "top", "--default-clock", "4000"]
        )
        _profile, _yosys, openroad = mod._resolve_ppa_settings(args)
        timing = mod._resolve_syn_timing(args, openroad)
        assert timing.utilization_pct == 55.0
        assert timing.repair_timing is False
        assert timing.placement_density is None

    def test_explicit_profile_replaces_legacy_timing_recipe(self, monkeypatch):
        from booley.flows.synth.backends import configure as mod

        monkeypatch.setattr(
            "booley.runtime.shared_infra._load_rtl_config",
            lambda project_root=None: {
                "flows": {"synth": {"timing": {"utilization_pct": 55, "repair_timing": False}}}
            },
        )
        args = mod._build_parser().parse_args(
            [
                "configure",
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
        from booley.flows.synth.backends import configure as mod

        result_dir = tmp_path / "syn_result"
        result_dir.mkdir()
        (result_dir / "test.log").write_text("old", encoding="utf-8")
        with patch.object(mod, "_synth_result_root", return_value=result_dir):
            mod.do_clean()
        assert not result_dir.exists()

    def test_clean_nothing(self, tmp_path):
        from booley.flows.synth.backends import configure as mod

        # No syn_result dir — should not crash
        with patch.object(mod, "_synth_result_root", return_value=tmp_path / "syn_result"):
            mod.do_clean()


# ---------------------------------------------------------------------------
# SETUP-27: result dirs land under .booley_project/.runtime, not util/syn
# ---------------------------------------------------------------------------


class TestSynResultRoot:
    def test_result_root_under_runtime_tree(self, tmp_path):
        """The Yosys result root must live under the project runtime tree,
        never in the design repo's util/syn/ namespace (SETUP-27)."""
        from booley.flows.synth.backends import configure as mod

        (tmp_path / ".booley_project").mkdir()
        result_root = mod._synth_result_root(tmp_path)
        parts = result_root.parts
        assert ".booley_project" in parts
        assert ".runtime" in parts
        assert result_root.name == "syn_result"
        assert "util" not in parts

    def test_resolve_workdir_uses_result_root(self, tmp_path):
        from booley.flows.synth.backends import configure as mod

        args = _make_args(workdir="core_a")
        with patch.object(mod, "_synth_result_root", return_value=tmp_path / "syn_result"):
            wd = mod._resolve_syn_workdir(args, "my_top", {})
        assert wd == tmp_path / "syn_result" / "core_a"

    def test_resolve_workdir_default_name(self, tmp_path):
        from booley.flows.synth.backends import configure as mod

        args = _make_args(workdir=None)
        with patch.object(mod, "_synth_result_root", return_value=tmp_path / "syn_result"):
            wd = mod._resolve_syn_workdir(args, "my_top", {})
        assert wd == tmp_path / "syn_result" / "standalone.my_top"


# ---------------------------------------------------------------------------
