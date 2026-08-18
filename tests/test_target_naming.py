"""Tests for target_naming: the ``<axis>_<subject>`` Target naming convention."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booley import target_naming
from booley.target_surface import TARGET_AWARE_FLOWS


class TestAxisVocabulary:
    def test_every_target_aware_flow_has_exactly_one_axis(self):
        """A Booley Flow with no axis would make its Targets unnameable under the rule."""
        assert set(target_naming.AXIS_FOR_FLOW) == set(TARGET_AWARE_FLOWS)

    def test_elaborate_shares_sims_axis(self):
        """elaborate builds a sim Target without running it, never its own Target."""
        assert target_naming.AXIS_FOR_FLOW["elab"] == "sim"
        assert target_naming.AXIS_FOR_FLOW["sim"] == "sim"

    def test_synth_and_fpga_are_distinct_axes(self):
        """The whole reason the axis is in the name: CAPI2 cannot tell them apart."""
        assert target_naming.AXIS_FOR_FLOW["synth"] == "synth"
        assert target_naming.AXIS_FOR_FLOW["fpga"] == "fpga"


class TestAxisOf:
    @pytest.mark.parametrize(
        "name,axis",
        [
            ("sim_soc", "sim"),
            ("sim", "sim"),  # bare axis: a single-config project has nothing to qualify
            ("synth_matmul_par_b8", "synth"),
            ("fpga_soc", "fpga"),
            ("lint_style", "lint"),
        ],
    )
    def test_recognized(self, name: str, axis: str):
        assert target_naming.axis_of(name) == axis

    @pytest.mark.parametrize("name", ["soc_sim", "simulate_soc", "simsoc", "asic"])
    def test_not_recognized(self, name: str):
        """`simsoc` matters: the axis is a whole segment, not a string prefix."""
        assert target_naming.axis_of(name) is None


class TestViolation:
    def test_conformant_name_has_no_violation(self):
        assert target_naming.violation("sim_soc") is None

    def test_fusesoc_default_is_exempt(self):
        """`default` is FuseSoC plumbing; it carries its own doctor finding."""
        assert target_naming.violation("default") is None
        assert target_naming.is_conventional("default")

    def test_missing_axis_is_named_as_such(self):
        assert "no axis prefix" in (target_naming.violation("soc_sim") or "")

    @pytest.mark.parametrize("name", ["Sim_SOC", "sim-soc", "sim__soc", "sim_"])
    def test_non_snake_case_is_reported_before_the_axis(self, name: str):
        """A name that is not snake_case fails on shape, whatever prefix it wears."""
        assert target_naming.violation(name) == "not lowercase_snake_case"


class TestSuggestName:
    @pytest.mark.parametrize(
        "legacy,expected",
        [
            ("soc_sim", "sim_soc"),
            ("matmul_par_b8_synth", "synth_matmul_par_b8"),
            ("style_lint", "lint_style"),
            ("soc_fpga", "fpga_soc"),
            ("soc_lite_lint", "lint_soc_lite"),
            ("soc_xcelium_sim", "sim_soc_xcelium"),
        ],
    )
    def test_legacy_suffix_drives_the_rename(self, legacy: str, expected: str):
        assert target_naming.suggest_name(legacy) == expected

    def test_bare_legacy_word_collapses_to_the_bare_axis(self):
        assert target_naming.suggest_name("synth") == "synth"

    def test_explicit_axis_overrides_the_suffix(self):
        """`[flows.*].default_target` wiring is stronger evidence than a trailing word.

        A project may declare its synth Targets as `flow: lint` resolution
        vehicles named `*_synth`; one wired to fpga_impl means fpga, not asic.
        """
        assert target_naming.suggest_name("soc_synth", "fpga") == "fpga_soc"

    def test_no_axis_evidence_yields_no_suggestion(self):
        """Better silent than a confidently wrong rename."""
        assert target_naming.suggest_name("soc") is None

    def test_leading_legacy_word_is_replaced_not_prepended(self):
        """An old `asic_core` Target becomes `synth_core`, without stuttering."""
        assert target_naming.suggest_name("asic_core", "synth") == "synth_core"
        assert target_naming.suggest_name("impl_top", "fpga") == "fpga_top"

    def test_leading_word_for_a_different_axis_is_kept(self):
        """`sim_core` wired to synth keeps `sim` — it is a real subject token
        there, not a redundant restatement of the synth axis."""
        assert target_naming.suggest_name("sim_core", "synth") == "synth_sim_core"

    def test_suggestions_are_themselves_conformant(self):
        for legacy in ("soc_sim", "matmul_par_b8_synth", "style_lint", "synth"):
            suggested = target_naming.suggest_name(legacy)
            assert suggested is not None
            assert target_naming.violation(suggested) is None
