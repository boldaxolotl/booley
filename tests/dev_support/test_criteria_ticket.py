"""Tests for Phase 4 — criteria system and ticket format integration.

Covers:
  - validate_criteria_section: valid/invalid criteria, structure checks
  - validate_ticket_fields: criteria + planned_stages mutual exclusion
  - TicketContext.exec_path: criteria vs legacy detection
  - _init_criteria_state: state file initialization from ticket criteria
  - check_criteria_acceptance: all-met, unmet, missing state file
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.dev_support.criteria import CriteriaTemplate
from booley.dev_support.development_state import DevelopmentState

# ===================================================================
# validate_criteria_section
# ===================================================================


class TestValidateCriteriaSection:
    """Tests for ticket_board.validation.validate_criteria_section."""

    @staticmethod
    def _validate(criteria):
        from booley.ticket_board.validation import validate_criteria_section

        return validate_criteria_section(criteria)

    def test_valid_simple(self):
        criteria = {
            "mandatory": {
                "lint_clean": ["lite", "full"],
                "review_rtl_bugs_done": "approved",
            },
        }
        assert self._validate(criteria) == []

    def test_valid_with_optional(self):
        criteria = {
            "mandatory": {"lint_clean": ["lite"]},
            "optional": {"mutation_score": {"min": 0.8}},
        }
        assert self._validate(criteria) == []

    def test_valid_sim_style(self):
        criteria = {
            "mandatory": {
                "sim_pass": ["alu_tb@lite@all", "alu_tb@full@all"],
            },
        }
        assert self._validate(criteria) == []

    def test_valid_parameterized_with_configs(self):
        criteria = {
            "mandatory": {
                "synthesis_ok": {"cell_count_max": 500, "targets": ["lite", "full"]},
            },
        }
        assert self._validate(criteria) == []

    def test_not_a_dict(self):
        errors = self._validate("not a dict")
        assert any("must be a dict" in e for e in errors)

    def test_unknown_top_level_keys(self):
        errors = self._validate({"mandatory": {"lint_clean": ["lite"]}, "extra": {}})
        assert any("unknown top-level keys" in e for e in errors)

    def test_mandatory_section_not_dict(self):
        errors = self._validate({"mandatory": "not a dict"})
        assert any("must be a dict" in e for e in errors)

    def test_empty_mandatory(self):
        errors = self._validate({"mandatory": {}})
        assert any("at least one criterion" in e for e in errors)

    def test_no_mandatory_key(self):
        errors = self._validate({"optional": {"mutation_score": {"min": 0.8}}})
        assert any("at least one criterion" in e for e in errors)

    def test_empty_string_in_list(self):
        errors = self._validate({"mandatory": {"lint_clean": [""]}})
        assert any("empty string" in e for e in errors)

    def test_invalid_list_item_type(self):
        errors = self._validate({"mandatory": {"lint_clean": [42]}})
        assert any("must be strings or dicts" in e for e in errors)

    def test_configs_not_list(self):
        errors = self._validate(
            {
                "mandatory": {"synthesis_ok": {"targets": "lite"}},
            }
        )
        assert any("targets must be a list" in e for e in errors)

    def test_scalar_values_accepted(self):
        criteria = {
            "mandatory": {
                "review_rtl_bugs_done": "approved",
                "rtl_plan_done": True,
                "mutation_score": "8/10",
            },
        }
        assert self._validate(criteria) == []

    def test_per_config_criterion_rejects_scalar(self):
        for key in ("sim_pass", "lint_clean"):
            errors = self._validate({"mandatory": {key: True}})
            assert any("per-target criterion" in e for e in errors), f"{key}: {errors}"

    def test_per_config_criterion_accepts_list(self):
        criteria = {
            "mandatory": {
                "sim_pass": ["default"],
            },
        }
        assert self._validate(criteria) == []


# ===================================================================
# validate_ticket_fields — criteria integration
# ===================================================================


class TestValidateTicketFieldsCriteria:
    """Criteria section within full ticket validation."""

    @staticmethod
    def _base_fields(**overrides):
        fields = {
            "summary": "test ticket",
            "type": "feature",
            "branch": "master",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {
                    "sim_pass": ["tb/foo_tb.sv @ lite @ all @ pass -> pass"],
                },
            },
        }
        fields.update(overrides)
        return fields

    @staticmethod
    def _validate(fields, body="## Description\ntest"):
        from booley.ticket_board.validation import validate_ticket_fields

        return validate_ticket_fields(fields, body)

    def test_criteria_valid_no_errors(self):
        fields = self._base_fields(
            criteria={
                "mandatory": {"lint_clean": ["lite"]},
            }
        )
        errors = self._validate(fields)
        assert not errors

    def test_criteria_invalid_structure_propagates(self):
        fields = self._base_fields(criteria="not a dict")
        errors = self._validate(fields)
        assert any("must be a dict" in e for e in errors)

    def test_criteria_none_to_pass_feature_valid(self):
        fields = self._base_fields(
            criteria={
                "mandatory": {
                    "sim_pass": ["tb/foo_tb.sv @ lite @ all @ none -> pass"],
                },
            }
        )
        errors = self._validate(fields)
        assert not errors

    def test_criteria_none_to_pass_refactor_valid(self):
        fields = self._base_fields(
            type="refactor",
            criteria={
                "mandatory": {
                    "sim_pass": ["tb/foo_tb.sv @ lite @ all @ none -> pass"],
                },
            },
        )
        errors = self._validate(fields)
        assert not any("Refactor" in e for e in errors)


# ===================================================================
# _init_criteria_state
# ===================================================================


class TestInitCriteriaState:
    def _make_ctx(self, tmp_path: Path, criteria=None):
        from booley.harness.models import TicketContext

        logs_dir = tmp_path / "logs" / "test-ticket"
        logs_dir.mkdir(parents=True)
        if criteria is None:
            criteria = {
                "mandatory": {
                    "sim_pass": [
                        "tb/foo_tb.sv @ lite @ all @ pass -> pass",
                        "tb/foo_tb.sv @ full @ all @ pass -> pass",
                    ],
                },
            }
        ctx = TicketContext(
            slug="test-ticket",
            ticket_path=tmp_path / "ticket.md",
            ticket_type="feature",
            branch="master",
            summary="test",
            criteria=criteria,
            project_root=tmp_path,
        )
        return ctx, logs_dir

    def test_init_from_explicit_criteria(self, tmp_path: Path):
        from unittest.mock import PropertyMock, patch

        from booley.harness.models import TicketContext
        from booley.harness.setup.intake import _init_criteria_state

        criteria = {
            "mandatory": {
                "lint_clean": ["lite", "full"],
                "review_rtl_bugs_done": "approved",
            },
            "optional": {
                "mutation_score": {"min": 0.8},
            },
        }
        ctx, logs_dir = self._make_ctx(tmp_path, criteria=criteria)
        with patch.object(
            TicketContext, "logs_dir", new_callable=PropertyMock, return_value=logs_dir
        ):
            _init_criteria_state(ctx)

        state_path = logs_dir / ".runtime" / "booley_state.json"
        assert state_path.exists()

        state = DevelopmentState.load(state_path)
        assert state.slug == "test-ticket"
        assert state.ticket_type == "feature"
        assert state.is_met("lint_clean_lite") is False
        assert state.is_met("lint_clean_full") is False
        assert state.is_met("review_rtl_bugs_done") is False
        assert "lint_clean_lite" in state.criteria
        assert state.criteria["lint_clean_lite"].mandatory is True
        assert "mutation_score" in state.criteria
        assert state.criteria["mutation_score"].mandatory is False

    def test_init_fallback_to_default_template(self, tmp_path: Path):
        from unittest.mock import PropertyMock, patch

        from booley.harness.models import TicketContext
        from booley.harness.setup.intake import _init_criteria_state

        ctx, logs_dir = self._make_ctx(tmp_path, criteria={})
        with patch.object(
            TicketContext, "logs_dir", new_callable=PropertyMock, return_value=logs_dir
        ):
            _init_criteria_state(ctx)

        state = DevelopmentState.load(logs_dir / ".runtime" / "booley_state.json")
        assert (
            "review_tb_quality_clean" in state.criteria
            or "review_rtl_bugs_clean" in state.criteria
        )

    def test_params_propagated_to_state(self, tmp_path: Path):
        """Ticket YAML thresholds (e.g. coverage_value: 90) must reach CriterionEntry.params."""
        from unittest.mock import PropertyMock, patch

        from booley.harness.models import TicketContext
        from booley.harness.setup.intake import _init_criteria_state

        criteria = {
            "mandatory": {
                "coverage_toggle": 90,
                "coverage_fsm": 100,
                "coverage_value": 85,
                "lint_clean": ["lite"],
            },
        }
        ctx, logs_dir = self._make_ctx(tmp_path, criteria=criteria)
        with patch.object(
            TicketContext, "logs_dir", new_callable=PropertyMock, return_value=logs_dir
        ):
            _init_criteria_state(ctx)

        state = DevelopmentState.load(logs_dir / ".runtime" / "booley_state.json")
        assert state.criteria["coverage_toggle"].params == {"min_pct": 90}
        assert state.criteria["coverage_fsm"].params == {"min_pct": 100}
        assert state.criteria["coverage_value"].params == {"min_pct": 85}
        # Criteria without params should have empty dict
        assert state.criteria["lint_clean_lite"].params == {}

    def test_synthesis_recipe_frozen_into_criterion_params(self, tmp_path: Path):
        from unittest.mock import PropertyMock, patch

        from booley import fusesoc_registry
        from booley.flows.synth.recipe import RECIPE_FINGERPRINT_PARAM
        from booley.harness.models import TicketContext
        from booley.harness.setup.intake import _init_criteria_state

        criteria = {
            "mandatory": {
                "synthesis_ok": {"targets": ["synth_lite"], "cell_count_max": 500},
            },
        }
        ctx, logs_dir = self._make_ctx(tmp_path, criteria=criteria)
        resolved = fusesoc_registry.ResolvedTarget(
            name="synth_lite",
            vlnv="::dut:0",
            toplevel="dut",
            eda_tool="yosys",
            flow_options={"tool": "yosys", "ppa_profile": "balanced"},
            files=(),
            parameters={},
            build_root=tmp_path,
            edam_path=tmp_path / "dut.eda.yml",
        )
        with (
            patch.object(
                TicketContext,
                "logs_dir",
                new_callable=PropertyMock,
                return_value=logs_dir,
            ),
            patch.object(fusesoc_registry, "resolve_ref"),
            patch.object(fusesoc_registry, "resolve_target", return_value=resolved),
        ):
            _init_criteria_state(ctx)

        state = DevelopmentState.load(logs_dir / ".runtime" / "booley_state.json")
        params = state.criteria["synthesis_ok_synth_lite"].params
        assert params["cell_count_max"] == 500
        assert len(params[RECIPE_FINGERPRINT_PARAM]) == 64

    def test_new_synthesis_target_defers_recipe_freeze(self, tmp_path: Path):
        """A ticket can require a Target that the developer must author."""
        from unittest.mock import PropertyMock, patch

        from booley.flows.synth.recipe import RECIPE_FINGERPRINT_PARAM
        from booley.harness.models import TicketContext
        from booley.harness.setup.intake import _init_criteria_state

        criteria = {
            "mandatory": {
                "synthesis_ok": {"targets": ["synth_new"], "cell_count_max": 500},
            },
        }
        ctx, logs_dir = self._make_ctx(tmp_path, criteria=criteria)
        with (
            patch.object(
                TicketContext,
                "logs_dir",
                new_callable=PropertyMock,
                return_value=logs_dir,
            ),
        ):
            _init_criteria_state(ctx)

        state = DevelopmentState.load(logs_dir / ".runtime" / "booley_state.json")
        params = state.criteria["synthesis_ok_synth_new"].params
        assert params["cell_count_max"] == 500
        assert RECIPE_FINGERPRINT_PARAM not in params

    def test_all_criteria_start_unmet(self, tmp_path: Path):
        from unittest.mock import PropertyMock, patch

        from booley.harness.models import TicketContext
        from booley.harness.setup.intake import _init_criteria_state

        criteria = {
            "mandatory": {
                "lint_clean": ["lite"],
                "sim_pass": ["alu_tb@lite@all"],
            },
        }
        ctx, logs_dir = self._make_ctx(tmp_path, criteria=criteria)
        with patch.object(
            TicketContext, "logs_dir", new_callable=PropertyMock, return_value=logs_dir
        ):
            _init_criteria_state(ctx)

        state = DevelopmentState.load(logs_dir / ".runtime" / "booley_state.json")
        for entry in state.criteria.values():
            assert entry.met is False


# ===================================================================
# check_criteria_acceptance
# ===================================================================


class TestCriteriaAcceptance:
    def test_all_mandatory_met_returns_review(self, tmp_path: Path):
        from booley.harness.criteria_acceptance import check_criteria_acceptance

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "test"
        state.init_criteria(
            {
                "lint_clean_lite": True,
                "sim_pass_lite": True,
                # Internal report-submission gate -- normally injected by
                # setup.intake, enforced as a special case by
                # check_criteria_acceptance.
                "_report_submitted": True,
            }
        )
        state.set_criterion("lint_clean_lite", True)
        state.set_criterion("sim_pass_lite", True)
        state.set_criterion("_report_submitted", True)
        state.save()

        verdict = check_criteria_acceptance(state_path)
        assert verdict.disposition == "review"
        assert verdict.passed is True
        assert verdict.mandatory_met == 2
        assert verdict.unmet_mandatory == []

    def test_unmet_mandatory_returns_failed(self, tmp_path: Path):
        from booley.harness.criteria_acceptance import check_criteria_acceptance

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "test"
        state.init_criteria(
            {
                "lint_clean_lite": True,
                "sim_pass_lite": True,
            }
        )
        state.set_criterion("lint_clean_lite", True)
        # sim_pass_lite left unmet
        state.save()

        verdict = check_criteria_acceptance(state_path)
        assert verdict.disposition == "failed"
        assert verdict.passed is False
        assert "sim_pass_lite" in verdict.unmet_mandatory

    def test_missing_state_file_returns_failed(self, tmp_path: Path):
        from booley.harness.criteria_acceptance import check_criteria_acceptance

        verdict = check_criteria_acceptance(tmp_path / "nonexistent.json")
        assert verdict.disposition == "failed"
        assert "not found" in verdict.blocked_reason

    def test_empty_criteria_returns_failed(self, tmp_path: Path):
        from booley.harness.criteria_acceptance import check_criteria_acceptance

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "test"
        state.save()

        verdict = check_criteria_acceptance(state_path)
        assert verdict.disposition == "failed"
        assert "no criteria" in verdict.blocked_reason

    def test_optional_unmet_still_passes(self, tmp_path: Path):
        from booley.harness.criteria_acceptance import check_criteria_acceptance

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "test"
        state.init_criteria(
            {
                "lint_clean_lite": True,
                "mutation_score": False,  # optional
                "_report_submitted": True,
            }
        )
        state.set_criterion("lint_clean_lite", True)
        state.set_criterion(
            "_report_submitted",
            True,
            detail={"unmet_optional_criteria": ["mutation_score"]},
        )
        # mutation_score left unmet (optional)
        state.save()

        verdict = check_criteria_acceptance(state_path)
        assert verdict.disposition == "review"
        assert verdict.passed is True

    def test_blocked_reason_returns_blocked(self, tmp_path: Path):
        """_blocked_reason criterion → disposition=blocked when mandatory criteria unmet."""
        from booley.harness.criteria_acceptance import check_criteria_acceptance

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "test"
        state.init_criteria(
            {
                "lint_clean_lite": True,
                "sim_pass_lite": True,
            }
        )
        state.set_criterion("lint_clean_lite", True)
        # Leave sim_pass_lite unmet -- _blocked_reason only fires when
        # mandatory criteria are NOT all satisfied (stale-block guard).
        state.set_criterion(
            "_blocked_reason",
            True,
            detail={"reason": "Missing FFT spec"},
        )
        state.save()

        verdict = check_criteria_acceptance(state_path)
        assert verdict.disposition == "blocked"
        assert verdict.blocked_reason == "Missing FFT spec"
        assert verdict.passed is False

    def test_blocked_reason_excludes_from_mandatory_count(self, tmp_path: Path):
        """_blocked_reason should NOT inflate mandatory counts."""
        from booley.harness.criteria_acceptance import check_criteria_acceptance

        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "test"
        state.init_criteria(
            {
                "lint_clean_lite": True,
                "_report_submitted": True,
            }
        )
        state.set_criterion("lint_clean_lite", True)
        state.set_criterion("_report_submitted", True)
        # _blocked_reason present but met=False → not blocking
        state.set_criterion("_blocked_reason", False)
        state.save()

        verdict = check_criteria_acceptance(state_path)
        # _blocked_reason.met is False, so not blocked
        # lint_clean_lite is met → review
        assert verdict.disposition == "review"
        # Mandatory count should be 1 (lint_clean_lite), not 2
        assert verdict.mandatory == 1

    def test_format_verdict_pass(self, tmp_path: Path):
        from booley.harness.criteria_acceptance import (
            CriteriaVerdict,
            format_criteria_verdict,
        )

        v = CriteriaVerdict(
            disposition="review",
            total=5,
            met=5,
            mandatory=4,
            mandatory_met=4,
        )
        text = format_criteria_verdict(v)
        assert "REVIEW" in text
        assert "5/5" in text

    def test_format_verdict_fail(self, tmp_path: Path):
        from booley.harness.criteria_acceptance import (
            CriteriaVerdict,
            format_criteria_verdict,
        )

        v = CriteriaVerdict(
            disposition="failed",
            total=5,
            met=3,
            mandatory=4,
            mandatory_met=3,
            unmet_mandatory=["sim_pass_lite"],
        )
        text = format_criteria_verdict(v)
        assert "FAILED" in text
        assert "sim_pass_lite" in text


# ===================================================================
# Per-config expansion round-trip
# ===================================================================


class TestCriteriaExpansionRoundTrip:
    """Verify the full YAML → expand → state-init → acceptance path."""

    def test_full_round_trip(self, tmp_path: Path):
        from booley.harness.criteria_acceptance import check_criteria_acceptance

        criteria_yaml = {
            "mandatory": {
                "lint_clean": ["lite", "full", "combo"],
                "sim_pass": ["alu_tb@lite@all"],
                "synthesis_ok": {"cell_count_max": 500, "targets": ["lite", "full", "combo"]},
                "review_rtl_bugs_done": "approved",
            },
            "optional": {
                "mutation_score": {"min": 0.8},
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
        # The developer injects _report_submitted in production; emulate
        # that here so the acceptance gate sees the report criterion.
        expanded["_report_submitted"] = True
        state.init_criteria(expanded, category_overrides=overrides)

        # Verify expanded keys
        assert "lint_clean_lite" in state.criteria
        assert "lint_clean_full" in state.criteria
        assert "lint_clean_combo" in state.criteria
        assert "sim_pass_alu_tb_lite_all" in state.criteria
        assert "synthesis_ok_lite" in state.criteria
        assert "review_rtl_bugs_done" in state.criteria
        assert "mutation_score" in state.criteria

        # All mandatory except mutation_score
        assert state.criteria["lint_clean_lite"].mandatory is True
        assert state.criteria["mutation_score"].mandatory is False

        # All start unmet
        assert state.all_mandatory_met() is False

        # Set all mandatory criteria met
        for key, entry in state.criteria.items():
            if entry.mandatory:
                state.set_criterion(key, True)
        state.set_criterion(
            "_report_submitted",
            True,
            detail={"unmet_optional_criteria": ["mutation_score"]},
        )
        state.save()

        # Acceptance should pass
        verdict = check_criteria_acceptance(state_path)
        assert verdict.passed is True
        # lint(3) + sim(1) + synth(3) + review_rtl_bugs(1) = 8 mandatory
        # (the internal _report_submitted is hidden from this count)
        assert verdict.mandatory == 8


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
