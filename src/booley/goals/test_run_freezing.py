"""Freeze the Test Runs of a Goal from its Target's Test Run plan (ADR 0072).

:func:`resolve_pending` fills the slots of a Goal's
:class:`~booley.goals.test_run_resolution.TestRunResolution` that can be
filled now and leaves every frozen run alone. It is the one rule entry,
deferred resolution and an approved Goal change all use:

- a selector slot becomes the Test Run it names: its Test Variant (the
  default one when none is written) at the written seed, or at the Target's
  Default Seed; ``@xN`` becomes N runs with distinct drawn seeds;
- the suite slot becomes the default Test Variant of every Test of the
  Target at the Default Seed, and later gains the Tests added to the Target;
- a slot whose Test or Test Variant does not exist yet stays pending.

Pure: the caller supplies the plan and the seed source.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from booley.config.tests_toml_rules import UnknownVariantError
from booley.flows.target_test_plan import TargetTestPlan, UnknownTestError
from booley.flows.test_runs import (
    PinnedSeed,
    SeedCount,
    TestRun,
    draw_seeds,
    parse_test_selector,
)
from booley.goals.model import GoalSpec, RecordedGoal
from booley.goals.test_run_goals import goal_selectors, names_test_runs, selector_text
from booley.goals.test_run_plans import TargetPlanError, TargetPlans
from booley.goals.test_run_resolution import (
    SUITE_SLOT,
    FrozenRun,
    ResolvedBy,
    TestRunResolution,
)

SeedDraw = Callable[..., tuple[int, ...]]


def goal_slots(goal: GoalSpec) -> tuple[str, ...]:
    """The slots of *goal*'s resolution, in written order."""
    selectors = goal_selectors(goal)
    if selectors is None:
        return (SUITE_SLOT,)
    return tuple(dict.fromkeys(selector_text(selector) for selector in selectors))


def unresolved(goal: GoalSpec, spec_revision: int) -> TestRunResolution:
    """The resolution of a Goal none of whose slots is resolved yet."""
    return TestRunResolution(spec_revision, (), goal_slots(goal))


@dataclass(frozen=True)
class ResolutionRequest:
    """How newly frozen runs are stamped, and where their seeds come from.

    ``reuse`` holds runs whose seeds a multi-seed slot keeps before it draws
    new ones: the Goal's runs before an approved change of its N.
    """

    resolved_by: ResolvedBy
    resolved_at: str
    draw: SeedDraw = draw_seeds
    reuse: tuple[TestRun, ...] = ()


def resolve_pending(
    goal: GoalSpec,
    current: TestRunResolution,
    plan: TargetTestPlan | None,
    request: ResolutionRequest,
) -> TestRunResolution:
    """*current* with every slot filled that *plan* can fill now.

    *plan* is the Target's Test Run plan, or ``None`` while the Target does
    not exist; then nothing changes. A frozen run is never touched. Raises
    :class:`~booley.flows.test_runs.UnseededTargetError` for a seed the
    Target cannot apply, and ``ValueError`` (the Test Run error types) for a
    selector the Target refuses for another reason than a missing Test.
    """
    if plan is None:
        return current
    runs = list(current.runs)
    pending: list[str] = []
    for slot in current.pending:
        found = _slot_runs(plan, slot, [item.run for item in runs], request)
        if found is None:
            pending.append(slot)
            continue
        runs.extend(_stamp(found, slot, runs, request))
    if SUITE_SLOT in goal_slots(goal) and SUITE_SLOT not in current.pending:
        runs.extend(_stamp(_added_tests(plan, runs), SUITE_SLOT, runs, request))
    return replace(current, runs=tuple(runs), pending=tuple(pending))


def _stamp(
    found: Sequence[TestRun], slot: str, frozen: Sequence[FrozenRun], request: ResolutionRequest
) -> list[FrozenRun]:
    """*found* as frozen runs of *slot*, without any run the Goal already holds."""
    names = {item.run.name for item in frozen}
    fresh = [run for run in dict.fromkeys(found) if run.name not in names]
    return [FrozenRun(run, slot, request.resolved_by, request.resolved_at) for run in fresh]


def _added_tests(plan: TargetTestPlan, frozen: Sequence[FrozenRun]) -> list[TestRun]:
    """Default runs of Tests the suite slot has not frozen a run of yet."""
    known = {item.run.test for item in frozen if item.slot == SUITE_SLOT}
    return [run for run in plan.default_runs() if run.test not in known and run.test is not None]


def _slot_runs(
    plan: TargetTestPlan, slot: str, frozen: Sequence[TestRun], request: ResolutionRequest
) -> list[TestRun] | None:
    """The Test Runs *slot* stands for, or ``None`` while its Test does not exist."""
    if slot == SUITE_SLOT:
        return list(plan.default_runs())
    selector = parse_test_selector(slot)
    try:
        base = plan.run(selector.test, selector.variant)
    except (UnknownTestError, UnknownVariantError):
        return None
    form = selector.seed_form
    if isinstance(form, PinnedSeed):
        return [plan.run(selector.test, selector.variant, form.seed)]
    if not isinstance(form, SeedCount):
        return [base]
    plan.run(selector.test, selector.variant, 1)  # refuses a Target that cannot apply seeds
    seeds = _multi_seed(base, form.count, frozen, request)
    return [plan.run(selector.test, selector.variant, seed) for seed in seeds]


def _multi_seed(
    base: TestRun,
    count: int,
    frozen: Sequence[TestRun],
    request: ResolutionRequest,
) -> tuple[int, ...]:
    """N distinct seeds: the reused ones first, in their frozen order, then new draws."""
    kept = tuple(seed for seed in _seeds_of(request.reuse, base))[:count]
    taken: Collection[int] = {*kept, *_seeds_of(frozen, base)}
    return (*kept, *request.draw(count - len(kept), exclude=taken))


def _seeds_of(runs: Sequence[TestRun], base: TestRun) -> list[int]:
    return [
        run.seed
        for run in runs
        if run.test == base.test and run.variant == base.variant and run.seed is not None
    ]


class GoalResolutionError(ValueError):
    """A Goal's Test Runs cannot be frozen on its Target as the Goal is written."""


def freeze_goals(
    goals: Iterable[RecordedGoal],
    existing: Mapping[str, TestRunResolution],
    plans: TargetPlans,
    request: ResolutionRequest,
    *,
    strict: bool,
) -> dict[str, TestRunResolution]:
    """The resolutions of *goals* with every slot filled that can be filled now.

    A Goal keeps the resolution *existing* holds at its current revision and
    otherwise starts unresolved. With *strict* (entry and an approved change)
    a Goal its Target refuses raises :class:`GoalResolutionError`, naming the
    Goal; without it (deferred resolution) that Goal is left as it was.
    """
    frozen: dict[str, TestRunResolution] = {}
    for goal in goals:
        if not names_test_runs(goal.spec):
            continue
        assert goal.spec.target is not None
        held = existing.get(goal.spec.key)
        if held is None or held.spec_revision != goal.spec_revision:
            held = unresolved(goal.spec, goal.spec_revision)
        try:
            plan = plans.plan(goal.spec.target)
            frozen[goal.spec.key] = resolve_pending(goal.spec, held, plan, request)
        except ValueError as exc:  # TargetPlanError and the Test Run error types
            if strict:
                detail = str(exc) if isinstance(exc, TargetPlanError) else f"{exc}"
                raise GoalResolutionError(f"Goal {goal.spec.key}: {detail}") from exc
            frozen[goal.spec.key] = held
    return frozen


def refreeze_changed_goal(
    goal: RecordedGoal,
    before: tuple[GoalSpec, TestRunResolution] | None,
    plans: TargetPlans,
    request: ResolutionRequest,
    *,
    keep_frozen: bool,
) -> TestRunResolution:
    """The resolution of a Goal an approved change just gave a new revision.

    *before* is the Goal as it was and its resolution, when it had one. With
    *keep_frozen* (a relaxation or a waiver, which leave the test selection
    alone) every slot the Goal still names keeps its frozen runs; otherwise
    (an added or retargeted Goal) every slot is resolved from the Target's
    plan as it is now. Either way a multi-seed slot keeps the seeds it had,
    in their frozen order: a smaller N keeps the first N, a larger N draws
    only the difference. Raises :class:`GoalResolutionError`.
    """
    start = unresolved(goal.spec, goal.spec_revision)
    if before is not None:
        old_goal, old = before
        request = replace(request, reuse=old.test_runs)
        if keep_frozen:
            resolved = set(goal_slots(old_goal)) - set(old.pending)
            carried = tuple(item for item in old.runs if item.slot in start.pending)
            pending = tuple(slot for slot in start.pending if slot not in resolved)
            start = replace(start, runs=carried, pending=pending)
    frozen = freeze_goals((goal,), {goal.spec.key: start}, plans, request, strict=True)
    return frozen[goal.spec.key]
