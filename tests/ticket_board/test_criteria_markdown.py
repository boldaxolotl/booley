"""Tests for criteria_markdown: render, parse, inject, strip, round-trip."""

from __future__ import annotations

import pytest

from booley.ticket_board.criteria_markdown import (
    inject_criteria_into_body,
    parse_criteria_section,
    render_criteria_section,
    strip_criteria_from_body,
)
from booley.ticket_board.frontmatter import format_frontmatter, parse_frontmatter

# ---------------------------------------------------------------------------
# Fixtures — representative criteria dicts
# ---------------------------------------------------------------------------

REAL_TICKET_CRITERIA = {
    "mandatory": {
        "review_rtl_bugs_clean": True,
        "review_tb_quality_clean": True,
        "sim_pass": ["verif/tb_aes128_enc.sv @ default @ none -> pass"],
        "elab_pass": ["verif/tb_aes128_enc.sv @ default @ none -> pass"],
        "coverage_toggle": 90,
        "coverage_fsm": 100,
        "coverage_value": 90,
        "coverage_branch": 80,
        "coverage_expression": 80,
    }
}

CRITERIA_WITH_OPTIONAL = {
    "mandatory": {
        "sim_pass": ["verif/tb.sv @ default @ none -> pass"],
        "elab_pass": ["verif/tb.sv @ default @ none -> pass"],
        "coverage_toggle": 90,
        "coverage_fsm": 100,
    },
    "optional": {
        "mutation_score": "8/10",
        "rtl_plan_done": True,
    },
}

CRITERIA_WITH_LINT = {
    "mandatory": {
        "sim_pass": ["verif/tb_iir_filter.sv @ default @ none -> pass"],
        "elab_pass": ["verif/tb_iir_filter.sv @ default @ none -> pass"],
        "lint_clean": ["default"],
        "review_rtl_bugs_clean": True,
        "review_tb_quality_clean": True,
        "coverage_toggle": 90,
        "coverage_fsm": 100,
        "coverage_value": 90,
        "coverage_branch": 80,
        "coverage_expression": 80,
    }
}

CRITERIA_WITH_SYNTHESIS = {
    "mandatory": {
        "sim_pass": ["verif/tb.sv @ default @ none -> pass"],
        "synthesis_ok": {
            "configs": ["default", "lite"],
            "cell_count_reduce_at_least": 20,
            "wire_count_reduce_at_least": 20,
        },
    }
}

CRITERIA_WITH_DIRECTED_SYNTHESIS_TARGET = {
    "mandatory": {
        "synthesis_ok": {
            "targets": [
                {
                    "baseline": "synth_core",
                    "candidate": "synth_core_zbb",
                }
            ],
            "cell_count_increase_at_most": 11,
            "critical_path_ps_increase_at_most": 3,
        }
    }
}

CRITERIA_WITH_TARGET_CAMPAIGNS = {
    "mandatory": {
        "sim_pass": ["verif/tb.sv @ sim_core @ smoke @ pass -> pass"],
        "coverage_toggle": [
            {
                "target": "sim_core",
                "scope": ["rtl/core.sv", "rtl/decoder.sv"],
                "min_pct": 95,
            }
        ],
    },
    "optional": {
        "mutation_score": [
            {
                "target": "sim_core",
                "scope": ["rtl/core.sv"],
                "min_detected": 14,
                "total": 15,
            }
        ]
    },
}


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """parse(render(d)) == d for all criterion types."""

    @pytest.mark.parametrize(
        "criteria",
        [
            REAL_TICKET_CRITERIA,
            CRITERIA_WITH_OPTIONAL,
            CRITERIA_WITH_LINT,
            CRITERIA_WITH_SYNTHESIS,
            CRITERIA_WITH_DIRECTED_SYNTHESIS_TARGET,
            CRITERIA_WITH_TARGET_CAMPAIGNS,
        ],
        ids=[
            "real_ticket",
            "with_optional",
            "with_lint",
            "with_synthesis",
            "with_directed_synthesis_target",
            "with_target_campaigns",
        ],
    )
    def test_round_trip(self, criteria):
        rendered = render_criteria_section(criteria)
        parsed = parse_criteria_section(rendered)
        assert parsed == criteria

    def test_round_trip_mandatory_only(self):
        criteria = {"mandatory": {"sim_pass": ["verif/tb.sv @ default @ none -> pass"]}}
        rendered = render_criteria_section(criteria)
        parsed = parse_criteria_section(rendered)
        assert parsed == criteria

    def test_round_trip_boolean_false(self):
        criteria = {"mandatory": {"lint_strict": False}}
        rendered = render_criteria_section(criteria)
        parsed = parse_criteria_section(rendered)
        assert parsed == criteria

    def test_round_trip_multiple_sim_entries(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "verif/tb1.sv @ default @ all @ pass -> pass",
                    "verif/tb2.sv @ config2 @ all @ pass -> pass",
                ],
            }
        }
        rendered = render_criteria_section(criteria)
        parsed = parse_criteria_section(rendered)
        assert parsed == criteria

    def test_round_trip_scalar_string(self):
        criteria = {"mandatory": {"mutation_score": "8/10"}}
        rendered = render_criteria_section(criteria)
        parsed = parse_criteria_section(rendered)
        assert parsed == criteria

    def test_round_trip_integer_value(self):
        criteria = {"mandatory": {"max_area": 500}}
        rendered = render_criteria_section(criteria)
        parsed = parse_criteria_section(rendered)
        assert parsed == criteria


# ---------------------------------------------------------------------------
# Grouped rendering/parsing
# ---------------------------------------------------------------------------


class TestCoverageGrouping:
    def test_coverage_grouped_into_single_bullet(self):
        criteria = {
            "mandatory": {
                "coverage_toggle": 90,
                "coverage_fsm": 100,
                "coverage_value": 90,
            }
        }
        rendered = render_criteria_section(criteria)
        assert "- **coverage**:" in rendered
        assert "toggle >= 90" in rendered
        assert "fsm >= 100" in rendered
        assert "value >= 90" in rendered
        assert "coverage_toggle" not in rendered
        assert "coverage_fsm" not in rendered

    def test_coverage_parses_gte_operator(self):
        body = "## Criteria\n\n- **coverage**: toggle >= 90, fsm >= 100"
        parsed = parse_criteria_section(body)
        assert parsed["mandatory"]["coverage_toggle"] == 90
        assert parsed["mandatory"]["coverage_fsm"] == 100

    def test_coverage_parses_eq_operator(self):
        body = "## Criteria\n\n- **coverage**: toggle = 90, fsm = 100"
        parsed = parse_criteria_section(body)
        assert parsed["mandatory"]["coverage_toggle"] == 90
        assert parsed["mandatory"]["coverage_fsm"] == 100

    def test_coverage_parses_unicode_gte(self):
        body = "## Criteria\n\n- **coverage**: toggle ≥ 90"
        parsed = parse_criteria_section(body)
        assert parsed["mandatory"]["coverage_toggle"] == 90


class TestReviewGrouping:
    def test_reviews_grouped_into_single_bullet(self):
        criteria = {
            "mandatory": {
                "review_rtl_bugs_clean": True,
                "review_tb_quality_clean": True,
            }
        }
        rendered = render_criteria_section(criteria)
        assert "- **reviews**:" in rendered
        assert "rtl_bugs, tb_quality" in rendered
        assert "all must be clean" in rendered
        assert "review_rtl_bugs_clean" not in rendered

    def test_reviews_parsed_back(self):
        body = "## Criteria\n\n- **reviews**: rtl_bugs, tb_quality -- all must be clean"
        parsed = parse_criteria_section(body)
        assert parsed["mandatory"]["review_rtl_bugs_clean"] is True
        assert parsed["mandatory"]["review_tb_quality_clean"] is True

    def test_reviews_unicode_dash(self):
        body = "## Criteria\n\n- **reviews**: rtl_bugs — all must be clean"
        parsed = parse_criteria_section(body)
        assert parsed["mandatory"]["review_rtl_bugs_clean"] is True

    def test_review_false_not_grouped(self):
        """review_*_clean: false should render individually, not be grouped."""
        criteria = {
            "mandatory": {
                "review_rtl_bugs_clean": True,
                "review_tb_quality_clean": False,
            }
        }
        rendered = render_criteria_section(criteria)
        assert "- **reviews**: rtl_bugs -- all must be clean" in rendered
        assert "- **review_tb_quality_clean**: false" in rendered


# ---------------------------------------------------------------------------
# Individual criterion types
# ---------------------------------------------------------------------------


class TestIndividualCriteria:
    def test_sim_entries_backtick_wrapped(self):
        criteria = {"mandatory": {"sim_pass": ["verif/tb.sv @ default @ none -> pass"]}}
        rendered = render_criteria_section(criteria)
        assert "- **sim_pass**: `verif/tb.sv @ default @ none -> pass`" in rendered

    def test_lint_configs_backtick_wrapped(self):
        criteria = {"mandatory": {"lint_clean": ["default", "strict"]}}
        rendered = render_criteria_section(criteria)
        assert "- **lint_clean**: `default`, `strict`" in rendered

    def test_synthesis_dict_rendered(self):
        criteria = {
            "mandatory": {
                "synthesis_ok": {
                    "cell_count_reduce_at_least": 20,
                    "configs": ["default", "lite"],
                },
            }
        }
        rendered = render_criteria_section(criteria)
        assert "- **synthesis_ok**:" in rendered
        assert "cell_count_reduce_at_least: 20" in rendered
        assert "configs: default, lite" in rendered

    def test_boolean_true_no_value(self):
        criteria = {"mandatory": {"rtl_plan_done": True}}
        rendered = render_criteria_section(criteria)
        assert "- **rtl_plan_done**" in rendered
        assert "- **rtl_plan_done**:" not in rendered

    def test_boolean_false_with_value(self):
        criteria = {"mandatory": {"rtl_plan_done": False}}
        rendered = render_criteria_section(criteria)
        assert "- **rtl_plan_done**: false" in rendered

    def test_string_value_as_is(self):
        criteria = {"mandatory": {"mutation_score": "8/10"}}
        rendered = render_criteria_section(criteria)
        assert "- **mutation_score**: 8/10" in rendered


# ---------------------------------------------------------------------------
# Body manipulation
# ---------------------------------------------------------------------------


class TestBodyManipulation:
    def test_inject_prepends_section(self):
        criteria = {"mandatory": {"sim_pass": ["verif/tb.sv @ default @ none -> pass"]}}
        body = "## Description\n\nSome text."
        result = inject_criteria_into_body(criteria, body)
        assert result.index("## Criteria") < result.index("## Description")

    def test_inject_idempotent(self):
        criteria = {"mandatory": {"sim_pass": ["verif/tb.sv @ default @ none -> pass"]}}
        body = "## Description\n\nSome text."
        once = inject_criteria_into_body(criteria, body)
        twice = inject_criteria_into_body(criteria, once)
        assert once == twice

    def test_strip_removes_criteria(self):
        body = "## Criteria\n\n- **sim_pass**: `tb.sv`\n\n## Description\n\nText."
        stripped = strip_criteria_from_body(body)
        assert "## Criteria" not in stripped
        assert "sim_pass" not in stripped
        assert "## Description" in stripped
        assert "Text." in stripped

    def test_strip_removes_with_subheadings(self):
        body = "## Criteria\n\n### Mandatory\n- **sim_pass**: `tb.sv`\n\n### Optional\n- **mutation_score**: 8/10\n\n## Description\nText."
        stripped = strip_criteria_from_body(body)
        assert "## Criteria" not in stripped
        assert "### Mandatory" not in stripped
        assert "### Optional" not in stripped
        assert "## Description" in stripped

    def test_strip_no_criteria_noop(self):
        body = "## Description\n\nText."
        assert strip_criteria_from_body(body) == body

    def test_inject_empty_body(self):
        criteria = {"mandatory": {"sim_pass": ["verif/tb.sv @ default @ none -> pass"]}}
        result = inject_criteria_into_body(criteria, "")
        assert "## Criteria" in result
        assert "sim_pass" in result


# ---------------------------------------------------------------------------
# Backwards compatibility: old YAML format via frontmatter
# ---------------------------------------------------------------------------


class TestBackwardsCompat:
    def test_old_yaml_format_still_parses(self):
        """Ticket with criteria in YAML frontmatter should still work."""
        text = (
            "---\n"
            "summary: test\n"
            "type: feature\n"
            "branch: main\n"
            "scope:\n"
            "  - rtl/test.sv\n"
            "criteria:\n"
            "  mandatory:\n"
            "    sim_pass:\n"
            "      - verif/tb.sv @ default @ none -> pass\n"
            "    coverage_toggle: 90\n"
            "---\n"
            "## Description\n"
            "Hello."
        )
        fields, _body = parse_frontmatter(text)
        assert "criteria" in fields
        assert "mandatory" in fields["criteria"]
        assert fields["criteria"]["mandatory"]["coverage_toggle"] == 90

    def test_new_body_format_parses(self):
        """Ticket with criteria in body section should populate fields."""
        text = (
            "---\n"
            "summary: test\n"
            "type: feature\n"
            "branch: main\n"
            "scope:\n"
            "  - rtl/test.sv\n"
            "---\n"
            "\n"
            "## Criteria\n"
            "\n"
            "- **sim_pass**: `verif/tb.sv @ default @ none -> pass`\n"
            "- **coverage**: toggle >= 90\n"
            "\n"
            "## Description\n"
            "Hello."
        )
        fields, _body = parse_frontmatter(text)
        assert "criteria" in fields
        assert fields["criteria"]["mandatory"]["coverage_toggle"] == 90
        assert fields["criteria"]["mandatory"]["sim_pass"] == [
            "verif/tb.sv @ default @ none -> pass"
        ]

    def test_format_moves_criteria_to_body(self):
        """format_frontmatter should put criteria in body, not YAML."""
        fields = {
            "summary": "test",
            "type": "feature",
            "branch": "main",
            "scope": ["rtl/test.sv"],
            "criteria": {"mandatory": {"sim_pass": ["verif/tb.sv @ default @ none -> pass"]}},
        }
        result = format_frontmatter(fields, "## Description\nHello.")
        # Criteria should NOT be in the YAML frontmatter
        fm_section = result.split("---")[1]
        assert "criteria:" not in fm_section
        # Criteria SHOULD be in the body
        assert "## Criteria" in result
        assert "sim_pass" in result

    def test_auto_migration_round_trip(self):
        """Old YAML ticket → format → re-parse should yield same criteria dict."""
        original_criteria = {
            "mandatory": {
                "sim_pass": ["verif/tb.sv @ default @ none -> pass"],
                "elab_pass": ["verif/tb.sv @ default @ none -> pass"],
                "review_rtl_bugs_clean": True,
                "review_tb_quality_clean": True,
                "coverage_toggle": 90,
                "coverage_fsm": 100,
            }
        }
        fields = {
            "summary": "test",
            "type": "feature",
            "branch": "main",
            "scope": ["rtl/test.sv"],
            "criteria": original_criteria,
        }
        text = format_frontmatter(fields, "## Description\nHello.")
        re_fields, _ = parse_frontmatter(text)
        assert re_fields["criteria"] == original_criteria


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_mandatory(self):
        criteria = {"mandatory": {}}
        rendered = render_criteria_section(criteria)
        assert "## Criteria" in rendered

    def test_empty_optional(self):
        criteria = {"mandatory": {"sim_pass": ["tb.sv"]}, "optional": {}}
        rendered = render_criteria_section(criteria)
        assert "### Optional" not in rendered

    def test_no_criteria_in_body_returns_none(self):
        assert parse_criteria_section("## Description\nHello.") is None

    def test_criteria_at_end_of_body(self):
        body = "## Criteria\n\n- **sim_pass**: `tb.sv`"
        parsed = parse_criteria_section(body)
        assert parsed is not None
        assert parsed["mandatory"]["sim_pass"] == ["tb.sv"]

    def test_criteria_section_between_headings(self):
        body = (
            "## Setup\nStuff.\n\n## Criteria\n\n- **sim_pass**: `tb.sv`\n\n## Description\nMore."
        )
        parsed = parse_criteria_section(body)
        assert parsed["mandatory"]["sim_pass"] == ["tb.sv"]

    def test_optional_only(self):
        criteria = {"optional": {"mutation_score": "8/10"}}
        rendered = render_criteria_section(criteria)
        # No mandatory items, but still renders
        parsed = parse_criteria_section(rendered)
        assert parsed is not None
        # Mandatory is empty, optional has the item
        assert "optional" in parsed
        assert parsed["optional"]["mutation_score"] == "8/10"
