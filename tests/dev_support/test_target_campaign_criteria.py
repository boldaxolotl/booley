"""Target-specific mutation and coverage criterion contracts."""

import json

import pytest

from booley.dev_support.criteria import CriteriaTemplate
from booley.dev_support.development_state import DevelopmentState
from booley.ticket_board.validation import validate_criteria_section


def test_mutation_campaign_expands_target_and_parameters() -> None:
    criteria = {
        "mandatory": {
            "mutation_score": [
                {
                    "target": "sim_unit",
                    "scope": ["rtl/alu.sv"],
                    "min_detected": 8,
                    "total": 10,
                }
            ]
        }
    }

    template = CriteriaTemplate.from_yaml(criteria)

    assert template.expand([]) == {"mutation_score_sim_unit": True}
    assert template.expand_params([])["mutation_score_sim_unit"] == {
        "scope": ["rtl/alu.sv"],
        "min_detected": 8,
        "total": 10,
    }
    assert validate_criteria_section(criteria) == []


def test_coverage_campaign_expands_target_and_threshold() -> None:
    criteria = {
        "optional": {
            "coverage_toggle": [
                {
                    "target": "sim_top",
                    "scope": ["rtl/top.sv"],
                    "min_pct": 95,
                }
            ]
        },
        "mandatory": {"sim_pass": ["sim_top"]},
    }

    template = CriteriaTemplate.from_yaml(criteria)

    assert template.expand([])["coverage_toggle_sim_top"] is False
    assert template.expand_params([])["coverage_toggle_sim_top"]["min_pct"] == 95
    assert validate_criteria_section(criteria) == []


def test_legacy_bare_campaign_is_rejected() -> None:
    errors = validate_criteria_section(
        {"mandatory": {"mutation_score": "8/10"}}
    )

    assert any("per-target criterion" in error for error in errors)


def test_campaign_requires_scope() -> None:
    errors = validate_criteria_section(
        {"mandatory": {"coverage_toggle": [{"target": "sim"}]}}
    )

    assert any("scope must be a non-empty list[str]" in error for error in errors)


@pytest.mark.parametrize(
    "params",
    [
        {"total": 2.5},
        {"total": 4, "min_detected": 5},
        {"auto": True, "total": 4},
    ],
)
def test_mutation_campaign_rejects_incoherent_counts(params) -> None:
    campaign = {"target": "sim", "scope": ["rtl/dut.sv"], **params}
    with pytest.raises(ValueError):
        CriteriaTemplate.from_yaml(
            {"mandatory": {"mutation_score": [campaign]}}
        )


@pytest.mark.parametrize("min_pct", [0, 101, True])
def test_coverage_campaign_rejects_invalid_threshold(min_pct) -> None:
    with pytest.raises(ValueError):
        CriteriaTemplate.from_yaml(
            {
                "mandatory": {
                    "coverage_toggle": [
                        {
                            "target": "sim",
                            "scope": ["rtl/dut.sv"],
                            "min_pct": min_pct,
                        }
                    ]
                }
            }
        )


def test_legacy_dut_info_is_dropped_on_state_save(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"slug": "old", "dut_info": {"dut_top_module": "dut"}}),
        encoding="utf-8",
    )

    state = DevelopmentState.load(state_path)
    state.save()

    assert "dut_info" not in json.loads(state_path.read_text(encoding="utf-8"))
