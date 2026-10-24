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
    TargetPlanError,
    TargetPlans,
    freeze_goals,
    goal_slots,
    refreeze_changed_goal,
    resolution_obstacle,
    resolve_pending,
    unresolved,
)
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
    frozen = freeze(goal(family="SIM"), plan())
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
    assert names(freeze(goal(family="SIM", test=test), plan())) == expected


def test_multi_seed_goal_draws_distinct_seeds_once() -> None:
    spec = goal(family="SIM", test="stress+soak@x3")
    frozen = freeze(spec, plan(), draw=fixed_draw(11, 12, 13))
    assert names(frozen) == ["stress+soak@11", "stress+soak@12", "stress+soak@13"]
    # Resolving again draws nothing: a frozen run is never touched.
    again = resolve_pending(spec, frozen, plan(), request(draw=fixed_draw()))
    assert again == frozen


def test_unseeded_target_freezes_runs_without_seeds_and_refuses_seeded_goals() -> None:
    unseeded = plan("icarus")
    assert names(freeze(goal(family="SIM"), unseeded)) == ["reset", "stress+short"]
    for test in ("stress@7", "stress@x2"):
        with pytest.raises(ValueError, match="seed"):
            freeze(goal(family="SIM", test=test), unseeded)


def test_missing_target_test_and_variant_stay_pending() -> None:
    assert freeze(goal(family="SIM"), None).pending == (SUITE_SLOT,)
    assert freeze(goal(family="SIM", test="later"), plan()).pending == ("later",)
    assert freeze(goal(family="SIM", test="stress+later"), plan()).pending == ("stress+later",)


def test_pending_test_resolves_once_as_deferred() -> None:
    spec = goal(family="SIM", test="later@x2")
    waiting = freeze(spec, plan())
    grown = plan(tests=["reset", "stress", "later"])
    resolved = resolve_pending(spec, waiting, grown, request("deferred", draw=fixed_draw(5, 6)))
    assert names(resolved) == ["later@5", "later@6"]
    assert [item.resolved_by for item in resolved.deferred] == ["deferred", "deferred"]
    assert resolve_pending(spec, resolved, grown, request("deferred")) == resolved


def test_test_added_to_a_target_goal_later_is_frozen_as_deferred() -> None:
    spec = goal(family="SIM")
    frozen = freeze(spec, plan())
    grown = plan(tests=["reset", "stress", "late"], default_seed=5)
    later = resolve_pending(spec, frozen, grown, request("deferred"))
    # The frozen runs keep the seed they had; only the new Test takes the new default.
    assert names(later) == ["reset@1", "stress+short@1", "late@5"]
    assert [item.run.name for item in later.deferred] == ["late@5"]


def test_empty_suite_is_resolved_and_empty_not_pending() -> None:
    frozen = freeze(goal(family="SIM"), plan(skip=["reset", "stress"]))
    assert (frozen.runs, frozen.pending) == ((), ())


def test_unpinned_goal_stays_on_its_frozen_run_after_default_edits() -> None:
    spec = goal(family="SIM", test="stress")
    frozen = freeze(spec, plan())
    moved = plan(
        default_seed=9,
        variants={"stress": {"default": "soak", "short": ["+n=1"], "soak": ["+n=9"]}},
    )
    assert names(resolve_pending(spec, frozen, moved, request("deferred"))) == ["stress+short@1"]


def test_cycle_count_and_coverage_goals_freeze_through_the_same_slots() -> None:
    cycle = goal(family="CYCLE_COUNT", test="stress+soak@7", thresholds={"cycle_count_max": 9})
    assert names(freeze(cycle, plan())) == ["stress+soak@7"]
    coverage = goal(
        family="COVERAGE", metrics={"line": 80}, tests=["reset", "stress@x2", "reset@1"]
    )
    assert goal_slots(coverage) == ("reset", "stress@x2", "reset@1")
    frozen = freeze(coverage, plan(), draw=fixed_draw(4, 5))
    # ``reset`` and ``reset@1`` are one Test Run; it is required once.
    assert names(frozen) == ["reset@1", "stress+short@4", "stress+short@5"]
    whole = goal(family="COVERAGE", metrics={"line": 80}, tests="all")
    assert names(freeze(whole, plan())) == ["reset@1", "stress+short@1"]


def test_changed_goal_keeps_frozen_seeds_and_draws_or_drops_the_difference() -> None:
    before = goal(family="SIM", test="stress@x3")
    held = freeze(before, plan(), draw=fixed_draw(11, 12, 13))
    plans = TargetPlans(Path("unused"), loader=lambda _dir, _target: plan())

    def change(test: str, draw=fixed_draw()) -> list[str]:  # noqa: B008
        after = RecordedGoal(goal(family="SIM", test=test), 2)
        frozen = refreeze_changed_goal(
            after, (before, held), plans, request("change", draw=draw), refreeze=False
        )
        assert frozen.spec_revision == 2
        return names(frozen)

    assert change("stress@x5", draw=fixed_draw(21, 22)) == [
        f"stress+short@{seed}" for seed in (11, 12, 13, 21, 22)
    ]
    assert change("stress@x2") == ["stress+short@11", "stress+short@12"]


def _changed(before, held, after, target_plan, *, draw=fixed_draw(), refreeze=False):  # noqa: B008
    plans = TargetPlans(Path("unused"), loader=lambda _dir, _target: target_plan)
    return refreeze_changed_goal(
        RecordedGoal(after, 2),
        (before, held),
        plans,
        request("change", draw=draw),
        refreeze=refreeze,
    )


def _documents(resolution: TestRunResolution) -> list[dict[str, Any]]:
    return [item.to_json() for item in resolution.runs]


MOVED_DEFAULT = {"stress": {"default": "soak", "short": ["+n=1"], "soak": ["+n=9"]}}
EDITED_ARGS = {"stress": {"default": "short", "short": ["+n=777"], "soak": ["+n=9"]}}


@pytest.mark.parametrize("variants", [MOVED_DEFAULT, EDITED_ARGS, {}])
def test_lowering_n_keeps_the_first_frozen_runs_whatever_tests_toml_says_now(variants) -> None:
    """A smaller N never re-resolves: not the Test Variant, the seeds or the fingerprint."""
    before = goal(family="SIM", test="stress@x3")
    held = freeze(before, plan(), draw=fixed_draw(11, 12, 13))
    after = goal(family="SIM", test="stress@x2")
    lowered = _changed(before, held, after, plan(variants=variants))
    kept = [{**document, "slot": "stress@x2"} for document in _documents(held)[:2]]
    assert _documents(lowered) == kept
    assert lowered.pending == ()


def test_raising_n_keeps_every_frozen_run_and_draws_for_the_frozen_variant() -> None:
    before = goal(family="SIM", test="stress@x2")
    held = freeze(before, plan(), draw=fixed_draw(11, 12))
    after = goal(family="SIM", test="stress@x4")
    # A new Test was added and the default seed moved: neither touches the frozen Variant.
    grown = plan(tests=["reset", "stress", "later"], default_seed=5)
    raised = _changed(before, held, after, grown, draw=fixed_draw(21, 22))
    kept = [{**document, "slot": "stress@x4"} for document in _documents(held)]
    assert _documents(raised)[:2] == kept
    assert names(raised)[2:] == ["stress+short@21", "stress+short@22"]
    assert {item.resolved_by for item in raised.runs[2:]} == {"change"}


@pytest.mark.parametrize(
    ("variants", "reason"),
    [
        (MOVED_DEFAULT, None),  # the frozen Variant still exists as it was frozen
        (EDITED_ARGS, "declared arguments or environment of its frozen Test Run"),
        ({"stress": {"default": "soak", "soak": ["+n=9"]}}, "no longer exists on the Target"),
    ],
)
def test_raising_n_is_refused_when_the_frozen_variant_is_gone_or_changed(variants, reason) -> None:
    before = goal(family="SIM", test="stress@x2")
    held = freeze(before, plan(), draw=fixed_draw(11, 12))
    after = goal(family="SIM", test="stress@x3")
    target_plan = plan(variants=variants)
    if reason is None:
        raised = _changed(before, held, after, target_plan, draw=fixed_draw(21))
        assert names(raised) == ["stress+short@11", "stress+short@12", "stress+short@21"]
        return
    with pytest.raises(GoalResolutionError, match=reason) as refused:
        _changed(before, held, after, target_plan, draw=fixed_draw(21))
    assert "retarget that leaves the Goal as written" in str(refused.value)
    # That retarget freezes the Goal again from the Target as it is now.
    again = _changed(before, held, before, target_plan, draw=fixed_draw(31, 32), refreeze=True)
    assert [run.seed for run in again.test_runs] == [31, 32]
    assert {item.resolved_by for item in again.runs} == {"change"}


def test_a_change_on_a_target_whose_plan_cannot_be_read_names_the_goal_publicly() -> None:
    """The refusal names the Goal as status does, not by its key."""
    before = goal(family="SIM", test="stress@x2")
    held = freeze(before, plan(), draw=fixed_draw(11, 12))
    after = goal(family="SIM", test="stress@x3")

    def unreadable(_dir: Path, _target: str) -> TargetTestPlan:
        raise ValueError("tests.toml [tb] is malformed")

    plans = TargetPlans(Path("unused"), loader=unreadable)
    refusal = r"Goal SIM:tb \(all selected tests pass, test stress@x3\): .*cannot be resolved"
    with pytest.raises(GoalResolutionError, match=refusal) as refused:
        refreeze_changed_goal(
            RecordedGoal(after, 2), (before, held), plans, request("change"), refreeze=False
        )
    assert after.key not in str(refused.value)


def test_raising_n_of_a_coverage_goal_with_a_pin_draws_the_difference() -> None:
    before = goal(family="COVERAGE", tests=["stress@5", "stress@x3"], metrics={"line": 80})
    held = freeze(before, plan(), draw=fixed_draw(11, 12, 13))
    assert names(held) == [f"stress+short@{seed}" for seed in (5, 11, 12, 13)]
    after = goal(family="COVERAGE", tests=["stress@5", "stress@x4"], metrics={"line": 80})
    raised = _changed(before, held, after, plan(), draw=fixed_draw(21))
    assert names(raised) == [f"stress+short@{seed}" for seed in (5, 11, 12, 13, 21)]
    assert _documents(raised)[0] == _documents(held)[0]


def test_a_drawn_seed_never_repeats_a_seed_the_goal_pins() -> None:
    spec = goal(family="COVERAGE", tests=["stress@x2", "stress@5"], metrics={"line": 80})
    seen: list[set[int]] = []

    def draw(count: int, *, exclude=()) -> tuple[int, ...]:
        seen.append(set(exclude))
        return tuple(range(100, 100 + count))

    assert len(freeze(spec, plan(), draw=draw).runs) == 3
    assert seen == [{5}]


def test_equal_multi_seed_slots_of_one_target_share_one_set_of_seeds() -> None:
    """``--test stress@x2`` names one set of Test Runs, whichever Goals require it."""
    specs = translate_goals(
        parse_goal_args(
            [
                {"family": "SIM", "target": "tb", "test": "stress@x2"},
                {
                    "family": "COVERAGE",
                    "target": "lib#tb",
                    "tests": ["stress+short@x2"],
                    "metrics": {"line": 80},
                },
                {"family": "SIM", "target": "other", "test": "stress@x2"},
            ]
        )
    ).goals
    plans = TargetPlans(
        Path("unused"),
        loader=lambda _dir, _target: plan(),
        identities=lambda _dir, target: target.rpartition("#")[2],
    )
    goals = tuple(RecordedGoal(spec, 1) for spec in specs)
    frozen = freeze_goals(goals, {}, plans, request(draw=fixed_draw(11, 12, 31, 32)), strict=True)
    seeds = [[run.seed for run in frozen[spec.key].test_runs] for spec in specs]
    assert seeds == [[11, 12], [11, 12], [31, 32]]


def test_first_registered_test_supersedes_the_unnamed_run_of_a_target_goal() -> None:
    spec = goal(family="SIM")
    bare = build_target_test_plan(
        "tb", normalize_tests_toml({"tb": {}})["tb"], adapter="verilator"
    )
    held = freeze(spec, bare)
    assert [(run.name, run.test) for run in held.test_runs] == [("<default>@1", None)]
    later = resolve_pending(spec, held, plan(), request("deferred"))
    assert names(later) == ["reset@1", "stress+short@1"]
    assert {item.resolved_by for item in later.runs} == {"deferred"}
    assert resolution_update_violation("k", held, later) == ""
    # Nothing else may drop a frozen run: not a named one, not for an empty suite, and not
    # while the unnamed run is kept beside the named ones.
    assert resolve_pending(spec, later, bare, request("deferred")) == later
    assert "cannot change" in resolution_update_violation("k", later, replace(later, runs=()))
    assert "cannot change" in resolution_update_violation("k", held, replace(held, runs=()))
    assert "cannot change" in resolution_update_violation(
        "k", later, replace(later, runs=later.runs[1:])
    )
    selector = goal(family="SIM", test="stress")
    frozen = freeze(selector, plan())
    assert "cannot change" in resolution_update_violation(
        "k", frozen, replace(frozen, runs=later.runs)
    )


def test_relaxed_goal_keeps_frozen_runs_and_retargeted_goal_resolves_again() -> None:
    before = goal(family="COVERAGE", metrics={"line": 80}, tests="all")
    held = freeze(before, plan())
    moved = TargetPlans(Path("unused"), loader=lambda _dir, _target: plan(default_seed=9))
    relaxed = RecordedGoal(goal(family="COVERAGE", metrics={"line": 70}, tests="all"), 2)
    kept = refreeze_changed_goal(relaxed, (before, held), moved, request("change"), refreeze=False)
    assert names(kept) == ["reset@1", "stress+short@1"]
    again = refreeze_changed_goal(relaxed, (before, held), moved, request("change"), refreeze=True)
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
                {"family": "SIM", "target": "gone"},
                {"family": "LINT", "target": "tb"},
                {"family": "SIM", "target": "tb", "test": "stress@x2"},
            ]
        )
    ).goals
    goals = tuple(RecordedGoal(spec, 1) for spec in specs)
    refusal = r"Goal SIM:tb \(all selected tests pass, test stress@x2\): .*seed"
    with pytest.raises(GoalResolutionError, match=refusal) as refused:
        freeze_goals(goals, {}, plans, request(), strict=True)
    assert "sim_pass_tb_run" not in str(refused.value)  # the public Goal name, not its key
    lenient = freeze_goals(goals, {}, plans, request("deferred"), strict=False)
    assert sorted(lenient) == ["sim_pass_gone", "sim_pass_tb_run_737472657373_x"]
    assert lenient["sim_pass_gone"].pending == (SUITE_SLOT,)
    assert lenient["sim_pass_tb_run_737472657373_x"].pending == ("stress@x2",)
    with pytest.raises(TargetPlanError, match="cannot be resolved"):
        plans.plan("broken")


SKIPPED_GOALS = {
    "test": {"family": "SIM", "test": "stress"},
    "variant": {"family": "SIM", "test": "stress+soak"},
    "pinned-seed": {"family": "SIM", "test": "stress@7"},
    "seed-count": {"family": "SIM", "test": "stress@x2"},
    "coverage-list": {
        "family": "COVERAGE",
        "tests": ["reset", "stress"],
        "metrics": {"line": 90},
    },
    "cycle-count": {
        "family": "CYCLE_COUNT",
        "test": "stress@7",
        "thresholds": {"cycle_count_max": 900},
    },
}


@pytest.mark.parametrize("form", SKIPPED_GOALS)
def test_a_goal_on_a_skipped_test_is_refused_strictly_and_stays_pending_when_deferred(
    form: str,
) -> None:
    """tests.toml skips the Test: its runs never count, so a Goal naming it is never frozen."""
    skipping = plan(skip=["stress"])
    # A Goal on the whole Target is unchanged: the skipped Test is simply not in the suite.
    assert names(freeze(goal(family="SIM"), skipping)) == ["reset@1"]
    spec = goal(**SKIPPED_GOALS[form])
    plans = TargetPlans(Path("unused"), loader=lambda _dir, _target: skipping)
    with pytest.raises(
        GoalResolutionError, match=r"Goal .*: Test 'stress' of Target 'tb' is skipped"
    ):
        freeze_goals((RecordedGoal(spec, 1),), {}, plans, request(), strict=True)
    lenient = freeze_goals((RecordedGoal(spec, 1),), {}, plans, request("deferred"), strict=False)
    assert lenient[spec.key].runs == ()
    assert lenient[spec.key].pending == goal_slots(spec)
    obstacle = resolution_obstacle(spec, lenient[spec.key], plans)
    assert "Test 'stress' of Target 'tb' is skipped" in obstacle


def test_raising_n_of_a_test_skipped_since_entry_is_refused() -> None:
    before = goal(family="SIM", test="stress@x2")
    held = freeze(before, plan(), draw=fixed_draw(11, 12))
    after = goal(family="SIM", test="stress@x3")
    with pytest.raises(
        GoalResolutionError, match=r"Goal SIM:tb .*: Test 'stress' of Target 'tb' is skipped"
    ):
        _changed(before, held, after, plan(skip=["stress"]), draw=fixed_draw(21))


# --- What a save may change ------------------------------------------------------


def test_resolution_round_trips_and_refuses_malformed_content() -> None:
    frozen = {"k": freeze(goal(family="SIM", test="stress@x2"), plan(), draw=fixed_draw(3, 4))}
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
    spec = goal(family="SIM", test="later")
    waiting = freeze(spec, plan())
    filled = resolve_pending(
        spec, waiting, plan(tests=["reset", "stress", "later"]), request("deferred")
    )
    assert resolution_update_violation("k", waiting, filled) == ""
    assert (
        resolution_update_violation("SIM:tb", filled, waiting)
        == "the frozen Test Runs of Goal SIM:tb cannot change"
    )
    other = freeze(goal(family="SIM", test="reset@5"), plan())
    assert "cannot change" in resolution_update_violation("k", filled, other)
    assert (
        resolution_update_violation(
            "SIM:tb", filled, replace(filled, runs=(*filled.runs, *other.runs))
        )
        == "Test Run reset@5 of Goal SIM:tb fills no pending slot"
    )
    assert (
        resolution_update_violation("SIM:tb", filled, replace(filled, pending=("x",)))
        == "Goal SIM:tb cannot gain pending Test Run slots"
    )
    suite = freeze(goal(family="SIM"), plan())
    grown = resolve_pending(
        goal(family="SIM"), suite, plan(tests=["reset", "stress", "late"]), request("deferred")
    )
    assert resolution_update_violation("k", suite, grown) == ""
    twice = replace(grown, runs=(*grown.runs, replace(other.runs[0], slot=SUITE_SLOT)))
    assert "fills no pending slot" in resolution_update_violation("k", grown, twice)
