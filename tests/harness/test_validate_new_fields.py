"""Quick smoke test for criteria and scope validation."""

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from booley.ticket_board.validation import validate_ticket_fields


def test_criteria_with_sim_entries():
    """Criteria with structured sim entries is valid."""
    fields = {
        "summary": "test",
        "type": "bugfix",
        "branch": "devel_branch",
        "scope": ["tb/tb_top.sv"],
        "criteria": {
            "mandatory": {
                "sim_pass": [
                    "tb/tb_top.sv @ config_b/v02 @ all @ fail -> pass",
                ],
            },
        },
        "priority": "low",
    }
    body = "## Description\ntest"
    errors = validate_ticket_fields(fields, body)
    assert errors == [], f"Expected no errors, got: {errors}"


def test_empty_scope_rejected():
    """Empty scope list is rejected by validation."""
    fields = {
        "summary": "test",
        "type": "bugfix",
        "branch": "main",
        "scope": [],
        "criteria": {
            "mandatory": {
                "sim_pass": ["tb/tb.sv @ config_a @ all @ fail -> pass"],
            },
        },
    }
    errors = validate_ticket_fields(fields, "## Description\ntest")
    assert any("scope is empty" in e for e in errors)


def test_criteria_must_have_mandatory():
    """Criteria with empty mandatory section is rejected."""
    fields = {
        "summary": "test",
        "type": "bugfix",
        "branch": "main",
        "scope": ["rtl/foo.sv"],
        "criteria": {"mandatory": {}},
    }
    errors = validate_ticket_fields(fields, "## Description\ntest")
    assert any("at least one criterion" in e for e in errors)


def test_unknown_synthesis_ok_param_rejected_at_enqueue():
    """An unknown synthesis_ok param (e.g. `configs`, whose real scoping key is
    `targets`) must be caught at enqueue validation, not crash the harness mid-run
    with a CRITICAL traceback from CriteriaTemplate.from_yaml()."""
    fields = {
        "summary": "test",
        "type": "feature",
        "branch": "main",
        "scope": ["rtl/foo.sv"],
        "criteria": {
            "mandatory": {
                "synthesis_ok": {"configs": ["synth_div"], "area_increase_at_most": "100%"},
            },
        },
    }
    errors = validate_ticket_fields(fields, "## Description\ntest")
    assert any("Unknown synthesis_ok params" in e and "configs" in e for e in errors)


def test_unknown_fpga_impl_ok_param_rejected_at_enqueue():
    """Same gap for fpga_impl_ok: `configs` is not a param (scoping key is `targets`)."""
    fields = {
        "summary": "test",
        "type": "feature",
        "branch": "main",
        "scope": ["rtl/foo.sv"],
        "criteria": {
            "mandatory": {
                "fpga_impl_ok": {"configs": ["lite"], "lut_count_max": 100000},
            },
        },
    }
    errors = validate_ticket_fields(fields, "## Description\ntest")
    assert any("Unknown fpga_impl_ok params" in e and "configs" in e for e in errors)


def test_synthesis_ok_targets_scoping_accepted():
    """`targets:` is the supported per-config scoping key for synthesis_ok."""
    fields = {
        "summary": "test",
        "type": "feature",
        "branch": "main",
        "scope": ["rtl/foo.sv"],
        "criteria": {
            "mandatory": {
                "synthesis_ok": {"targets": ["synth_div"], "area_increase_at_most": "100%"},
            },
        },
    }
    errors = validate_ticket_fields(fields, "## Description\ntest")
    assert errors == [], f"Expected no errors, got: {errors}"


def test_percentage_threshold_requires_explicit_symbol_at_enqueue():
    fields = {
        "summary": "test",
        "type": "feature",
        "branch": "main",
        "scope": ["rtl/foo.sv"],
        "criteria": {
            "mandatory": {
                "synthesis_ok": {
                    "targets": ["synth_div"],
                    "area_increase_at_most": 8,
                },
            },
        },
    }

    errors = validate_ticket_fields(fields, "## Description\ntest")

    assert any("must end in '%'" in error for error in errors)


if __name__ == "__main__":
    test_criteria_with_sim_entries()
    test_empty_scope_rejected()
    test_criteria_must_have_mandatory()
    print("All tests passed!")
