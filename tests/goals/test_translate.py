"""Goal arguments to Criteria, and the D11 stricter-Goal merge."""

from __future__ import annotations

from typing import Any

import pytest

from booley.criteria.templates import CriteriaTemplate, cycle_count_criterion_key
from booley.goals.model import GoalFamily, parse_goal_args
from booley.goals.translate import (
    GoalConflictError,
    GoalTranslationError,
    criterion_spec,
    stricter_threshold,
    translate_goals,
)


def translate(*raw: dict[str, Any]):
    return translate_goals(parse_goal_args(list(raw)))


# --- One Goal per family --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key", "params"),
    [
        ({"family": "lint", "target": "lint_uart"}, "lint_clean_lint_uart", {}),
        ({"family": "sim", "target": "sim_uart"}, "sim_pass_sim_uart", {}),
        ({"family": "elab", "target": "sim_uart"}, "elab_pass_sim_uart", {}),
        (
            {
                "family": "synth",
                "target": "synth_uart",
                "thresholds": {"area_increase_at_most": "5%"},
            },
            "synthesis_ok_synth_uart",
            {"area_increase_at_most": 5},
        ),
        (
            {
                "family": "fpga",
                "target": "fpga_new",
                "baseline": "fpga_old",
                "thresholds": {"lut_count_reduce_at_least": "10%"},
            },
            "fpga_impl_ok_fpga_new",
            {"lut_count_reduce_at_least": 10, "_baseline_target": "fpga_old"},
        ),
        (
            {
                "family": "cycle_count",
                "target": "sim_uart",
                "test": "smoke",
                "thresholds": {"cycle_count_max": 900},
            },
            cycle_count_criterion_key("sim_uart", "smoke"),
            {"target": "sim_uart", "test": "smoke", "cycle_count_max": 900},
        ),
        (
            {"family": "coverage", "target": "sim_uart", "metrics": {"line": 80}, "tests": "all"},
            "coverage_sim_uart",
            {"target": "sim_uart", "metrics": {"line": {"min_pct": 80}}, "tests": "all"},
        ),
        (
            {"family": "mutation", "target": "sim_uart", "min_detected": 8, "total": 10},
            "mutation_score_sim_uart",
            {"min_detected": 8, "total": 10},
        ),
        (
            {"family": "review", "review": "rtl_bugs", "verdict": "clean"},
            "review_rtl_bugs_clean",
            {},
        ),
        (
            {"family": "review", "review": "rtl_spec", "verdict": "done", "spec": "doc/uart.md"},
            "review_rtl_spec_done",
            {"spec": "doc/uart.md"},
        ),
    ],
)
def test_every_family_translates_to_its_criterion_key(
    raw: dict[str, Any], key: str, params: dict[str, Any]
) -> None:
    result = translate(raw)

    assert result.warnings == ()
    (goal,) = result.goals
    assert (goal.key, goal.params, goal.origins) == (key, params, ("ad-hoc",))
    assert criterion_spec(goal).expand([]) == [(key, True)]


@pytest.mark.parametrize(
    ("raw", "authoring"),
    [
        ({"family": "lint", "target": "t"}, {"lint_clean": ["t"]}),
        (
            {"family": "synth", "target": "t", "thresholds": {"cell_count_max": 3}},
            {"synthesis_ok": {"targets": ["t"], "cell_count_max": 3}},
        ),
        (
            {"family": "coverage", "target": "t", "metrics": {"line": 9}, "tests": ["a"]},
            {
                "coverage": [
                    {"targets": ["t"], "metrics": {"line": {"min_pct": 9}}, "tests": ["a"]}
                ]
            },
        ),
        (
            {"family": "review", "review": "tb_quality", "verdict": "clean"},
            {"review_tb_quality": None},
        ),
    ],
)
def test_criterion_specs_equal_the_ticket_authoring_ones(
    raw: dict[str, Any], authoring: dict[str, Any]
) -> None:
    (goal,) = translate(raw).goals
    (expected,) = CriteriaTemplate.from_yaml({"mandatory": authoring}).specs
    assert criterion_spec(goal) == expected


def test_goals_are_mandatory_criteria() -> None:
    result = translate({"family": "lint", "target": "a"}, {"family": "sim", "target": "a"})
    assert all(spec.mandatory for spec in result.criteria)
    assert [goal.family for goal in result.goals] == [GoalFamily.LINT, GoalFamily.SIM]


def test_a_candidate_target_that_does_not_exist_yet_is_accepted() -> None:
    # Translation never consults the Target catalog; entry warns later (Phase 2).
    (goal,) = translate({"family": "synth", "target": "synth_not_written_yet"}).goals
    assert goal.target == "synth_not_written_yet"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            {"family": "synth", "target": "t", "thresholds": {"clk.area_um2_max": 1}},
            "Unknown synthesis_ok params",
        ),
        (
            {
                "family": "synth",
                "target": "t",
                "thresholds": {"area_um2_max": 1, "area_kge_max": 1},
            },
            "mutually exclusive",
        ),
        ({"family": "fpga", "target": "new", "baseline": "old"}, "relative threshold"),
        ({"family": "cycle_count", "target": "t", "test": "x", "thresholds": {}}, "at least one"),
        (
            {"family": "coverage", "target": "t", "metrics": {"line": 120}, "tests": "all"},
            r"\(0, 100\]",
        ),
        ({"family": "mutation", "target": "t", "min_detected": 9, "total": 3}, "cannot exceed"),
        ({"family": "synth", "target": "t", "thresholds": {"area_increase_at_most": 5}}, "'%'"),
    ],
)
def test_criteria_rules_reject_bad_goals_and_name_the_argument(
    raw: dict[str, Any], message: str
) -> None:
    with pytest.raises(GoalTranslationError, match=message) as caught:
        translate(raw)
    assert "goals[0]" in str(caught.value)


# --- D11 merge table --------------------------------------------------------------


def _synth(origin: str, **thresholds: Any) -> dict[str, Any]:
    return {"family": "synth", "target": "synth_u", "thresholds": thresholds, "origin": origin}


def _cycle(origin: str, **thresholds: Any) -> dict[str, Any]:
    return {
        "family": "cycle_count",
        "target": "sim_u",
        "test": "t",
        "thresholds": thresholds,
        "origin": origin,
    }


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        # absolute upper bound: smaller wins
        (
            _synth("feature", area_um2_max=100),
            _synth("ad-hoc", area_um2_max=80),
            {"area_um2_max": 80},
        ),
        # absolute lower bound: larger wins
        (
            _synth("feature", fmax_mhz_min=200),
            _synth("ad-hoc", fmax_mhz_min=150),
            {"fmax_mhz_min": 200},
        ),
        # relative increase cap: smaller wins
        (
            _synth("feature", area_increase_at_most="5%"),
            _synth("ad-hoc", area_increase_at_most="2%"),
            {"area_increase_at_most": 2},
        ),
        # relative reduction floor: larger wins
        (
            _synth("feature", cell_count_reduce_at_least="3%"),
            _synth("ad-hoc", cell_count_reduce_at_least="10%"),
            {"cell_count_reduce_at_least": 10},
        ),
        # per-clock thresholds merge per clock
        (
            _synth("feature", **{"clk.fmax_mhz_min": 100}),
            _synth("ad-hoc", **{"clk.fmax_mhz_min": 120}),
            {"clk.fmax_mhz_min": 120},
        ),
        # a threshold only one Goal sets is kept
        (
            _synth("feature", area_um2_max=100),
            _synth("ad-hoc", fmax_mhz_min=150),
            {"area_um2_max": 100, "fmax_mhz_min": 150},
        ),
        # cycle count: every direction
        (
            _cycle("a", cycle_count_max=10),
            _cycle("b", cycle_count_max=12),
            {"cycle_count_max": 10},
        ),
        (
            _cycle("a", cycle_count_min=10),
            _cycle("b", cycle_count_min=12),
            {"cycle_count_min": 12},
        ),
        (
            _cycle("a", cycle_count_reduce_at_most="20%"),
            _cycle("b", cycle_count_reduce_at_most="5%"),
            {"cycle_count_reduce_at_most": 5},
        ),
        (
            _cycle("a", cycle_count_increase_at_least_cycles=3),
            _cycle("b", cycle_count_increase_at_least_cycles=7),
            {"cycle_count_increase_at_least_cycles": 7},
        ),
    ],
)
def test_same_key_merges_to_the_stricter_threshold(
    first: dict[str, Any], second: dict[str, Any], expected: dict[str, Any]
) -> None:
    for order in ((first, second), (second, first)):
        result = translate(*order)
        (goal,) = result.goals
        identity = {k: v for k, v in goal.params.items() if k in {"target", "test"}}
        assert goal.params == {**identity, **expected}
        assert set(goal.origins) == {first["origin"], second["origin"]}
        assert len(result.warnings) == 1
        assert goal.key in result.warnings[0]


def test_identical_goals_merge_with_a_warning() -> None:
    result = translate(
        {"family": "lint", "target": "a", "origin": "feature"},
        {"family": "lint", "target": "a", "origin": "refactor"},
        {"family": "sim", "target": "a"},
    )
    assert [goal.key for goal in result.goals] == ["lint_clean_a", "sim_pass_a"]
    assert result.goals[0].origins == ("feature", "refactor")
    assert result.warnings == ("Goal lint_clean_a from feature, refactor merged: identical Goals",)


def test_review_clean_wins_over_done_for_the_same_review() -> None:
    result = translate(
        {"family": "review", "review": "rtl_bugs", "verdict": "done", "origin": "feature"},
        {"family": "review", "review": "rtl_bugs", "verdict": "clean", "origin": "ad-hoc"},
        {"family": "review", "review": "tb_quality", "verdict": "done"},
    )
    assert [goal.key for goal in result.goals] == [
        "review_rtl_bugs_clean",
        "review_tb_quality_done",
    ]
    assert result.goals[0].origins == ("feature", "ad-hoc")
    assert "'clean' (from ad-hoc) wins over 'done' (from feature)" in result.warnings[0]


def test_coverage_floors_merge_per_metric() -> None:
    def coverage(origin: str, metrics: dict[str, int]) -> dict[str, Any]:
        return {
            "family": "coverage",
            "target": "s",
            "metrics": metrics,
            "tests": ["b", "a"],
            "origin": origin,
        }

    result = translate(
        coverage("x", {"line": 80, "branch": 50}), coverage("y", {"line": 90, "toggle": 10})
    )
    (goal,) = result.goals
    assert goal.params["metrics"] == {
        "line": {"min_pct": 90},
        "branch": {"min_pct": 50},
        "toggle": {"min_pct": 10},
    }
    assert "line 80 (from x) and 90 (from y) -> stricter 90" in result.warnings[0]


@pytest.mark.parametrize(
    ("first", "second", "message"),
    [
        (
            {
                "family": "synth",
                "target": "c",
                "baseline": "b1",
                "thresholds": {"area_increase_at_most": "1%"},
            },
            {
                "family": "synth",
                "target": "c",
                "baseline": "b2",
                "thresholds": {"area_increase_at_most": "1%"},
            },
            "different baseline Targets",
        ),
        (
            {"family": "coverage", "target": "s", "metrics": {"line": 1}, "tests": "all"},
            {"family": "coverage", "target": "s", "metrics": {"line": 1}, "tests": ["a"]},
            "different coverage test selections",
        ),
        (
            {"family": "mutation", "target": "s", "min_detected": 8, "total": 10},
            {"family": "mutation", "target": "s", "min_detected": 8, "total": 12},
            "different mutation scope, total, or auto",
        ),
        (
            {"family": "review", "review": "rtl_spec", "verdict": "clean", "spec": "a.md"},
            {"family": "review", "review": "rtl_spec", "verdict": "clean", "spec": "b.md"},
            "different spec files",
        ),
        (_synth("x", area_um2_max=1), _synth("y", area_kge_max=1), "mutually exclusive"),
        (
            _cycle("x", cycle_count_min=10),
            _cycle("y", cycle_count_max=5),
            "contradictory absolute min/max bounds",
        ),
    ],
)
def test_goals_without_a_stricter_one_conflict(
    first: dict[str, Any], second: dict[str, Any], message: str
) -> None:
    with pytest.raises(GoalConflictError, match=message):
        translate(first, second)


def test_stricter_threshold_refuses_a_name_without_a_direction() -> None:
    with pytest.raises(GoalConflictError, match="no defined stricter value"):
        stricter_threshold("scope", 1, 2)


def test_disjoint_thresholds_are_listed_per_origin_not_called_identical() -> None:
    result = translate(_synth("feature", area_um2_max=100), _synth("ad-hoc", fmax_mhz_min=150))
    (warning,) = result.warnings
    assert "identical" not in warning
    assert "area_um2_max 100 added from feature" in warning
    assert "fmax_mhz_min 150 added from ad-hoc" in warning


def test_a_goal_without_relative_thresholds_has_no_baseline_opinion() -> None:
    plain = {"family": "synth", "target": "t", "origin": "feature"}
    paired = {
        "family": "synth",
        "target": "t",
        "baseline": "b",
        "thresholds": {"area_reduce_at_least": "5%"},
        "origin": "ad-hoc",
    }
    for order in ((plain, paired), (paired, plain)):
        (goal,) = translate(*order).goals
        assert goal.params == {"area_reduce_at_least": 5, "_baseline_target": "b"}


def test_a_relative_threshold_without_baseline_compares_against_the_candidate() -> None:
    own = {"family": "synth", "target": "t", "thresholds": {"area_increase_at_most": "1%"}}
    paired = {
        "family": "synth",
        "target": "t",
        "baseline": "b",
        "thresholds": {"area_reduce_at_least": "5%"},
    }
    with pytest.raises(GoalConflictError, match="different baseline Targets"):
        translate(own, paired)


def test_mutation_min_detected_merges_upward_when_the_rest_agree() -> None:
    def mutation(origin: str, detected: int) -> dict[str, Any]:
        return {
            "family": "mutation",
            "target": "s",
            "scope": ["rtl/a.sv"],
            "min_detected": detected,
            "total": 10,
            "origin": origin,
        }

    result = translate(mutation("feature", 6), mutation("ad-hoc", 8))
    (goal,) = result.goals
    assert goal.params == {"scope": ["rtl/a.sv"], "min_detected": 8, "total": 10}
    assert "min_detected 6 (from feature) and 8 (from ad-hoc) -> stricter 8" in result.warnings[0]


_HOSTILE_THRESHOLDS: list[dict[str, Any]] = [
    {"targets": ["other"], "cell_count_max": 10},
    {"target": "other", "cycle_count_max": 5},
    {"test": "other", "cycle_count_max": 5},
    {"baseline": "other"},
    {"candidate": "other"},
]


def _hostile_goals() -> list[dict[str, Any]]:
    goals: list[dict[str, Any]] = [
        {"family": "lint", "target": "wanted"},
        {"family": "sim", "target": "wanted"},
        {"family": "elab", "target": "wanted"},
        {"family": "coverage", "target": "wanted", "metrics": {"line": 5}, "tests": ["a"]},
        {"family": "coverage", "target": "wanted", "metrics": {"targets": 5}, "tests": "all"},
        {"family": "mutation", "target": "wanted", "min_detected": 1, "total": 2},
        {"family": "mutation", "target": "wanted", "targets": ["other"]},
        {"family": "synth", "target": "wanted", "thresholds": {"cell_count_max": 3}},
        {
            "family": "fpga",
            "target": "wanted",
            "baseline": "old",
            "thresholds": {"lut_count_reduce_at_least": "1%"},
        },
        {
            "family": "cycle_count",
            "target": "wanted",
            "test": "smoke",
            "thresholds": {"cycle_count_max": 2},
        },
    ]
    for thresholds in _HOSTILE_THRESHOLDS:
        goals.append({"family": "synth", "target": "wanted", "thresholds": thresholds})
        goals.append({"family": "fpga", "target": "wanted", "thresholds": thresholds})
        goals.append(
            {
                "family": "cycle_count",
                "target": "wanted",
                "test": "smoke",
                "thresholds": thresholds,
            }
        )
    return goals


@pytest.mark.parametrize("raw", _hostile_goals())
def test_goal_key_and_target_always_follow_the_goal_argument(raw: dict[str, Any]) -> None:
    from booley.goals.model import GoalArgError

    try:
        result = translate(raw)
    except (GoalArgError, GoalTranslationError):
        return  # refused at the boundary or by the Criteria rules
    (goal,) = result.goals
    spec = criterion_spec(goal)
    assert spec.expand([]) == [(goal.key, True)]
    assert goal.target == "wanted"
    if raw["family"] == "cycle_count":
        assert goal.key == cycle_count_criterion_key("wanted", "smoke")
        assert (spec.params["target"], spec.params["test"]) == ("wanted", "smoke")
    else:
        assert spec.targets == ["wanted"]
        assert goal.key.endswith("_wanted")
