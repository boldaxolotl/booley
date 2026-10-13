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
- a slot whose Test or Test Variant does not exist yet stays pending;
- two multi-seed slots with the same Target, Test Variant and N share one
  set of seeds, whichever Goals name them, so ``--test test+variant@xN``
  names one set of Test Runs.

An approved change (:func:`refreeze_changed_goal`) keeps what the change does
not touch: a slot the Goal still names keeps its frozen runs, and a changed N
keeps the first N of them or draws only the difference, for the frozen Test
Variant with its frozen declared inputs.

Pure: the caller supplies the plans and the seed source.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from booley.config.tests_toml_rules import UnknownVariantError
from booley.flows.target_test_plan import TargetTestPlan, UnknownTestError
from booley.flows.test_runs import (
    PinnedSeed,
    SeedCount,
    TestRun,
    TestSelector,
    UnseededTargetError,
    draw_seeds,
    parse_test_selector,
)
from booley.goals.model import GoalSpec, RecordedGoal
from booley.goals.test_run_goals import (
    goal_selectors,
    names_test_runs,
    selector_text,
    variant_text,
)
from booley.goals.test_run_plans import TargetPlanError, TargetPlans
from booley.goals.test_run_resolution import (
    DEFERRED,
    SUITE_SLOT,
    FrozenRun,
    ResolvedBy,
    TestRunResolution,
)

SeedDraw = Callable[..., tuple[int, ...]]
#: The frozen seeds of multi-seed slots, by (Test, Test Variant, N).
SeedGroups = Mapping[tuple[str, str | None, int], tuple[int, ...]]

logger = logging.getLogger(__name__)


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

    ``shared`` holds the seeds other Goals of the same Target already froze
    for a multi-seed slot; a slot with the same Test Variant and N takes
    them instead of drawing its own.
    """

    resolved_by: ResolvedBy
    resolved_at: str
    draw: SeedDraw = draw_seeds
    shared: SeedGroups = field(default_factory=dict[tuple[str, str | None, int], tuple[int, ...]])


def resolve_pending(
    goal: GoalSpec,
    current: TestRunResolution,
    plan: TargetTestPlan | None,
    request: ResolutionRequest,
) -> TestRunResolution:
    """*current* with every slot filled that *plan* can fill now.

    *plan* is the Target's Test Run plan, or ``None`` while the Target does
    not exist; then nothing changes. A frozen run is never touched, with one
    exception: the unnamed run a whole-Target Goal froze while its Target
    registered no Test is superseded by the Tests the Target registers now.
    Raises :class:`~booley.flows.test_runs.UnseededTargetError` for a seed
    the Target cannot apply, and ``ValueError`` (the Test Run error types)
    for a selector the Target refuses for another reason than a missing Test.
    """
    if plan is None:
        return current
    runs = list(current.runs)
    pending: list[str] = []
    slots = goal_slots(goal)
    for slot in current.pending:
        found = _slot_runs(plan, slot, slots, [item.run for item in runs], request)
        if found is None:
            pending.append(slot)
            continue
        runs.extend(_stamp(found, slot, runs, request))
    if SUITE_SLOT in slots and SUITE_SLOT not in current.pending:
        runs = _grown_suite(plan, runs, request)
    return replace(current, runs=tuple(runs), pending=tuple(pending))


def _grown_suite(
    plan: TargetTestPlan, runs: list[FrozenRun], request: ResolutionRequest
) -> list[FrozenRun]:
    """*runs* with the Tests added to the Target since the suite slot was frozen."""
    added = _stamp(_added_tests(plan, runs), SUITE_SLOT, runs, request)
    if not plan.tests:
        return runs
    # The Target registers Tests now: its unnamed invocation can no longer run.
    return [item for item in runs if item.slot != SUITE_SLOT or item.run.test is not None] + added


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
    plan: TargetTestPlan,
    slot: str,
    slots: Sequence[str],
    frozen: Sequence[TestRun],
    request: ResolutionRequest,
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
    taken = {*_seeds_of(frozen, base), *_pinned_seeds(plan, slots, base)}
    seeds = request.shared.get((selector.test, base.variant, form.count), ())
    if not seeds or taken & set(seeds):
        seeds = request.draw(form.count, exclude=taken)
    return [plan.run(selector.test, selector.variant, seed) for seed in seeds]


def _pinned_seeds(plan: TargetTestPlan, slots: Sequence[str], base: TestRun) -> set[int]:
    """Seeds the Goal pins for *base*'s Test Variant in another slot: never drawn again."""
    pinned: set[int] = set()
    for slot in slots:
        selector = None if slot == SUITE_SLOT else parse_test_selector(slot)
        if selector is None or not isinstance(selector.seed_form, PinnedSeed):
            continue
        if selector.test == base.test and _variant_of(plan, selector) == base.variant:
            pinned.add(selector.seed_form.seed)
    return pinned


def _variant_of(plan: TargetTestPlan, selector: TestSelector) -> str | None:
    """The Test Variant *selector* names on *plan*; its written one while it does not exist."""
    try:
        return plan.run(selector.test, selector.variant).variant
    except (UnknownTestError, UnknownVariantError):
        return selector.variant


def _seeds_of(runs: Iterable[TestRun], base: TestRun) -> list[int]:
    return [
        run.seed
        for run in runs
        if run.test == base.test and run.variant == base.variant and run.seed is not None
    ]


def seed_groups(
    resolution: TestRunResolution,
) -> dict[tuple[str, str | None, int], tuple[int, ...]]:
    """The frozen seeds of each multi-seed slot of *resolution*, by (Test, Variant, N)."""
    groups: dict[tuple[str, str | None, int], tuple[int, ...]] = {}
    for slot in dict.fromkeys(item.slot for item in resolution.runs):
        if slot == SUITE_SLOT or not isinstance(parse_test_selector(slot).seed_form, SeedCount):
            continue
        runs = [item.run for item in resolution.runs if item.slot == slot]
        seeds = tuple(run.seed for run in runs if run.seed is not None)
        if runs[0].test is not None:
            groups[(runs[0].test, runs[0].variant, len(seeds))] = seeds
    return groups


class GoalResolutionError(ValueError):
    """A Goal's Test Runs cannot be frozen on its Target as the Goal is written."""


def freeze_goals(
    goals: Iterable[RecordedGoal],
    existing: Mapping[str, TestRunResolution],
    plans: TargetPlans,
    request: ResolutionRequest,
    *,
    strict: bool,
    only: Collection[str] | None = None,
) -> dict[str, TestRunResolution]:
    """The resolutions of *goals* with every slot filled that can be filled now.

    A Goal keeps the resolution *existing* holds at its current revision and
    otherwise starts unresolved. Goals of one Target share the seeds of
    equal multi-seed slots. With *only*, just the Goals with those keys are
    resolved; the others still lend their seeds. With *strict* (entry and an
    approved change) a Goal its Target refuses raises
    :class:`GoalResolutionError`, naming the Goal; without it (deferred
    resolution) that Goal is left as it was and the refusal is logged:
    :func:`resolution_obstacle` names it for status.
    """
    frozen: dict[str, TestRunResolution] = {}
    shared: dict[str, dict[tuple[str, str | None, int], tuple[int, ...]]] = {}
    for goal in goals:
        if not names_test_runs(goal.spec):
            continue
        assert goal.spec.target is not None
        key = goal.spec.key
        held = existing.get(key)
        if held is None or held.spec_revision != goal.spec_revision:
            held = unresolved(goal.spec, goal.spec_revision)
        frozen[key] = held
        try:
            groups = shared.setdefault(plans.identity(goal.spec.target), {})
            if only is None or key in only:
                plan = plans.plan(goal.spec.target)
                frozen[key] = resolve_pending(
                    goal.spec, held, plan, replace(request, shared=groups)
                )
        except ValueError as exc:  # TargetPlanError and the Test Run error types
            if strict:
                raise GoalResolutionError(f"Goal {key}: {exc}") from exc
            logger.warning(
                "Test Runs of Goal %s on Target %s stay pending: %s", key, goal.spec.target, exc
            )
            continue
        for group, seeds in seed_groups(frozen[key]).items():
            groups.setdefault(group, seeds)
    return frozen


def resolution_obstacle(goal: GoalSpec, held: TestRunResolution, plans: TargetPlans) -> str:
    """Why *held*'s pending slots cannot be resolved, when it is not that they do not exist yet.

    Empty while the Target, Test or Test Variant is simply not there. Says
    what the Target refuses otherwise: a ``tests.toml`` that cannot be read
    or applied, or a seed on a Target that cannot apply seeds.
    """
    assert goal.target is not None
    try:
        resolve_pending(goal, held, plans.plan(goal.target), ResolutionRequest(DEFERRED, ""))
    except ValueError as exc:  # TargetPlanError and the Test Run error types
        return str(exc)
    return ""


def refreeze_changed_goal(
    goal: RecordedGoal,
    before: tuple[GoalSpec, TestRunResolution] | None,
    plans: TargetPlans,
    request: ResolutionRequest,
    *,
    refreeze: bool,
    siblings: Mapping[str, tuple[RecordedGoal, TestRunResolution]] | None = None,
) -> TestRunResolution:
    """The resolution of a Goal an approved change just gave a new revision.

    *before* is the Goal as it was and its resolution, when it had one. What
    the change leaves alone stays frozen, byte for byte: a slot the Goal
    still names on the same Target keeps its runs, a multi-seed slot whose N
    was lowered keeps its first N runs, and one whose N was raised keeps all
    of them and draws only the difference, for the frozen Test Variant with
    its frozen declared inputs. Every other slot is resolved from the
    Target's plan as it is now. With *refreeze* (a retarget that leaves the
    Goal as written) nothing is kept: the Goal is frozen again.

    *siblings* are the record's other Goals with their resolutions; equal
    multi-seed slots share their seeds. Raises :class:`GoalResolutionError`,
    also when a raised N can no longer draw for the frozen Test Variant.
    """
    start = unresolved(goal.spec, goal.spec_revision)
    key, target = goal.spec.key, goal.spec.target
    assert target is not None
    try:
        if before is not None and not refreeze and before[0].target == target:
            start = _carried(start, before, plans.plan(target), request)
    except TargetPlanError as exc:
        raise GoalResolutionError(f"Goal {key}: {exc}") from exc
    others = dict(siblings or {})
    others.pop(key, None)
    existing = {**{name: held for name, (_, held) in others.items()}, key: start}
    recorded = [*(item for item, _ in others.values()), goal]
    return freeze_goals(recorded, existing, plans, request, strict=True, only={key})[key]


def _carried(
    start: TestRunResolution,
    before: tuple[GoalSpec, TestRunResolution],
    plan: TargetTestPlan | None,
    request: ResolutionRequest,
) -> TestRunResolution:
    """*start* with the frozen runs of *before* that the changed Goal keeps."""
    old_goal, old = before
    resolved = [slot for slot in goal_slots(old_goal) if slot not in old.pending]
    runs: list[FrozenRun] = []
    pending: list[str] = []
    for slot in start.pending:
        source = slot if slot in resolved else _resized_slot(slot, resolved)
        kept = [item for item in old.runs if item.slot == source]
        if source is None or (source != slot and not kept):
            pending.append(slot)
            continue
        count = _count(slot) if source != slot else len(kept)
        runs.extend(replace(item, slot=slot) for item in kept[:count])
        if count > len(kept):
            held = [item.run for item in (*old.runs, *runs)]
            runs.extend(_more_seeds(plan, kept, count, (slot, start.pending), held, request))
    return replace(start, runs=tuple(runs), pending=tuple(pending))


def _count(slot: str) -> int:
    form = parse_test_selector(slot).seed_form
    assert isinstance(form, SeedCount)
    return form.count


def _resized_slot(slot: str, resolved: Sequence[str]) -> str | None:
    """The multi-seed slot among *resolved* that *slot* is with another N, if any."""
    if slot == SUITE_SLOT:
        return None
    selector = parse_test_selector(slot)
    if not isinstance(selector.seed_form, SeedCount):
        return None
    for other in resolved:
        candidate = None if other == SUITE_SLOT else parse_test_selector(other)
        if (
            candidate is not None
            and isinstance(candidate.seed_form, SeedCount)
            and variant_text(candidate) == variant_text(selector)
        ):
            return other
    return None


def _more_seeds(
    plan: TargetTestPlan | None,
    kept: Sequence[FrozenRun],
    count: int,
    slot: tuple[str, Sequence[str]],
    held: Sequence[TestRun],
    request: ResolutionRequest,
) -> list[FrozenRun]:
    """The runs a raised N adds to *kept*: new seeds of the same frozen Test Variant.

    Refused when the Target no longer declares that Test Variant with the
    declared inputs the kept runs were frozen with: the added runs would be
    runs of something else.
    """
    first = kept[0].run
    name, slots = slot
    again = "approve a retarget that leaves the Goal as written to freeze its Test Runs again"
    try:
        current = None if plan is None else plan.run(first.test, first.variant, first.seed)
    except (UnknownTestError, UnknownVariantError, UnseededTargetError):
        current = None
    if plan is None or current is None:
        raise GoalResolutionError(
            f"the seed count of {name} cannot be raised: its frozen Test Run {first.name} no "
            f"longer exists on the Target; {again}"
        )
    if current != first:
        raise GoalResolutionError(
            f"the seed count of {name} cannot be raised: the declared arguments or environment "
            f"of its frozen Test Run {first.name} changed; {again}"
        )
    taken = {*_seeds_of(held, first), *_pinned_seeds(plan, slots, first)}
    drawn = request.draw(count - len(kept), exclude=taken)
    return [
        FrozenRun(
            plan.run(first.test, first.variant, seed),
            name,
            request.resolved_by,
            request.resolved_at,
        )
        for seed in drawn
    ]
