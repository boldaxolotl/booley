"""Criteria expansion and tool aliases persist in state."""

from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import CriteriaTemplate


class TestCriteriaExpansionRoundTrip:
    """Verify YAML expansion and persisted state satisfaction."""

    def test_full_round_trip(self, tmp_path: Path):

        criteria_yaml = {
            "mandatory": {
                "lint_clean": ["lite", "full", "combo"],
                "sim_pass": ["alu_tb@lite@all"],
                "synthesis_ok": {"cell_count_max": 500, "targets": ["lite", "full", "combo"]},
                "review_rtl_bugs_done": "approved",
            },
            "optional": {
                "mutation_score": [
                    {
                        "target": "sim_unit",
                        "scope": ["rtl/dut.sv"],
                        "min_detected": 8,
                        "total": 10,
                    }
                ],
            },
        }

        template = CriteriaTemplate.from_yaml(criteria_yaml)
        configs = ["lite", "full", "combo"]
        expanded = template.expand(configs)
        overrides = template.category_overrides(configs)

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "round-trip"
        state.ticket_type = "feature"
        state.init_criteria(expanded, category_overrides=overrides)

        # Verify expanded keys
        assert "lint_clean_lite" in state.criteria
        assert "lint_clean_full" in state.criteria
        assert "lint_clean_combo" in state.criteria
        assert "sim_pass_alu_tb_lite_all" in state.criteria
        assert "synthesis_ok_lite" in state.criteria
        assert "review_rtl_bugs_done" in state.criteria
        assert "mutation_score_sim_unit" in state.criteria

        # All mandatory except mutation_score
        assert state.criteria["lint_clean_lite"].mandatory is True
        assert state.criteria["mutation_score_sim_unit"].mandatory is False

        # All start unmet
        assert state.all_mandatory_met() is False

        # Set all mandatory criteria met
        for key, entry in state.criteria.items():
            if entry.mandatory:
                state.set_criterion(key, True)
        state.save()

        assert DevelopmentState.load(state_path).all_mandatory_met()


class TestToolKeyAliasResolution:
    """Verify that tools setting generic per-config keys fan out to
    file-specific criteria via the alias map."""

    def test_alias_fanout_sets_file_specific_criteria(self, tmp_path: Path):
        """Simulate tool sets sim_pass_default → resolves to file-specific key."""
        criteria_yaml = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb_aes128_dec.sv @ default @ fail -> pass",
                ],
                "review_rtl_bugs_done": True,
            },
        }
        template = CriteriaTemplate.from_yaml(criteria_yaml)
        expanded = template.expand(["default"])
        aliases = template.flow_key_aliases()

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "alias-test"
        state.ticket_type = "bugfix"
        state.init_criteria(expanded, flow_key_aliases=aliases)

        # Verify file-specific keys were initialized
        assert "sim_pass_verif_tb_aes128_dec.sv_default" in state.criteria

        # Generic keys should NOT be in criteria (they're aliases)
        assert "sim_pass_default" not in state.criteria

        # Tool sets generic key — should fan out to file-specific
        state.set_criterion("sim_pass_default", True, detail={"tests_passed": 1, "tests_total": 1})
        state.set_criterion("review_rtl_bugs_done", True)

        assert state.criteria["sim_pass_verif_tb_aes128_dec.sv_default"].met is True
        assert state.criteria["sim_pass_verif_tb_aes128_dec.sv_default"].detail == {
            "tests_passed": 1,
            "tests_total": 1,
        }
        assert state.all_mandatory_met() is True

    def test_alias_fanout_multiple_tbs(self, tmp_path: Path):
        """Generic key fans out to multiple file-specific keys."""
        criteria_yaml = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb_a.sv @ default @ fail -> pass",
                    "verif/tb_b.sv @ default @ fail -> pass",
                ],
            },
        }
        template = CriteriaTemplate.from_yaml(criteria_yaml)
        expanded = template.expand(["default"])
        aliases = template.flow_key_aliases()

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.init_criteria(expanded, flow_key_aliases=aliases)

        # Setting generic key updates both file-specific keys
        state.set_criterion("sim_pass_default", True)
        assert state.criteria["sim_pass_verif_tb_a.sv_default"].met is True
        assert state.criteria["sim_pass_verif_tb_b.sv_default"].met is True

    @pytest.mark.parametrize(
        ("actual_selector", "expected_met"),
        [("run_test_tx", False), ("all", True)],
    )
    def test_all_tests_alias_requires_full_suite(
        self,
        tmp_path: Path,
        actual_selector: str,
        expected_met: bool,
    ):
        template = CriteriaTemplate.from_yaml(
            {
                "mandatory": {
                    "sim_pass": [
                        "verif/tb.sv @ default @ all @ pass -> pass",
                    ]
                }
            }
        )
        state = DevelopmentState.load(tmp_path / "booley_state.json")
        state.init_criteria(
            template.expand(["default"]),
            criterion_params=template.expand_params(["default"]),
            flow_key_aliases=template.flow_key_aliases(),
        )

        state.set_criterion(
            "sim_pass_default",
            True,
            detail={
                "test_selector": actual_selector,
                "selected_tests": ["run_test_tx"],
            },
        )

        assert state.is_met("sim_pass_default") is expected_met

    def test_alias_does_not_create_generic_key(self, tmp_path: Path):
        """When aliases resolve, the generic key is NOT created in criteria."""
        criteria_yaml = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb.sv @ lite @ fail -> pass",
                ],
            },
        }
        template = CriteriaTemplate.from_yaml(criteria_yaml)
        expanded = template.expand(["lite"])
        aliases = template.flow_key_aliases()

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.init_criteria(expanded, flow_key_aliases=aliases)

        state.set_criterion("sim_pass_lite", True)
        # Generic key should not exist — only the file-specific one
        assert "sim_pass_lite" not in state.criteria
        assert "sim_pass_verif_tb.sv_lite" in state.criteria
        assert state.criteria["sim_pass_verif_tb.sv_lite"].met is True

    def test_direct_key_still_works(self, tmp_path: Path):
        """When criteria use simple config lists, direct key setting works as before."""
        criteria_yaml = {
            "mandatory": {
                "sim_pass": ["lite", "full"],
            },
        }
        template = CriteriaTemplate.from_yaml(criteria_yaml)
        expanded = template.expand(["lite", "full"])
        aliases = template.flow_key_aliases()
        assert aliases == {}

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.init_criteria(expanded, flow_key_aliases=aliases)

        state.set_criterion("sim_pass_lite", True)
        assert state.criteria["sim_pass_lite"].met is True

    def test_alias_persists_through_save_load(self, tmp_path: Path):
        """Alias map survives JSON round-trip."""
        criteria_yaml = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb.sv @ default @ fail -> pass",
                ],
            },
        }
        template = CriteriaTemplate.from_yaml(criteria_yaml)
        expanded = template.expand(["default"])
        aliases = template.flow_key_aliases()

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "persist-test"
        state.init_criteria(expanded, flow_key_aliases=aliases)
        state.save()

        # Reload and verify alias resolution still works
        state2 = DevelopmentState.load(state_path)
        state2.set_criterion("sim_pass_default", True)
        assert state2.criteria["sim_pass_verif_tb.sv_default"].met is True
