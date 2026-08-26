"""Tests for CriteriaTemplate — expansion, YAML parsing, per-config naming."""

from __future__ import annotations

import pytest

from booley.dev_support.criteria import (
    CriteriaTemplate,
    CriterionDef,
    CriterionSpec,
    TargetPair,
    _param_base_metric,
    _split_clock_scope,
    _validate_criterion_params,
    eligible_eda_tool_criterion_families,
    expand_criteria_defs,
    parse_target_pair,
)


class TestCriterionSpec:
    def test_expand_no_config(self):
        spec = CriterionSpec("review_rtl_bugs_done", mandatory=True)
        result = spec.expand(["lite", "full"])
        assert result == [("review_rtl_bugs_done", True)]

    def test_expand_per_config(self):
        spec = CriterionSpec("lint_clean", mandatory=True, per_target=True)
        result = spec.expand(["lite", "full", "combo"])
        assert result == [
            ("lint_clean_lite", True),
            ("lint_clean_full", True),
            ("lint_clean_combo", True),
        ]

    def test_expand_per_config_empty_configs(self):
        spec = CriterionSpec("lint_clean", per_target=True)
        result = spec.expand([])
        assert result == [("lint_clean", True)]


class TestTargetPair:
    def test_string_is_equal_pair(self):
        assert parse_target_pair("synth_default") == TargetPair(
            baseline="synth_default",
            candidate="synth_default",
        )

    def test_mapping_names_both_roles(self):
        assert parse_target_pair(
            {"baseline": "synth_before", "candidate": "synth_after"}
        ) == TargetPair(baseline="synth_before", candidate="synth_after")

    @pytest.mark.parametrize(
        "value",
        [
            "",
            {"baseline": "synth_before"},
            {"candidate": "synth_after"},
            {"baseline": "synth_before", "candidate": ""},
            {"baseline": "synth_before", "candidate": "synth_after", "extra": True},
        ],
    )
    def test_rejects_malformed_pair(self, value):
        with pytest.raises(ValueError):
            parse_target_pair(value)


class TestCriteriaTemplateDefaults:
    def test_feature_template(self):
        t = CriteriaTemplate.for_ticket_type("feature")
        expanded = t.expand(["lite", "full"])
        assert "lint_clean_lite" in expanded
        assert "lint_clean_full" in expanded
        assert "review_rtl_bugs_clean" in expanded
        assert "review_tb_quality_clean" in expanded
        assert "sim_pass_lite" in expanded
        # Mandatory review criteria
        assert expanded["review_rtl_bugs_clean"] is True
        assert expanded["review_tb_quality_clean"] is True

    def test_bugfix_template(self):
        t = CriteriaTemplate.for_ticket_type("bugfix")
        expanded = t.expand(["lite"])
        assert "sim_pass_lite" in expanded
        # Bugfix doesn't require lint, synthesis, or tb-review
        assert "lint_clean_lite" not in expanded
        assert "synthesis_ok_lite" not in expanded
        assert "review_tb_quality_clean" not in expanded

    def test_refactor_template(self):
        t = CriteriaTemplate.for_ticket_type("refactor")
        expanded = t.expand(["lite"])
        assert "lint_clean_lite" in expanded
        assert "sim_pass_lite" in expanded
        assert "review_rtl_bugs_clean" in expanded

    def test_unknown_type_falls_back_to_bugfix(self):
        t = CriteriaTemplate.for_ticket_type("unknown_type")
        expanded = t.expand(["lite"])
        assert "sim_pass_lite" in expanded


class TestCriteriaTemplateYAML:
    def test_simple_config_list(self):
        yaml_section = {
            "mandatory": {
                "lint_clean": ["lite", "full", "combo"],
                "review_rtl_bugs_done": "approved",
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand(["lite", "full", "combo"])
        assert "lint_clean_lite" in expanded
        assert "lint_clean_full" in expanded
        assert "lint_clean_combo" in expanded
        assert expanded["lint_clean_lite"] is True
        assert "review_rtl_bugs_done" in expanded

    def test_sim_style_explicit_keys(self):
        yaml_section = {
            "mandatory": {
                "sim_pass": [
                    "alu_tb@lite@all",
                    "alu_tb@full@all",
                ],
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand(["lite", "full"])
        assert "sim_pass_alu_tb_lite_all" in expanded
        assert "sim_pass_alu_tb_full_all" in expanded

    def test_parameterized_with_configs(self):
        yaml_section = {
            "mandatory": {
                "synthesis_ok": {
                    "cell_count_max": 500,
                    "targets": ["lite", "full"],
                },
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand(["lite", "full"])
        assert "synthesis_ok_lite" in expanded
        assert "synthesis_ok_full" in expanded

    def test_optional_criteria(self):
        yaml_section = {
            "mandatory": {"lint_clean": ["lite"]},
            "optional": {
                "mutation_score": [
                    {
                        "target": "lite",
                        "scope": ["rtl/top.sv"],
                        "min_detected": 8,
                        "total": 10,
                    }
                ],
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand(["lite"])
        assert expanded["lint_clean_lite"] is True
        assert expanded["mutation_score_lite"] is False  # optional

    def test_empty_criteria_section(self):
        t = CriteriaTemplate.from_yaml({})
        expanded = t.expand(["lite"])
        assert expanded == {}


class TestExpandParams:
    """expand_params() extracts {expanded_key: params} for specs with params."""

    def test_integer_coverage_thresholds(self):
        yaml_section = {
            "mandatory": {
                "coverage_toggle": 90,
                "coverage_value": 85,
                "coverage_branch": 80,
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        params = t.expand_params([])
        assert params["coverage_toggle"] == {"min_pct": 90}
        assert params["coverage_value"] == {"min_pct": 85}
        assert params["coverage_branch"] == {"min_pct": 80}

    def test_specs_without_params_excluded(self):
        yaml_section = {
            "mandatory": {
                "lint_clean": ["lite", "full"],
                "coverage_value": 90,
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        params = t.expand_params(["lite", "full"])
        assert "lint_clean_lite" not in params
        assert "lint_clean_full" not in params
        assert params["coverage_value"] == {"min_pct": 90}

    def test_empty_template_returns_empty(self):
        t = CriteriaTemplate.from_yaml({})
        assert t.expand_params(["lite"]) == {}

    def test_per_config_params_propagate(self):
        yaml_section = {
            "mandatory": {
                "synthesis_ok": {
                    "cell_count_max": 500,
                    "targets": ["lite", "full"],
                },
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        params = t.expand_params(["lite", "full"])
        assert "synthesis_ok_lite" in params
        assert "synthesis_ok_full" in params
        assert params["synthesis_ok_lite"]["cell_count_max"] == 500


class TestReviewBaseKeyExpansion:
    """Bare review keys are durable clean gates; _done stays explicit."""

    def test_review_rtl_bugs_expands(self):
        yaml_section = {
            "mandatory": {"review_rtl_bugs": True},
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand([])
        assert expanded["review_rtl_bugs_clean"] is True
        assert "review_rtl_bugs" not in expanded

    def test_review_tb_quality_expands(self):
        yaml_section = {
            "mandatory": {"review_tb_quality": True},
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand([])
        assert expanded["review_tb_quality_clean"] is True

    def test_optional_review_key_expands_correctly(self):
        yaml_section = {
            "optional": {"review_rtl_security": True},
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand([])
        assert expanded["review_rtl_security_clean"] is False  # optional

    def test_explicit_done_suffix_not_double_expanded(self):
        yaml_section = {
            "mandatory": {"review_rtl_bugs_done": "approved"},
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand([])
        assert "review_rtl_bugs_done" in expanded
        assert "review_rtl_bugs_done_done" not in expanded

    def test_explicit_clean_suffix_not_double_expanded(self):
        yaml_section = {
            "optional": {"review_tb_clean": True},
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand([])
        assert "review_tb_clean" in expanded
        assert "review_tb_clean_done" not in expanded

    def test_typical_ticket_criteria(self):
        """Matches what existing tickets actually write."""
        yaml_section = {
            "mandatory": {
                "lint_clean": ["default"],
                "sim_pass": ["verif/tb.sv @ default @ fail -> pass"],
                "review_rtl_bugs": True,
                "review_tb_quality": True,
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand(["default"])
        assert expanded["review_rtl_bugs_clean"] is True
        assert expanded["review_tb_quality_clean"] is True


class TestCriteriaTemplateModification:
    def test_add_spec(self):
        t = CriteriaTemplate.for_ticket_type("bugfix")
        t.add(CriterionSpec("custom_check", mandatory=False))
        expanded = t.expand(["lite"])
        assert "custom_check" in expanded
        assert expanded["custom_check"] is False

    def test_remove_spec(self):
        t = CriteriaTemplate.for_ticket_type("feature")
        removed = t.remove("review_tb_quality_clean")
        assert removed is True
        expanded = t.expand(["lite"])
        assert "review_tb_quality_clean" not in expanded

    def test_remove_nonexistent(self):
        t = CriteriaTemplate.for_ticket_type("bugfix")
        removed = t.remove("nonexistent")
        assert removed is False


class TestToolKeyAliases:
    """Tests for CriteriaTemplate.flow_key_aliases() — structured entry alias map."""

    def test_structured_sim_entry_produces_alias(self):
        yaml_section = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb_aes128_dec.sv @ default @ fail -> pass",
                ],
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        aliases = t.flow_key_aliases()
        assert "sim_pass_default" in aliases
        assert "sim_pass_verif_tb_aes128_dec.sv_default" in aliases["sim_pass_default"]

    def test_multiple_tbs_same_config_grouped(self):
        yaml_section = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb_a.sv @ default @ fail -> pass",
                    "verif/tb_b.sv @ default @ fail -> pass",
                ],
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        aliases = t.flow_key_aliases()
        assert len(aliases["sim_pass_default"]) == 2
        assert "sim_pass_verif_tb_a.sv_default" in aliases["sim_pass_default"]
        assert "sim_pass_verif_tb_b.sv_default" in aliases["sim_pass_default"]

    def test_structured_none_to_pass_produces_alias(self):
        yaml_section = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb_foo.sv @ default @ none -> pass",
                ],
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        aliases = t.flow_key_aliases()
        assert "sim_pass_default" in aliases
        assert "sim_pass_verif_tb_foo.sv_default" in aliases["sim_pass_default"]

    def test_transition_from_state_is_carried_into_params(self):
        """The `fail` leg must survive parsing, not be silently dropped (F-53)."""
        t = CriteriaTemplate.from_yaml(
            {"mandatory": {"sim_pass": ["verif/tb_foo.sv @ default @ fail -> pass"]}}
        )
        params = t.expand_params(["default"])
        assert params["sim_pass_verif_tb_foo.sv_default"]["from_state"] == "fail"
        assert params["sim_pass_verif_tb_foo.sv_default"]["test_selector"] == "all"

    def test_pass_and_none_legs_are_distinguishable_from_fail(self):
        for leg in ("pass", "none"):
            t = CriteriaTemplate.from_yaml(
                {"mandatory": {"sim_pass": [f"verif/tb_foo.sv @ default @ {leg} -> pass"]}}
            )
            params = t.expand_params(["default"])
            assert params["sim_pass_verif_tb_foo.sv_default"]["from_state"] == leg

    def test_different_configs_separate_aliases(self):
        yaml_section = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb.sv @ lite @ fail -> pass",
                    "verif/tb.sv @ full @ fail -> pass",
                ],
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        aliases = t.flow_key_aliases()
        assert "sim_pass_lite" in aliases
        assert "sim_pass_full" in aliases

    def test_simple_config_list_no_aliases(self):
        yaml_section = {
            "mandatory": {
                "sim_pass": ["lite", "full"],
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        aliases = t.flow_key_aliases()
        assert aliases == {}

    def test_no_aliases_for_scalar_criteria(self):
        yaml_section = {
            "mandatory": {
                "review_rtl_bugs_done": True,
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        assert t.flow_key_aliases() == {}

    def test_structured_with_test_name(self):
        yaml_section = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb.sv @ lite @ test_encrypt @ fail -> pass",
                ],
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        aliases = t.flow_key_aliases()
        assert "sim_pass_lite" in aliases
        assert "sim_pass_verif_tb.sv_lite_test_encrypt" in aliases["sim_pass_lite"]

    def test_x2_suffix_no_longer_expands_dual_tb_criteria(self):
        yaml_section = {
            "mandatory": {
                "sim_pass": [
                    "verif/lane1/tb_fifo.sv @ default @ none -> pass x2",
                ],
                "review_tb_quality_clean": True,
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand(["default"])
        assert "sim_pass_verif_lane1_tb_fifo.sv_default" in expanded
        assert "sim_pass_verif_lane2_tb_fifo.sv_default" not in expanded
        assert "review_tb_quality_clean__x2" not in expanded
        aliases = t.flow_key_aliases()
        assert aliases["sim_pass_default"] == [
            "sim_pass_verif_lane1_tb_fifo.sv_default",
        ]
        assert "sim_pass_default__x2" not in aliases


# ===========================================================================
# synthesis_ok parsing and validation
# ===========================================================================


class TestSynthesisOkParsing:
    def test_short_form_expansion(self):
        """synthesis_ok: [lite, full] expands per-config."""
        yaml_section = {
            "mandatory": {"synthesis_ok": ["lite", "full"]},
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand(["lite", "full"])
        assert "synthesis_ok_lite" in expanded
        assert "synthesis_ok_full" in expanded

    def test_block_form_with_params(self):
        """synthesis_ok with configs + threshold params."""
        yaml_section = {
            "mandatory": {
                "synthesis_ok": {
                    "targets": ["lite", "full"],
                    "cell_count_max": 500,
                    "area_reduce_at_least": 10,
                },
            },
        }
        t = CriteriaTemplate.from_yaml(yaml_section)
        expanded = t.expand(["lite", "full"])
        assert "synthesis_ok_lite" in expanded
        assert "synthesis_ok_full" in expanded
        # Params are stored in the spec
        synth_spec = next(s for s in t.specs if s.name == "synthesis_ok")
        assert synth_spec.params["cell_count_max"] == 500
        assert synth_spec.params["area_reduce_at_least"] == 10

    def test_paired_target_expands_by_candidate(self):
        yaml_section = {
            "mandatory": {
                "synthesis_ok": {
                    "targets": [{"baseline": "synth_before", "candidate": "synth_after"}],
                    "area_reduce_at_least": 10,
                }
            }
        }

        template = CriteriaTemplate.from_yaml(yaml_section)

        assert template.expand([]) == {"synthesis_ok_synth_after": True}
        assert template.expand_params([])["synthesis_ok_synth_after"] == {
            "area_reduce_at_least": 10,
            "_baseline_target": "synth_before",
        }

    def test_mixed_legacy_and_paired_targets(self):
        yaml_section = {
            "mandatory": {
                "synthesis_ok": {
                    "targets": [
                        "synth_default",
                        {"baseline": "synth_before", "candidate": "synth_after"},
                    ],
                    "area_reduce_at_least": 10,
                }
            }
        }

        template = CriteriaTemplate.from_yaml(yaml_section)

        assert template.expand([]) == {
            "synthesis_ok_synth_default": True,
            "synthesis_ok_synth_after": True,
        }
        assert template.expand_params([])["synthesis_ok_synth_default"] == {
            "area_reduce_at_least": 10
        }

    def test_pair_requires_relative_threshold(self):
        yaml_section = {
            "mandatory": {
                "synthesis_ok": {
                    "targets": [{"baseline": "synth_before", "candidate": "synth_after"}],
                    "cell_count_max": 500,
                }
            }
        }

        with pytest.raises(ValueError, match="relative threshold"):
            CriteriaTemplate.from_yaml(yaml_section)

    def test_conflicting_candidate_baselines_are_rejected(self):
        yaml_section = {
            "mandatory": {
                "synthesis_ok": {
                    "targets": [
                        {"baseline": "synth_a", "candidate": "synth_after"},
                        {"baseline": "synth_b", "candidate": "synth_after"},
                    ],
                    "area_reduce_at_least": 10,
                }
            }
        }

        with pytest.raises(ValueError, match="conflicting baselines"):
            CriteriaTemplate.from_yaml(yaml_section)

    def test_conflicting_pair_across_sections_is_rejected(self):
        yaml_section = {
            "mandatory": {
                "synthesis_ok": {
                    "targets": [{"baseline": "synth_a", "candidate": "synth_after"}],
                    "area_reduce_at_least": 10,
                }
            },
            "optional": {
                "synthesis_ok": {
                    "targets": [{"baseline": "synth_b", "candidate": "synth_after"}],
                    "area_reduce_at_least": 5,
                }
            },
        }

        with pytest.raises(ValueError, match="across criteria sections"):
            CriteriaTemplate.from_yaml(yaml_section)


class TestClockScopedParamValidation:
    """Clock-scoped params ``<clock>.<param>`` are accepted only for per-clock
    timing metrics (Fmax/critical-path/slack/period); area/counts stay flat.

    Mutex pairs are enforced *per scope* — clashing within one clock, but the
    same pair split across two clocks is fine.
    """

    def test_flat_params_still_accepted(self):
        # The unchanged flat frozenset path: no clock scope, no error.
        _validate_criterion_params(
            "synthesis_ok",
            {"cell_count_max": 500, "fmax_mhz_min": 400},
        )

    def test_clock_scoped_per_clock_metric_accepted(self):
        # fmax_mhz / critical_path_ps are per-clock → clock scope is valid.
        _validate_criterion_params(
            "synthesis_ok",
            {"clk_i.fmax_mhz_min": 400, "clk_2x.critical_path_ps_max": 800},
        )

    def test_clock_scoped_delta_param_accepted(self):
        _validate_criterion_params(
            "synthesis_ok",
            {"clk_i.critical_path_ps_increase_at_most": 5},
        )

    def test_clock_scope_rejected_for_non_per_clock_metric(self):
        # area is not a per-clock metric — clock-scoping it is invalid.
        with pytest.raises(ValueError, match="Unknown synthesis_ok params"):
            _validate_criterion_params("synthesis_ok", {"clk_i.area_kge_max": 10})

    def test_unknown_base_param_rejected(self):
        with pytest.raises(ValueError, match="Unknown synthesis_ok params"):
            _validate_criterion_params("synthesis_ok", {"x.bogus_min": 1})

    def test_mutex_rejected_within_a_clock(self):
        # critical_path_ps_max and fmax_mhz_min are mutually exclusive; both
        # scoped to clk_i → rejected, and the message names the clock.
        with pytest.raises(ValueError, match=r"mutually exclusive.*clk_i"):
            _validate_criterion_params(
                "synthesis_ok",
                {"clk_i.critical_path_ps_max": 800, "clk_i.fmax_mhz_min": 400},
            )

    def test_mutex_allowed_across_different_clocks(self):
        # Same mutex pair, but each side scoped to a different clock → allowed.
        _validate_criterion_params(
            "synthesis_ok",
            {"clk_i.fmax_mhz_min": 400, "clk_2x.critical_path_ps_max": 800},
        )

    def test_flat_mutex_still_rejected(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            _validate_criterion_params(
                "synthesis_ok",
                {"critical_path_ps_max": 800, "fmax_mhz_min": 400},
            )

    def test_fpga_impl_clock_scoped_accepted(self):
        _validate_criterion_params(
            "fpga_impl_ok",
            {"clk_i.fmax_mhz_min": 300, "lut_count_max": 1000},
        )

    def test_clock_scoped_param_must_be_positive(self):
        with pytest.raises(ValueError, match="positive number"):
            _validate_criterion_params("synthesis_ok", {"clk_i.fmax_mhz_min": -1})

    @pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
    def test_numeric_param_rejects_bool_and_non_finite_values(self, value):
        with pytest.raises(ValueError, match="positive number"):
            _validate_criterion_params("synthesis_ok", {"clk_i.fmax_mhz_min": value})


class TestClockScopeHelpers:
    def test_split_clock_scope_flat(self):
        assert _split_clock_scope("fmax_mhz_min") == ("", "fmax_mhz_min")

    def test_split_clock_scope_scoped(self):
        assert _split_clock_scope("clk_i.fmax_mhz_min") == ("clk_i", "fmax_mhz_min")

    def test_param_base_metric_strips_suffixes(self):
        assert _param_base_metric("fmax_mhz_min") == "fmax_mhz"
        assert _param_base_metric("critical_path_ps_max") == "critical_path_ps"
        assert _param_base_metric("area_increase_at_most") == "area"
        assert _param_base_metric("cell_count_reduce_at_least") == "cell_count"


# ---------------------------------------------------------------------------
# Decision-11 tool → criterion-family eligibility
# ---------------------------------------------------------------------------


def _per_config_def(name: str) -> CriterionDef:
    return CriterionDef(
        name=name,
        description=name,
        workflow_region="core_loop",
        per_target=True,
        category="rtl",
        group="other",
    )


class TestCriterionEligibility:
    def test_eligible_families_per_tool(self):
        assert eligible_eda_tool_criterion_families("yosys") == frozenset({"synthesis_ok"})
        assert eligible_eda_tool_criterion_families("verilator") == frozenset(
            {"sim_pass", "lint_clean"}
        )
        assert eligible_eda_tool_criterion_families("icarus") == frozenset({"sim_pass"})
        assert eligible_eda_tool_criterion_families("iverilog") == frozenset({"sim_pass"})
        assert eligible_eda_tool_criterion_families("unknown_tool") == frozenset()
        # Unsupported commercial simulators have no criterion eligibility.
        assert eligible_eda_tool_criterion_families("xcelium") == frozenset()
        assert eligible_eda_tool_criterion_families("vcs") == frozenset()

    def test_expand_unfiltered_without_target_tools(self):
        """No tool map → every config gets every per-config criterion."""
        defs = [_per_config_def("sim_pass"), _per_config_def("synthesis_ok")]
        out = expand_criteria_defs(defs, ["a", "b"])
        assert set(out) == {
            "sim_pass_a",
            "sim_pass_b",
            "synthesis_ok_a",
            "synthesis_ok_b",
        }

    def test_expand_filters_by_target_tool(self):
        """A yosys Target carries synthesis_ok but not sim_pass (decision 11)."""
        defs = [_per_config_def("sim_pass"), _per_config_def("synthesis_ok")]
        tools = {"sim_cfg": "verilator", "syn_cfg": "yosys"}
        out = expand_criteria_defs(defs, ["sim_cfg", "syn_cfg"], tools)
        assert set(out) == {"sim_pass_sim_cfg", "synthesis_ok_syn_cfg"}

    def test_unknown_tool_is_not_filtered(self):
        """A config whose tool is None (pre-migration) keeps every criterion."""
        defs = [_per_config_def("sim_pass"), _per_config_def("synthesis_ok")]
        out = expand_criteria_defs(defs, ["x"], {"x": None})
        assert set(out) == {"sim_pass_x", "synthesis_ok_x"}

    def test_non_tool_gated_family_always_kept(self):
        """A non-tool-gated family (e.g. coverage) is never filtered."""
        defs = [_per_config_def("coverage_toggle")]
        out = expand_criteria_defs(defs, ["c"], {"c": "yosys"})
        assert set(out) == {"coverage_toggle_c"}
