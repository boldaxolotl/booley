"""Tests for the criteria field validation across all layers.

Covers:
  - Frontmatter inline dict parse/format round-trip
  - Validation rules (type checks, cross-validation with ticket type)
  - Checkpoint baseline_status serialization
  - TicketContext criteria-derived properties
"""

from __future__ import annotations

from booley.ticket_board.frontmatter import format_frontmatter, parse_frontmatter
from booley.ticket_board.validation import validate_ticket_fields

# ---------------------------------------------------------------------------
# 1. Frontmatter inline dict parse/format
# ---------------------------------------------------------------------------


class TestFrontmatterDict:
    def test_empty_dict_parse(self):
        text = "---\nbaseline_tests: {}\n---\nBody"
        fields, body = parse_frontmatter(text)
        assert fields["baseline_tests"] == {}
        assert body == "Body"

    def test_single_entry_parse(self):
        text = "---\nbaseline_tests: {my_adder_tb: pass}\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["baseline_tests"] == {"my_adder_tb": "pass"}

    def test_multi_entry_parse(self):
        text = "---\nbaseline_tests: {my_adder_tb: pass, my_module_tb: fail, my_mul_tb: no_test}\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["baseline_tests"] == {
            "my_adder_tb": "pass",
            "my_module_tb": "fail",
            "my_mul_tb": "no_test",
        }

    def test_empty_dict_format(self):
        fields = {"baseline_tests": {}}
        text = format_frontmatter(fields, "")
        assert "baseline_tests: {}" in text

    def test_dict_format_with_entries(self):
        fields = {"baseline_tests": {"my_adder_tb": "pass", "my_module_tb": "fail"}}
        text = format_frontmatter(fields, "")
        assert "baseline_tests: {my_adder_tb: pass, my_module_tb: fail}" in text

    def test_dict_round_trip(self):
        original = {"my_adder_tb": "pass", "my_module_tb": "fail", "my_mul_tb": "no_test"}
        fields = {"baseline_tests": original, "summary": "test"}
        text = format_frontmatter(fields, "## Body")
        parsed, body = parse_frontmatter(text)
        assert parsed["baseline_tests"] == original
        assert body == "## Body"

    def test_empty_dict_round_trip(self):
        fields = {"baseline_tests": {}, "summary": "test"}
        text = format_frontmatter(fields, "")
        parsed, _ = parse_frontmatter(text)
        assert parsed["baseline_tests"] == {}

    def test_dict_with_boolean_values(self):
        text = "---\nmy_dict: {a: true, b: false}\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["my_dict"] == {"a": True, "b": False}

    def test_dict_with_integer_values(self):
        text = "---\nmy_dict: {x: 42, y: 0}\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["my_dict"] == {"x": 42, "y": 0}


# ---------------------------------------------------------------------------
# 2. Criteria validation rules
# ---------------------------------------------------------------------------


class TestCriteriaValidation:
    def _make_fields(self, ticket_type="feature", criteria=None):
        """Build minimal valid ticket fields with optional criteria dict."""
        if criteria is None:
            criteria = {
                "mandatory": {
                    "sim_pass": ["tb/foo_tb.sv @ config_a @ all @ pass -> pass"],
                },
            }
        fields = {
            "summary": "test ticket",
            "type": ticket_type,
            "branch": "master",
            "scope": ["rtl/foo.sv"],
            "criteria": criteria,
        }
        return fields

    def test_valid_feature_all_pass(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ pass -> pass",
                    "tb/tb2.sv @ config_a @ all @ pass -> pass",
                ],
            },
        }
        fields = self._make_fields("feature", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert not any("criteria" in e.lower() and "invalid" in e.lower() for e in errors)

    def test_valid_bugfix_has_fail(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ fail -> pass",
                    "tb/tb2.sv @ config_a @ all @ pass -> pass",
                ],
            },
        }
        fields = self._make_fields("bugfix", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert not any("Bugfix" in e for e in errors)

    def test_valid_refactor_all_pass(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ pass -> pass",
                    "tb/tb2.sv @ config_a @ all @ pass -> pass",
                ],
            },
        }
        fields = self._make_fields("refactor", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert not any("Refactor" in e for e in errors)

    def test_refactor_must_be_all_pass(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ pass -> pass",
                    "tb/tb2.sv @ config_a @ all @ fail -> pass",
                ],
            },
        }
        fields = self._make_fields("refactor", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert any("Refactor" in e or "pass -> pass" in e for e in errors)

    def test_bugfix_must_have_fail(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ pass -> pass",
                    "tb/tb2.sv @ config_a @ all @ pass -> pass",
                ],
            },
        }
        fields = self._make_fields("bugfix", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert any("Bugfix" in e or "fail -> pass" in e for e in errors)

    # -- none -> pass tests --------------------------------------------------

    def test_valid_feature_none_to_pass(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ none -> pass",
                ],
            },
        }
        fields = self._make_fields("feature", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert not any("criteria" in e.lower() and "invalid" in e.lower() for e in errors)

    def test_valid_bugfix_fail_plus_none(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ fail -> pass",
                    "tb/tb2.sv @ config_a @ all @ none -> pass",
                ],
            },
        }
        fields = self._make_fields("bugfix", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert not any("Bugfix" in e for e in errors)

    def test_bugfix_only_none_to_pass_rejected(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ none -> pass",
                    "tb/tb2.sv @ config_a @ all @ none -> pass",
                ],
            },
        }
        fields = self._make_fields("bugfix", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert any("Bugfix" in e or "fail -> pass" in e for e in errors)

    def test_valid_refactor_none_to_pass(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ none -> pass",
                ],
            },
        }
        fields = self._make_fields("refactor", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert not any("Refactor" in e for e in errors)

    def test_valid_refactor_mixed_pass_and_none(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ pass -> pass",
                    "tb/tb2.sv @ config_a @ all @ none -> pass",
                ],
            },
        }
        fields = self._make_fields("refactor", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert not any("Refactor" in e for e in errors)

    def test_refactor_fail_to_pass_still_rejected(self):
        criteria = {
            "mandatory": {
                "sim_pass": [
                    "tb/tb1.sv @ config_a @ all @ fail -> pass",
                ],
            },
        }
        fields = self._make_fields("refactor", criteria)
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert any("Refactor" in e for e in errors)

    def test_criteria_must_be_dict(self):
        fields = self._make_fields("feature", criteria="not a dict")
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert any("must be a dict" in e for e in errors)

    def test_empty_mandatory_is_error(self):
        """Empty mandatory section triggers validation error."""
        for t in ("feature", "bugfix", "refactor"):
            fields = self._make_fields(t, {"mandatory": {}})
            errors = validate_ticket_fields(fields, "## Description\nTest.")
            assert any("at least one criterion" in e for e in errors)

    def test_valid_default(self):
        """Default criteria from _make_fields is valid."""
        fields = self._make_fields("feature")
        errors = validate_ticket_fields(fields, "## Description\nTest.")
        assert not any("criteria" in e.lower() and "invalid" in e.lower() for e in errors)
