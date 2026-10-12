"""Freezing a Goal's Test Runs from its Target's Test Run plan (ADR 0072)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.target_test_plan import TargetTestPlan, build_target_test_plan
from booley.goals.model import GoalSpec, RecordedGoal, parse_goal_args
from booley.goals.test_run_freezing import (
    GoalResolutionError,
    ResolutionRequest,
    freeze_goals,
    goal_slots,
    refreeze_changed_goal,
    resolve_pending,
    unresolved,
)
from booley.goals.test_run_plans import TargetPlanError, TargetPlans
from booley.goals.test_run_resolution import (
    SUITE_SLOT,
    ResolutionFormatError,
    TestRunResolution,
    resolution_update_violation,
    resolutions_from_json,
    resolutions_to_json,
)
from booley.goals.translate import translate_goals
from booley.targets.domain import UnknownTargetError

AT = "2026-10-13T10:00:00Z"
SECTION: dict[str, Any] = {
    "tests": ["reset", "stress"],
    "variants": {"stress": {"default": "short", "short": ["+n=1"], "soak": ["+n=9"]}},
}


def plan(adapter: str = "verilator", **changes: Any) -> TargetTestPlan:
    section = normalize_tests_toml({"tb": {**SECTION, **changes}})["tb"]
    return build_target_test_plan("tb", section, adapter=adapter)


def goal(**raw: Any) -> GoalSpec:
    (spec,) = translate_goals(parse_goal_args([{"target": "tb", **raw}])).goals
    return spec


def fixed_draw(*seeds: int):
    """Hand out *seeds* in order, never one the caller excludes."""
    pending = list(seeds)

    def draw(count: int, *, exclude=()) -> tuple[int, ...]:
        drawn = [pending.pop(0) for _ in range(count)]
        assert not set(drawn) & set(exclude)
        return tuple(drawn)

    return draw


def request(by: str = "entry", **extra: Any) -> ResolutionRequest:
    return ResolutionRequest(by, AT, **extra)  # type: ignore[arg-type]


def freeze(spec: GoalSpec, target_plan: TargetTestPlan | None, **extra: Any) -> TestRunResolution:
    return resolve_pending(spec, unresolved(spec, 1), target_plan, request(**extra))


def names(resolution: TestRunResolution) -> list[str]:
    return [run.name for run in resolution.test_runs]


def test_target_goal_freezes_every_test_as_its_default_variant_at_the_default_seed() -> None:
    frozen = freeze(goal(family="sim"), plan())
    assert names(frozen) == ["reset@1", "stress+short@1"]
    assert frozen.pending == ()
    assert {item.slot for item in frozen.runs} == {SUITE_SLOT}
    assert {(item.resolved_by, item.resolved_at) for item in frozen.runs} == {("entry", AT)}


@pytest.mark.parametrize(
    ("test", "expected"),
    [
        ("stress", ["stress+short@1"]),
        ("stress+soak", ["stress+soak@1"]),
        ("stress+soak@7", ["stress+soak@7"]),
        ("reset@9", ["reset@9"]),
    ],
)
def test_test_goal_freezes_its_one_test_run(test: str, expected: list[str]) -> None:
    assert names(freeze(goal(family="sim", test=test), plan())) == expected


def test_multi_seed_goal_draws_distinct_seeds_once() -> None:
    spec = goal(family="sim", test="stress+soak@x3")
    frozen = freeze(spec, plan(), draw=fixed_draw(11, 12, 13))
    assert names(frozen) == ["stress+soak@11", "stress+soak@12", "stress+soak@13"]
    # Resolving again draws nothing: a frozen run is never touched.
    again = resolve_pending(spec, frozen, plan(), request(draw=fixed_draw()))
    assert again == frozen


def test_unseeded_target_freezes_runs_without_seeds_and_refuses_seeded_goals() -> None:
    unseeded = plan("icarus")
    assert names(freeze(goal(family="sim"), unseeded)) == ["reset", "stress+short"]
    for test in ("stress@7", "stress@x2"):
        with pytest.raises(ValueError, match="seed"):
            freeze(goal(family="sim", test=test), unseeded)


def test_missing_target_test_and_variant_stay_pending() -> None:
    assert freeze(goal(family="sim"), None).pending == (SUITE_SLOT,)
    assert freeze(goal(family="sim", test="later"), plan()).pending == ("later",)
    assert freeze(goal(family="sim", test="stress+later"), plan()).pending == ("stress+later",)


def test_pending_test_resolves_once_as_deferred() -> None:
    spec = goal(family="sim", test="later@x2")
    waiting = freeze(spec, plan())
    grown = plan(tests=["reset", "stress", "later"])
    resolved = resolve_pending(spec, waiting, grown, request("deferred", draw=fixed_draw(5, 6)))
    assert names(resolved) == ["later@5", "later@6"]
    assert [item.resolved_by for item in resolved.deferred] == ["deferred", "deferred"]
    assert resolve_pending(spec, resolved, grown, request("deferred")) == resolved


def test_test_added_to_a_target_goal_later_is_frozen_as_deferred() -> None:
    spec = goal(family="sim")
    frozen = freeze(spec, plan())
    grown = plan(tests=["reset", "stress", "late"], default_seed=5)
    later = resolve_pending(spec, frozen, grown, request("deferred"))
    # The frozen runs keep the seed they had; only the new Test takes the new default.
    assert names(later) == ["reset@1", "stress+short@1", "late@5"]
    assert [item.run.name for item in later.deferred] == ["late@5"]


def test_empty_suite_is_resolved_and_empty_not_pending() -> None:
    frozen = freeze(goal(family="sim"), plan(skip=["reset", "stress"]))
    assert (frozen.runs, frozen.pending) == ((), ())


def test_unpinned_goal_stays_on_its_frozen_run_after_default_edits() -> None:
    spec = goal(family="sim", test="stress")
    frozen = freeze(spec, plan())
    moved = plan(
        default_seed=9,
        variants={"stress": {"default": "soak", "short": ["+n=1"], "soak": ["+n=9"]}},
    )
    assert names(resolve_pending(spec, frozen, moved, request("deferred"))) == ["stress+short@1"]


def test_cycle_count_and_coverage_goals_freeze_through_the_same_slots() -> None:
    cycle = goal(family="cycle_count", test="stress+soak@7", thresholds={"cycle_count_max": 9})
    assert names(freeze(cycle, plan())) == ["stress+soak@7"]
    coverage = goal(
        family="coverage", metrics={"line": 80}, tests=["reset", "stress@x2", "reset@1"]
    )
    assert goal_slots(coverage) == ("reset", "stress@x2", "reset@1")
    frozen = freeze(coverage, plan(), draw=fixed_draw(4, 5))
    # ``reset`` and ``reset@1`` are one Test Run; it is required once.
    assert names(frozen) == ["reset@1", "stress+short@4", "stress+short@5"]
    whole = goal(family="coverage", metrics={"line": 80}, tests="all")
    assert names(freeze(whole, plan())) == ["reset@1", "stress+short@1"]


def test_changed_goal_keeps_frozen_seeds_and_draws_or_drops_the_difference() -> None:
    before = goal(family="sim", test="stress@x3")
    held = freeze(before, plan(), draw=fixed_draw(11, 12, 13))
    plans = TargetPlans(Path("unused"), loader=lambda _dir, _target: plan())

    def change(test: str, *, keep: bool, draw=fixed_draw()) -> list[str]:  # noqa: B008
        after = RecordedGoal(goal(family="sim", test=test), 2)
        frozen = refreeze_changed_goal(
            after, (before, held), plans, request("change", draw=draw), keep_frozen=keep
        )
        assert frozen.spec_revision == 2
        return names(frozen)

    assert change("stress@x5", keep=False, draw=fixed_draw(21, 22)) == [
        f"stress+short@{seed}" for seed in (11, 12, 13, 21, 22)
    ]
    assert change("stress@x2", keep=True) == ["stress+short@11", "stress+short@12"]


def test_relaxed_goal_keeps_frozen_runs_and_retargeted_goal_resolves_again() -> None:
    before = goal(family="coverage", metrics={"line": 80}, tests="all")
    held = freeze(before, plan())
    moved = TargetPlans(Path("unused"), loader=lambda _dir, _target: plan(default_seed=9))
    relaxed = RecordedGoal(goal(family="coverage", metrics={"line": 70}, tests="all"), 2)
    kept = refreeze_changed_goal(
        relaxed, (before, held), moved, request("change"), keep_frozen=True
    )
    assert names(kept) == ["reset@1", "stress+short@1"]
    again = refreeze_changed_goal(
        relaxed, (before, held), moved, request("change"), keep_frozen=False
    )
    assert names(again) == ["reset@9", "stress+short@9"]
    assert {item.resolved_by for item in again.runs} == {"change"}


def test_freeze_goals_is_strict_at_entry_and_lenient_when_deferred() -> None:
    def loader(_dir: Path, target: str) -> TargetTestPlan:
        if target == "gone":
            raise UnknownTargetError(target)
        if target == "broken":
            raise ValueError("tests.toml [broken] is malformed")
        return plan("icarus")

    plans = TargetPlans(Path("unused"), loader=loader)
    specs = translate_goals(
        parse_goal_args(
            [
                {"family": "sim", "target": "gone"},
                {"family": "lint", "target": "tb"},
                {"family": "sim", "target": "tb", "test": "stress@x2"},
            ]
        )
    ).goals
    goals = tuple(RecordedGoal(spec, 1) for spec in specs)
    with pytest.raises(GoalResolutionError, match=r"Goal sim_run_tb_\w+_x: .*seed"):
        freeze_goals(goals, {}, plans, request(), strict=True)
    lenient = freeze_goals(goals, {}, plans, request("deferred"), strict=False)
    assert sorted(lenient) == ["sim_pass_gone", "sim_run_tb_737472657373_x"]
    assert lenient["sim_pass_gone"].pending == (SUITE_SLOT,)
    assert lenient["sim_run_tb_737472657373_x"].pending == ("stress@x2",)
    with pytest.raises(TargetPlanError, match="cannot be resolved"):
        plans.plan("broken")


# --- What a save may change ------------------------------------------------------


def test_resolution_round_trips_and_refuses_malformed_content() -> None:
    frozen = {"k": freeze(goal(family="sim", test="stress@x2"), plan(), draw=fixed_draw(3, 4))}
    stored = resolutions_to_json(frozen)
    assert resolutions_from_json(stored) == frozen
    row = stored["k"]["runs"][0]
    for broken in (
        {**stored["k"], "extra": 1},
        {**stored["k"], "spec_revision": 0},
        {**stored["k"], "pending": [""]},
        {**stored["k"], "runs": [row, row]},
        {**stored["k"], "runs": [{**row, "resolved_by": "agent"}]},
        {**stored["k"], "runs": [{**row, "fingerprint": "0" * 64}]},
        {**stored["k"], "runs": [{**row, "seed": 99}]},
    ):
        with pytest.raises(ResolutionFormatError):
            resolutions_from_json({"k": broken})


def test_an_update_only_fills_pending_slots() -> None:
    spec = goal(family="sim", test="later")
    waiting = freeze(spec, plan())
    filled = resolve_pending(
        spec, waiting, plan(tests=["reset", "stress", "later"]), request("deferred")
    )
    assert resolution_update_violation("k", waiting, filled) == ""
    assert "cannot change" in resolution_update_violation("k", filled, waiting)
    other = freeze(goal(family="sim", test="reset@5"), plan())
    assert "cannot change" in resolution_update_violation("k", filled, other)
    assert "fills no pending slot" in resolution_update_violation(
        "k", filled, replace(filled, runs=(*filled.runs, *other.runs))
    )
    assert "cannot gain pending" in resolution_update_violation(
        "k", filled, replace(filled, pending=("x",))
    )
    suite = freeze(goal(family="sim"), plan())
    grown = resolve_pending(
        goal(family="sim"), suite, plan(tests=["reset", "stress", "late"]), request("deferred")
    )
    assert resolution_update_violation("k", suite, grown) == ""
    twice = replace(grown, runs=(*grown.runs, replace(other.runs[0], slot=SUITE_SLOT)))
    assert "fills no pending slot" in resolution_update_violation("k", grown, twice)
