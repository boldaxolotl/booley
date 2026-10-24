"""Freeze the Test Runs of Goals from their Targets' Test Run plans (ADR 0072).

Three things that change together, in the order a reader meets them:

**The plans** (:class:`TargetPlans`). Goal Mode freezes and later checks a
Goal's Test Runs against the plan the Simulation Flow would build for the
Goal's Target. Each Target's plan is loaded at most once; a Target that does
not exist yet (the Goal waits for it, ADR 0067) is told apart from one whose
plan cannot be built (an error the caller reports). The same catalog gives
the identity of a Target however it is written.

**The rule** (:func:`resolve_pending`, :func:`freeze_goals`,
:func:`refreeze_changed_goal`), pure given the plans and a seed source. It
fills the slots of a Goal's
:class:`~booley.goals.test_run_resolution.TestRunResolution` that can be
filled now and leaves every frozen run alone; entry, deferred resolution and
an approved Goal change all use it:

- a selector slot becomes the Test Run it names: its Test Variant (the
  default one when none is written) at the written seed, or at the Target's
  Default Seed; ``@xN`` becomes N runs with distinct drawn seeds;
- the suite slot becomes the default Test Variant of every Test of the
  Target at the Default Seed, and later gains the Tests added to the Target;
- a slot whose Test or Test Variant does not exist yet stays pending;
- a slot whose Test the Target skips is refused: a skipped Test's runs never count;
- two multi-seed slots with the same Target, Test Variant and N share one
  set of seeds, whichever Goals name them, so ``--test test+variant@xN``
  names one set of Test Runs.

An approved change keeps what it does not touch: a slot the Goal still names
keeps its frozen runs, and a changed N keeps the first N of them or draws
only the difference, for the frozen Test Variant with its frozen declared
inputs.

**Deferred resolution** (:func:`ensure_test_run_resolutions`). A Goal may
name a Target or a Test that does not exist yet, a Test may be added later to
the Target of a Target Goal, and a Goal Record an older Booley created has no
frozen Test Runs at all. Each of those slots is pending until it is filled,
when a run is bound and when Goal status is read. A resolution is **one
atomic save** of ``record.json`` under the record lock. The saved record
already holds the exact resolved Test Runs and drawn seeds, so a crash leaves
either the old record (nothing was observable; the next call draws again) or
the new one. The Change Log is not involved: its entries are approved Goal
changes, which recovery and the apply barrier act on. While an apply is
interrupted nothing is resolved, so a resolution never saves over one. A
resolver whose record another writer replaced re-reads it and tries again;
what the other resolver froze stays frozen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from booley.config.tests_toml_rules import UnknownVariantError
from booley.flows import target_test_plan_loading
from booley.flows.target_test_plan import TargetTestPlan, UnknownTestError
from booley.flows.test_runs import (
    PinnedSeed,
    SeedCount,
    TestRun,
    TestSelector,
    UnpinnedSeed,
    UnseededTargetError,
    draw_seeds,
    parse_test_selector,
)
from booley.goals.apply_barrier import pending_applies
from booley.goals.model import GoalRecord, GoalSpec, GoalState, RecordedGoal
from booley.goals.paths import record_paths
from booley.goals.presentation import goal_presentation
from booley.goals.store import GoalStore, StaleRevisionError
from booley.goals.test_run_goals import (
    CanonicalNames,
    goal_selectors,
    names_test_runs,
    selector_text,
    variant_text,
)
from booley.goals.test_run_resolution import (
    DEFERRED,
    SUITE_SLOT,
    FrozenRun,
    ResolvedBy,
    TestRunResolution,
)
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.domain import FuseSocError, UnknownTargetError

# ---------------------------------------------------------------------------
# The Test Run plans of one checkout
# ---------------------------------------------------------------------------

PlanLoader = Callable[[Path, str], TargetTestPlan]


class TargetPlanError(ValueError):
    """A Target exists but its Test Run plan cannot be built."""


def load_plan(work_dir: Path, target: str) -> TargetTestPlan:
    """The default :data:`PlanLoader`: the plan the Simulation Flow would build."""
    return target_test_plan_loading.load_checkout_target_test_plan(work_dir, target)


def load_identity(work_dir: Path, target: str) -> str:
    """The default Target identity: the one the Target catalog of the checkout gives."""
    return target_test_plan_loading.checkout_target_identity(work_dir, target)


@dataclass
class TargetPlans:
    """The Test Run plans of the checkout at ``work_dir``, each loaded once."""

    work_dir: Path
    loader: PlanLoader = load_plan
    identities: Callable[[Path, str], str] = load_identity
    _identities: dict[str, str] = field(default_factory=dict[str, str])
    _plans: dict[str, TargetTestPlan | None] = field(
        default_factory=dict[str, TargetTestPlan | None]
    )

    def plan(self, target: str) -> TargetTestPlan | None:
        """*target*'s plan, or ``None`` while the Target does not exist.

        Raises :class:`TargetPlanError` when the Target exists but cannot be
        inspected, or its ``tests.toml`` section cannot be applied to it.
        """
        if target not in self._plans:
            try:
                self._plans[target] = self.loader(self.work_dir, target)
            except UnknownTargetError:
                self._plans[target] = None
            except (FuseSocError, ValueError, OSError) as exc:
                raise TargetPlanError(
                    f"the Test Runs of Target {target!r} cannot be resolved: {exc}"
                ) from exc
        return self._plans[target]

    def identity(self, target: str) -> str:
        """The identity of *target* however it is written; itself while it does not exist.

        Raises :class:`TargetPlanError` when the Target catalog cannot be read.
        """
        if target not in self._identities:
            try:
                self._identities[target] = self.identities(self.work_dir, target)
            except UnknownTargetError:
                self._identities[target] = target
            except (FuseSocError, ValueError, OSError) as exc:
                raise TargetPlanError(f"Target {target!r} cannot be identified: {exc}") from exc
        return self._identities[target]

    def resolver(self) -> CanonicalNames:
        """Canonical names for :func:`booley.goals.translate.translate_goals`."""

        def resolve(target: str, selector: TestSelector) -> TestSelector | None:
            plan = self.plan(target)
            if plan is None:
                return None
            try:
                base = plan.run(selector.test, selector.variant)
            except (UnknownTestError, UnknownVariantError):
                return None
            form = selector.seed_form
            if isinstance(form, UnpinnedSeed) and base.seed is not None:
                form = PinnedSeed(base.seed)
            return TestSelector(selector.test, base.variant, form)

        return CanonicalNames(resolve, self.identity)


# ---------------------------------------------------------------------------
# The freezing rule
# ---------------------------------------------------------------------------

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
    for a selector the Target refuses for another reason than a missing Test,
    a skipped Test included.
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
    _refuse_skipped(plan, selector.test)
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


def _refuse_skipped(plan: TargetTestPlan, test: str | None) -> None:
    """Refuse to freeze a run of *test* while tests.toml skips it: its runs never count."""
    if test is not None and test in plan.skipped:
        raise GoalResolutionError(
            f"Test {test!r} of Target {plan.target!r} is skipped: tests.toml lists it "
            "in the Target's skip. Remove it from skip, or drop the Goal"
        )


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
                raise GoalResolutionError(
                    f"Goal {goal_presentation(goal.spec).text}: {exc}"
                ) from exc
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
    or applied, a seed on a Target that cannot apply seeds, or a Test the Target skips.
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
    except (TargetPlanError, GoalResolutionError) as exc:
        raise GoalResolutionError(f"Goal {goal_presentation(goal.spec).text}: {exc}") from exc
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
    _refuse_skipped(plan, first.test)
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


# ---------------------------------------------------------------------------
# Deferred resolution
# ---------------------------------------------------------------------------

# A resolver loses a save only to another writer of the same record; each
# retry re-reads what that writer saved, so a few attempts always suffice.
_SAVE_ATTEMPTS = 3


def ensure_test_run_resolutions(
    store: GoalStore,
    record_id: str,
    work_dir: Path,
    *,
    draw: SeedDraw | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> GoalRecord:
    """Fill every pending Test Run slot of Goal Record *record_id* that can be filled now.

    *work_dir* is the Goal worktree whose ``tests.toml`` and Targets the
    slots resolve against. Returns the record as it is afterwards; a record
    that is not ``active``, or whose apply is interrupted, is returned
    unchanged. A Goal its Target refuses stays pending. *checkpoint* is
    called with ``"before:save"`` and ``"after:save"`` (tests interrupt
    there).

    Raises :class:`~booley.goals.store.GoalStoreError` when the record cannot
    be read or saved.
    """
    for _attempt in range(_SAVE_ATTEMPTS):
        with store.record_lock(record_id) as lock:
            record = store.load(record_id)
            resolved = _resolved(store, record, work_dir, draw)
            if resolved is None:
                return record
            if checkpoint is not None:
                checkpoint("before:save")
            try:
                saved = store.save(lock, resolved)
            except StaleRevisionError:
                continue
            if checkpoint is not None:
                checkpoint("after:save")
            return saved
    raise StaleRevisionError(
        f"Goal Record {record_id} kept changing while its Test Runs were resolved"
    )


def _resolved(
    store: GoalStore, record: GoalRecord, work_dir: Path, draw: SeedDraw | None
) -> GoalRecord | None:
    """*record* with its pending slots filled, or ``None`` when nothing may or need change."""
    if record.state is not GoalState.ACTIVE:
        return None
    if pending_applies(record_paths(store.booley_dir, record.id).root):
        return None
    # Looked up here, not bound as a default: a caller without its own source
    # (a run being bound, a status read) draws from this module's.
    request = ResolutionRequest(DEFERRED, utc_now_rfc3339(), draw or draw_seeds)
    frozen = freeze_goals(
        record.goals, record.test_run_resolutions, TargetPlans(work_dir), request, strict=False
    )
    if frozen == dict(record.test_run_resolutions):
        return None
    return replace(record, test_run_resolutions=frozen)
