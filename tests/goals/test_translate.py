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


# --- Goals that name Test Runs (ADR 0072) -----------------------------------------


def _resolver(default_variant: str | None = "short", default_seed: int | None = 1):
    """Canonical selectors of a Target whose Tests default to *default_variant*."""
    from booley.flows.test_runs import PinnedSeed, TestSelector, UnpinnedSeed
    from booley.goals.test_run_goals import CanonicalNames

    def resolve(_target: str, selector: TestSelector) -> TestSelector | None:
        if selector.test == "later":
            return None  # the Test does not exist yet
        form = selector.seed_form
        if isinstance(form, UnpinnedSeed) and default_seed is not None:
            form = PinnedSeed(default_seed)
        return TestSelector(selector.test, selector.variant or default_variant, form)

    # ``tb`` and ``lib#tb`` are one Target.
    return CanonicalNames(resolve, lambda target: f"lib#{target.rpartition('#')[2]}")


def test_one_target_written_two_ways_is_one_goal_with_one_set_of_seeds() -> None:
    """Aliases of a Target merge before any seed is drawn (the larger N wins)."""
    args = parse_goal_args(
        [
            {"family": "sim", "target": "tb", "test": "stress@x2", "origin": "a"},
            {"family": "sim", "target": "lib#tb", "test": "stress@x3", "origin": "b"},
            {"family": "sim", "target": "tb", "origin": "a"},
            {"family": "sim", "target": "lib#tb", "origin": "b"},
            {
                "family": "coverage",
                "target": "tb",
                "tests": "all",
                "metrics": {"line": 80},
                "origin": "a",
            },
            {
                "family": "coverage",
                "target": "lib#tb",
                "tests": "all",
                "metrics": {"line": 90},
                "origin": "b",
            },
        ]
    )
    assert len(translate_goals(args).goals) == 6  # nothing says the two names are one Target
    merged = translate_goals(args, resolver=_resolver())
    assert [(goal.key, goal.target, goal.origins) for goal in merged.goals] == [
        ("sim_pass_lib#tb_run_737472657373_x", "lib#tb", ("a", "b")),
        ("sim_pass_tb", "tb", ("a", "b")),
        ("coverage_tb", "tb", ("a", "b")),
    ]
    assert merged.goals[0].params == {"test": "stress@x3"}
    assert merged.goals[2].params["metrics"]["line"]["min_pct"] == 90


@pytest.mark.parametrize(
    ("test", "key"),
    [
        ("smoke", "sim_pass_tb_run_736d6f6b65_d"),
        ("stress+soak", "sim_pass_tb_run_7374726573732b736f616b_d"),
        ("stress+soak@7", "sim_pass_tb_run_7374726573732b736f616b_s7"),
        ("stress@x5", "sim_pass_tb_run_737472657373_x"),
    ],
)
def test_test_goal_key_comes_from_the_written_selector(test: str, key: str) -> None:
    (goal,) = translate({"family": "sim", "target": "tb", "test": test}).goals
    assert (goal.key, goal.family, goal.target) == (key, GoalFamily.SIM, "tb")
    assert goal.params == {"test": test}
    assert criterion_spec(goal).expand([])[0][0] == key


def test_target_goal_and_bare_cycle_count_keys_are_unchanged() -> None:
    goals = translate(
        {"family": "sim", "target": "tb"},
        {"family": "cycle_count", "target": "tb", "test": "smoke", "thresholds": CYCLE},
    ).goals
    assert [goal.key for goal in goals] == [
        "sim_pass_tb",
        cycle_count_criterion_key("tb", "smoke"),
    ]


CYCLE = {"cycle_count_max": 900}


def test_cycle_count_key_is_built_from_the_written_selector() -> None:
    (goal,) = translate(
        {"family": "cycle_count", "target": "tb", "test": "stress+soak@7", "thresholds": CYCLE}
    ).goals
    assert goal.key == cycle_count_criterion_key("tb", "stress+soak@7")
    assert goal.params["test"] == "stress+soak@7"


def test_goals_that_resolve_to_one_test_run_merge_and_union_their_origins() -> None:
    args = parse_goal_args(
        [
            {"family": "sim", "target": "tb", "test": "stress", "origin": "a"},
            {"family": "sim", "target": "tb", "test": "stress+short", "origin": "b"},
            {"family": "sim", "target": "tb", "test": "stress@1", "origin": "c"},
        ]
    )
    unresolved = translate_goals(args)
    assert len(unresolved.goals) == 3  # written forms differ; nothing says they are one run
    merged = translate_goals(args, resolver=_resolver())
    (goal,) = merged.goals
    assert goal.key == "sim_pass_tb_run_737472657373_d"  # the first written form
    assert goal.origins == ("a", "b", "c")
    assert "name the same Test Runs" in merged.warnings[0]


def test_two_multi_seed_goals_merge_to_the_larger_count() -> None:
    merged = translate_goals(
        parse_goal_args(
            [
                {"family": "sim", "target": "tb", "test": "stress@x3", "origin": "a"},
                {"family": "sim", "target": "tb", "test": "stress+short@x5", "origin": "b"},
            ]
        ),
        resolver=_resolver(),
    )
    (goal,) = merged.goals
    assert goal.params == {"test": "stress+short@x5"}
    assert goal.origins == ("a", "b")
    assert "seed counts 3 (from a), 5 (from b) -> stricter 5" in merged.warnings[0]


def test_same_written_multi_seed_goals_merge_without_a_resolver() -> None:
    (goal,) = translate(
        {"family": "sim", "target": "tb", "test": "stress@x3"},
        {"family": "sim", "target": "tb", "test": "stress@x2", "origin": "b"},
    ).goals
    assert goal.params == {"test": "stress@x3"}


def test_pinned_and_multi_seed_goals_on_one_variant_stay_separate() -> None:
    goals = translate_goals(
        parse_goal_args(
            [
                {"family": "sim", "target": "tb", "test": "stress@x3"},
                {"family": "sim", "target": "tb", "test": "stress@7"},
                {"family": "sim", "target": "tb", "test": "stress"},
                {"family": "sim", "target": "tb"},
            ]
        ),
        resolver=_resolver(),
    ).goals
    assert len(goals) == 4


def test_a_test_that_does_not_exist_yet_keeps_its_written_identity() -> None:
    goals = translate_goals(
        parse_goal_args(
            [
                {"family": "sim", "target": "tb", "test": "later"},
                {"family": "sim", "target": "tb", "test": "later+short"},
            ]
        ),
        resolver=_resolver(),
    ).goals
    assert len(goals) == 2


def test_cycle_count_goals_on_one_test_run_merge_to_the_stricter_threshold() -> None:
    merged = translate_goals(
        parse_goal_args(
            [
                {"family": "cycle_count", "target": "tb", "test": "stress", "thresholds": CYCLE},
                {
                    "family": "cycle_count",
                    "target": "tb",
                    "test": "stress+short@1",
                    "thresholds": {"cycle_count_max": 800},
                    "origin": "b",
                },
            ]
        ),
        resolver=_resolver(),
    )
    (goal,) = merged.goals
    assert goal.key == cycle_count_criterion_key("tb", "stress")
    assert goal.params["cycle_count_max"] == 800


def test_coverage_selections_compare_by_resolved_test_runs() -> None:
    metrics = {"line": 80}
    same = [
        {"family": "coverage", "target": "tb", "metrics": metrics, "tests": ["a", "s@x3"]},
        {"family": "coverage", "target": "tb", "metrics": metrics, "tests": ["s+short@x3", "a@1"]},
    ]
    (goal,) = translate_goals(parse_goal_args(same), resolver=_resolver()).goals
    assert goal.params["tests"] == ["a", "s@x3"]
    with pytest.raises(GoalConflictError, match="coverage test selections"):
        translate_goals(parse_goal_args(same))
    other_count = [same[0], {**same[1], "tests": ["s+short@x4", "a@1"]}]
    with pytest.raises(GoalConflictError, match="coverage test selections"):
        translate_goals(parse_goal_args(other_count), resolver=_resolver())
