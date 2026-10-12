"""The Test Runs the Goals of one run are frozen to, as a Flow reads them (ADR 0072).

Inside Goal Mode a Goal requires exact Test Runs: the ones it was frozen to
when it was resolved. The Goal execution adapter hands the Simulation Flow
those runs as plain values (:class:`FrozenTestRuns`), so the Flow can

- run a Target Goal's frozen Test Runs when a Target is run without
  ``--test``, with the current default of every Test not frozen yet;
- run the frozen seeds of a multi-seed Goal when ``--test test+variant@xN``
  names its Target, Test Variant and N; and
- decide which Goals a campaign's Test Runs are evidence for: a Test Run
  matches a Goal only by its resolved name and the fingerprint of its
  declared arguments and environment. Every other Test Run is diagnostic.

Flow-neutral values and rules only; nothing here knows the Goal Record.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from booley.config.tests_toml_rules import UnknownVariantError
from booley.flows.target_test_plan import TargetTestPlan, UnknownTestError
from booley.flows.test_runs import (
    SEED_SEPARATOR,
    SeedCount,
    TestRun,
    UnseededTargetError,
    parse_test_selector,
)

__all__ = [
    "FrozenTestRuns",
    "GoalTestRuns",
    "SeedGroup",
    "resolve_goal_test_runs",
]

GoalRunsKind = Literal["simulation", "cycle_count", "coverage"]


@dataclass(frozen=True)
class SeedGroup:
    """The frozen seeds of one multi-seed selector (``test[+variant]@xN``)."""

    test: str
    variant: str | None
    seeds: tuple[int, ...]


@dataclass(frozen=True)
class GoalTestRuns:
    """The frozen Test Runs one Goal requires of its Target.

    ``suite`` marks a Goal over every Test of the Target. ``complete`` is
    false while a slot of the Goal is still pending: its runs so far are
    known, but no evidence can meet it yet.
    """

    key: str
    kind: GoalRunsKind
    target: str
    runs: tuple[TestRun, ...]
    suite: bool = False
    complete: bool = True
    seed_groups: tuple[SeedGroup, ...] = ()

    def requires(self, name: object, fingerprint: object) -> bool:
        """Whether a Test Run with this resolved name and fingerprint is one of ``runs``."""
        return any(run.name == name and run.fingerprint == fingerprint for run in self.runs)

    def matching(self, rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        """The Test Run rows of *rows* this Goal requires, in their order."""
        return [
            dict(row) for row in rows if self.requires(row.get("name"), row.get("fingerprint"))
        ]

    def passed_by(self, matched: Sequence[Mapping[str, object]]) -> bool:
        """Whether *matched* rows alone show every required Test Run passing.

        One invocation's own verdict. The Goal's recorder decides the Goal
        from every matching run recorded so far, which may pass runs this
        invocation did not run and holds a failure an earlier one recorded.
        """
        passed = {row.get("name") for row in matched if row.get("grade") == "pass"}
        return self.complete and bool(self.runs) and all(run.name in passed for run in self.runs)


@dataclass(frozen=True)
class FrozenTestRuns:
    """Every Goal of one run that is judged by Test Runs."""

    goals: tuple[GoalTestRuns, ...] = ()

    def of_target(self, names: Collection[str]) -> tuple[GoalTestRuns, ...]:
        """The Goals bound to the Target known by any of *names* (identity, selector)."""
        return tuple(goal for goal in self.goals if goal.target in names)

    def suite_runs(self, names: Collection[str]) -> tuple[TestRun, ...] | None:
        """The frozen runs of the Target's whole-suite Goals; ``None`` without such a Goal."""
        suites = [goal for goal in self.of_target(names) if goal.suite]
        if not suites:
            return None
        return tuple(dict.fromkeys(run for goal in suites for run in goal.runs))

    def seeds(
        self, names: Collection[str], test: str, variant: str | None, count: int
    ) -> tuple[int, ...] | None:
        """The frozen seeds of the Goal with this Target, Test Variant and N, if any."""
        for goal in self.of_target(names):
            for group in goal.seed_groups:
                if (group.test, group.variant, len(group.seeds)) == (test, variant, count):
                    return group.seeds
        return None


def resolve_goal_test_runs(
    plan: TargetTestPlan,
    selectors: Sequence[str] | None,
    frozen: FrozenTestRuns,
    names: Collection[str],
) -> tuple[tuple[TestRun, ...], tuple[str, ...]]:
    """The Test Runs a request without ``--seed`` runs on *plan*'s Target inside Goal Mode.

    Returns the runs and the multi-seed selectors that matched no Goal: their
    seeds are drawn now and their Test Runs are diagnostic. Every other
    selector resolves from the current ``tests.toml``, as it does outside
    Goal Mode. Raises what :meth:`TargetTestPlan.resolve` raises.
    """
    if selectors is None:
        suite = frozen.suite_runs(names)
        runs = _frozen_suite(plan, suite) if suite is not None else ()
        return (runs or plan.resolve(None)), ()
    pinned: list[str] = []
    fresh: list[str] = []
    for text in selectors:
        seeds = _frozen_seeds(plan, text, frozen, names)
        if seeds is None:
            if isinstance(parse_test_selector(text).seed_form, SeedCount):
                fresh.append(text)
            pinned.append(text)
            continue
        head = text.split(SEED_SEPARATOR)[0]
        pinned.extend(f"{head}{SEED_SEPARATOR}{seed}" for seed in seeds)
    return plan.resolve(pinned), tuple(fresh)


def _frozen_seeds(
    plan: TargetTestPlan, text: str, frozen: FrozenTestRuns, names: Collection[str]
) -> tuple[int, ...] | None:
    """The frozen seeds a ``@xN`` selector runs; ``None`` for any other selector."""
    selector = parse_test_selector(text)
    if not isinstance(selector.seed_form, SeedCount):
        return None
    try:
        base = plan.run(selector.test, selector.variant)
    except (UnknownTestError, UnknownVariantError):
        return None  # ``plan.resolve`` reports the unknown name
    return frozen.seeds(names, selector.test, base.variant, selector.seed_form.count)


def _frozen_suite(plan: TargetTestPlan, frozen: Sequence[TestRun]) -> tuple[TestRun, ...]:
    """The frozen runs the Target can still run, then the default of every other Test.

    A frozen run is rebuilt from the current declaration at its frozen Test
    Variant and seed: if the declaration changed, the run that executes has
    another fingerprint and matches no Goal. A frozen run whose Test or Test
    Variant is gone, or is skipped now, does not run.
    """
    runs: list[TestRun] = []
    for run in frozen:
        if run.test in plan.skipped:
            continue
        try:
            runs.append(plan.run(run.test, run.variant, run.seed))
        except (UnknownTestError, UnknownVariantError, UnseededTargetError):
            continue
    known = {run.test for run in frozen}
    runs.extend(run for run in plan.default_runs() if run.test not in known)
    return tuple(dict.fromkeys(runs))
