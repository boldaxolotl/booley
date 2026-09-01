"""Target-specific mutation and coverage criterion contracts."""

import json

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import (
    CriteriaTemplate,
    find_retired_criteria,
    load_base_criteria,
)
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


def test_coverage_campaign_expands_shared_policy_per_target() -> None:
    criteria = {
        "optional": {
            "coverage": [
                {
                    "targets": ["sim_small", "sim_large"],
                    "metrics": {
                        "line": {"min_pct": 90},
                        "branch": {"min_pct": 85.5},
                    },
                    "tests": ["reset", "smoke"],
                }
            ]
        },
        "mandatory": {"sim_pass": ["sim_small", "sim_large"]},
    }

    template = CriteriaTemplate.from_yaml(criteria)

    assert template.expand([]) == {
        "coverage_sim_small": False,
        "coverage_sim_large": False,
        "sim_pass_sim_small": True,
        "sim_pass_sim_large": True,
    }
    assert template.expand_params([])["coverage_sim_small"] == {
        "target": "sim_small",
        "metrics": {
            "line": {"min_pct": 90},
            "branch": {"min_pct": 85.5},
        },
        "tests": ["reset", "smoke"],
    }
    assert template.expand_params([])["coverage_sim_large"] == {
        "target": "sim_large",
        "metrics": {
            "line": {"min_pct": 90},
            "branch": {"min_pct": 85.5},
        },
        "tests": ["reset", "smoke"],
    }
    assert validate_criteria_section(criteria) == []


def test_coverage_campaign_rejects_duplicate_target_ownership() -> None:
    criteria = {
        "mandatory": {
            "coverage": [
                {
                    "targets": ["sim_core"],
                    "metrics": {"line": {"min_pct": 90}},
                    "tests": "all",
                }
            ]
        },
        "optional": {
            "coverage": [
                {
                    "targets": ["sim_core"],
                    "metrics": {"branch": {"min_pct": 80}},
                    "tests": ["smoke"],
                }
            ]
        },
    }

    with pytest.raises(ValueError, match="Target 'sim_core' occurs in more than one coverage"):
        CriteriaTemplate.from_yaml(criteria)


@pytest.mark.parametrize(
    "legacy_name",
    [
        "coverage_toggle",
        "coverage_fsm",
        "coverage_value",
        "coverage_branch",
        "coverage_expression",
        "coverage_mean",
    ],
)
def test_legacy_coverage_criteria_are_hard_rejected(legacy_name: str) -> None:
    assert find_retired_criteria([legacy_name]) == [
        (
            legacy_name,
            "replace it with 'coverage: [{targets: [...], metrics: {...}, tests: all}]'",
        )
    ]


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"metrics": {"line": {"min_pct": 90}}, "tests": "all"}, "targets is required"),
        (
            {"targets": [], "metrics": {"line": {"min_pct": 90}}, "tests": "all"},
            "targets must be a non-empty",
        ),
        (
            {
                "targets": ["sim", "sim"],
                "metrics": {"line": {"min_pct": 90}},
                "tests": "all",
            },
            "targets must not contain duplicates",
        ),
        (
            {"targets": ["sim"], "metrics": {}, "tests": "all"},
            "metrics must be a non-empty",
        ),
        (
            {"targets": ["sim"], "metrics": {"fsm": {"min_pct": 90}}, "tests": "all"},
            "unknown metrics",
        ),
        (
            {
                "targets": ["sim"],
                "metrics": {"line": {"minimum": 90}},
                "tests": "all",
            },
            "exactly 'min_pct'",
        ),
        (
            {"targets": ["sim"], "metrics": {"line": {"min_pct": True}}, "tests": "all"},
            "must be numeric",
        ),
        (
            {"targets": ["sim"], "metrics": {"line": {"min_pct": 0}}, "tests": "all"},
            r"must be in \(0, 100\]",
        ),
        (
            {"targets": ["sim"], "metrics": {"line": {"min_pct": 90}}},
            "tests is required",
        ),
        (
            {
                "targets": ["sim"],
                "metrics": {"line": {"min_pct": 90}},
                "tests": [],
            },
            "tests must be 'all' or a non-empty",
        ),
        (
            {
                "targets": ["sim"],
                "metrics": {"line": {"min_pct": 90}},
                "tests": ["smoke", "smoke"],
            },
            "tests must not contain duplicates",
        ),
        (
            {
                "targets": ["sim"],
                "metrics": {"line": {"min_pct": 90}},
                "tests": "all",
                "scope": ["rtl"],
            },
            "unknown fields",
        ),
    ],
)
def test_coverage_campaign_rejects_invalid_authoring(record: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CriteriaTemplate.from_yaml({"mandatory": {"coverage": [record]}})


def test_base_catalog_contains_only_the_new_coverage_family() -> None:
    definitions = {definition.name: definition for definition in load_base_criteria()}

    assert definitions["coverage"].per_target is True
    assert definitions["coverage"].hidden is True
    assert (
        not {
            "coverage_toggle",
            "coverage_fsm",
            "coverage_value",
            "coverage_branch",
            "coverage_expression",
            "coverage_mean",
        }
        & definitions.keys()
    )


def test_coverage_authoring_validation_runs_at_preflight_seam() -> None:
    criteria = {
        "mandatory": {
            "coverage": [
                {
                    "targets": [],
                    "metrics": {"line": {"min_pct": 90}},
                    "tests": "all",
                }
            ]
        }
    }

    assert validate_criteria_section(criteria) == [
        "criteria: coverage.targets must be a non-empty list of names"
    ]


def test_coverage_authoring_requires_a_list_even_for_one_record() -> None:
    record = {
        "targets": ["sim"],
        "metrics": {"line": {"min_pct": 90}},
        "tests": "all",
    }

    with pytest.raises(ValueError, match="coverage must be a list of authoring records"):
        CriteriaTemplate.from_yaml({"mandatory": {"coverage": record}})


def test_coverage_authoring_rejects_empty_record_list() -> None:
    with pytest.raises(ValueError, match="coverage must contain at least one authoring record"):
        CriteriaTemplate.from_yaml({"mandatory": {"coverage": []}})


def test_legacy_bare_campaign_is_rejected() -> None:
    errors = validate_criteria_section({"mandatory": {"mutation_score": "8/10"}})

    assert any("per-target criterion" in error for error in errors)


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
        CriteriaTemplate.from_yaml({"mandatory": {"mutation_score": [campaign]}})


def test_legacy_dut_info_is_dropped_on_state_save(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"slug": "old", "dut_info": {"dut_top_module": "dut"}}),
        encoding="utf-8",
    )

    state = DevelopmentState.load(state_path)
    state.save()

    assert "dut_info" not in json.loads(state_path.read_text(encoding="utf-8"))
