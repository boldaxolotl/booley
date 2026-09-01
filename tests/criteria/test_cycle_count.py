from __future__ import annotations

import math

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import CriteriaTemplate
from booley.criteria.thresholds import evaluate_cycle_threshold, is_relative_threshold


def _entry(**thresholds):
    return {"target": "sim_core", "test": "coremark", **thresholds}


def test_cycle_count_mapping_expands_with_original_binding() -> None:
    template = CriteriaTemplate.from_yaml(
        {"mandatory": {"cycle_count": [_entry(cycle_count_max=100_000)]}}
    )

    expanded = template.expand([])
    assert len(expanded) == 1
    key = next(iter(expanded))
    assert key.startswith("cycle_count_")
    assert template.expand_params([])[key] == {
        "target": "sim_core",
        "test": "coremark",
        "cycle_count_max": 100_000,
    }


def test_cycle_count_key_encoding_cannot_collide_on_underscores() -> None:
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [
                    {"target": "a_b", "test": "c", "cycle_count_max": 1},
                    {"target": "a", "test": "b_c", "cycle_count_max": 1},
                ]
            }
        }
    )
    assert len(template.expand([])) == 2


def test_cycle_count_duplicate_binding_is_rejected_across_sections() -> None:
    with pytest.raises(ValueError, match=r"duplicate.*sim_core.*coremark"):
        CriteriaTemplate.from_yaml(
            {
                "mandatory": {"cycle_count": [_entry(cycle_count_max=10)]},
                "optional": {"cycle_count": [_entry(cycle_count_min=1)]},
            }
        )


@pytest.mark.parametrize("missing", ["target", "test"])
def test_cycle_count_requires_non_empty_binding_fields(missing: str) -> None:
    entry = _entry(cycle_count_max=10)
    entry.pop(missing)
    with pytest.raises(ValueError, match=missing):
        CriteriaTemplate.from_yaml({"mandatory": {"cycle_count": [entry]}})


@pytest.mark.parametrize(
    ("param", "value"),
    [
        ("cycle_count_max", -1),
        ("cycle_count_min", True),
        ("cycle_count_increase_at_most_cycles", 1.5),
        ("cycle_count_reduce_at_least", "100.1%"),
        ("cycle_count_increase_at_least", math.inf),
        ("cycle_count_increase_at_most", math.nan),
    ],
)
def test_cycle_count_rejects_invalid_numeric_values(param: str, value: object) -> None:
    with pytest.raises(ValueError, match=param):
        CriteriaTemplate.from_yaml({"mandatory": {"cycle_count": [_entry(**{param: value})]}})


def test_cycle_count_accepts_zero_and_fractional_percentages() -> None:
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [
                    _entry(
                        cycle_count_max=0,
                        cycle_count_increase_at_most="0%",
                        cycle_count_reduce_at_least="99.9%",
                    )
                ]
            }
        }
    )
    params = next(iter(template.expand_params([]).values()))
    assert params["cycle_count_increase_at_most"] == 0
    assert params["cycle_count_reduce_at_least"] == 99.9


@pytest.mark.parametrize("value", [8, 8.5, "8", "8 percent"])
def test_cycle_count_percentage_requires_percent_suffix(value: object) -> None:
    with pytest.raises(ValueError, match="must end in '%'"):
        CriteriaTemplate.from_yaml(
            {"mandatory": {"cycle_count": [_entry(cycle_count_reduce_at_least=value)]}}
        )


def test_cycle_count_rejects_static_same_unit_contradictions() -> None:
    with pytest.raises(ValueError, match="contradictory"):
        CriteriaTemplate.from_yaml(
            {"mandatory": {"cycle_count": [_entry(cycle_count_min=11, cycle_count_max=10)]}}
        )


@pytest.mark.parametrize(
    ("param", "current", "baseline", "passed"),
    [
        ("cycle_count_max", 10, None, True),
        ("cycle_count_min", 10, None, True),
        ("cycle_count_increase_at_least", 110, 100, True),
        ("cycle_count_increase_at_most", 90, 100, True),
        ("cycle_count_reduce_at_least", 90, 100, True),
        ("cycle_count_reduce_at_most", 120, 100, True),
        ("cycle_count_increase_at_least_cycles", 110, 100, True),
        ("cycle_count_increase_at_most_cycles", 90, 100, True),
        ("cycle_count_reduce_at_least_cycles", 90, 100, True),
        ("cycle_count_reduce_at_most_cycles", 120, 100, True),
    ],
)
def test_cycle_thresholds_use_inclusive_signed_bounds(
    param: str, current: int, baseline: int | None, passed: bool
) -> None:
    result = evaluate_cycle_threshold(param, 10, current=current, baseline=baseline)
    assert result["pass"] is passed


def test_percentage_threshold_fails_closed_on_zero_baseline() -> None:
    result = evaluate_cycle_threshold("cycle_count_increase_at_most", 0, current=0, baseline=0)
    assert result["pass"] is False
    assert result["skipped"] is False
    assert "zero baseline" in result["reason"]


def test_absolute_delta_remains_defined_on_zero_baseline() -> None:
    result = evaluate_cycle_threshold(
        "cycle_count_increase_at_most_cycles", 0, current=0, baseline=0
    )
    assert result["pass"] is True
    assert result["delta_cycles"] == 0


def test_relative_registry_covers_every_relative_direction_and_unit() -> None:
    assert is_relative_threshold("cell_count_increase_at_most")
    assert is_relative_threshold("cycle_count_reduce_at_most")
    assert is_relative_threshold("cycle_count_increase_at_least_cycles")
    assert not is_relative_threshold("cycle_count_max")


def test_synthesis_and_fpga_thresholds_accept_zero() -> None:
    CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "synthesis_ok": {"targets": ["synth"], "cell_count_max": 0},
                "fpga_impl_ok": {"targets": ["fpga"], "lut_count_max": 0},
            }
        }
    )


def test_development_state_ands_all_cycle_thresholds() -> None:
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [_entry(cycle_count_max=95, cycle_count_reduce_at_least="5%")]
            }
        }
    )
    key = next(iter(template.expand([])))
    state = DevelopmentState()
    state.init_criteria(template.expand([]), criterion_params=template.expand_params([]))

    state.set_criterion(
        key,
        True,
        detail={"cycles": 95, "baseline_cycles": 100},
    )

    assert state.criteria[key].met is True
    assert [check["pass"] for check in state.criteria[key].detail["checks"]] == [True, True]


def test_development_state_cycle_evidence_fails_closed_and_clears_latch() -> None:
    template = CriteriaTemplate.from_yaml(
        {"mandatory": {"cycle_count": [_entry(cycle_count_max=10)]}}
    )
    key = next(iter(template.expand([])))
    state = DevelopmentState()
    state.init_criteria(template.expand([]), criterion_params=template.expand_params([]))
    state.set_criterion(key, True, detail={"cycles": 10})
    assert state.criteria[key].ever_met is True

    state.set_criterion(key, True, detail={"cycles": None})

    assert state.criteria[key].met is False
    assert state.criteria[key].ever_met is False
    assert "unavailable" in state.criteria[key].detail["checks"][0]["reason"]
