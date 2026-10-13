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

from collections.abc import Collection, Iterable, Mapping, Sequence
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
    "OUTCOMES_ONLY_DETAIL_KEY",
    "FrozenTestRuns",
    "GoalTestRuns",
    "SeedGroup",
    "current_declaration",
    "fold_test_run_outcomes",
    "matching_test_run_rows",
    "resolve_goal_test_runs",
]

GoalRunsKind = Literal["simulation", "cycle_count", "coverage"]

#: Detail key of a change that carries a Goal's matching Test Runs but no
#: measurement of the Goal's metric (its Cycle Count or coverage): the change
#: says nothing about the metric. The runs' outcomes count for a Cycle Count
#: Goal; a coverage Goal is judged by its coverage floors alone.
OUTCOMES_ONLY_DETAIL_KEY = "test_run_outcomes_only"


def matching_test_run_rows(
    rows: Iterable[Mapping[str, object]], runs: Sequence[TestRun]
) -> list[dict[str, object]]:
    """The rows of *rows* that are one of *runs*, in their order.

    The one matching rule: a row is evidence of a Test Run only by that
    run's resolved name and the fingerprint of its declared inputs.
    """
    wanted = {(run.name, run.fingerprint) for run in runs}
    return [dict(row) for row in rows if (row.get("name"), row.get("fingerprint")) in wanted]


def fold_test_run_outcomes(
    rows: Iterable[Mapping[str, object]], required: Sequence[TestRun]
) -> list[dict[str, str]]:
    """The outcome of every run of *required* after *rows*, in required order.

    The one verdict over frozen Test Runs. *rows* are Test Run rows
    (``name``, ``fingerprint``, ``grade``) in the order they were observed.
    A row counts only for the required run with its name and fingerprint.
    ``fail`` makes the run ``failed`` for good; ``pass`` makes a ``missing``
    run ``passed``; ``error`` and ``inconclusive`` observed nothing and
    change nothing.
    """
    outcomes = {(run.name, run.fingerprint): "missing" for run in required}
    for row in rows:
        run = (str(row.get("name")), str(row.get("fingerprint")))
        if run not in outcomes:
            continue
        grade = row.get("grade")
        if grade == "fail":
            outcomes[run] = "failed"
        elif grade == "pass" and outcomes[run] == "missing":
            outcomes[run] = "passed"
    return [
        {"name": name, "fingerprint": fingerprint, "outcome": outcome}
        for (name, fingerprint), outcome in outcomes.items()
    ]


def current_declaration(plan: TargetTestPlan | None, run: TestRun) -> TestRun | None:
    """A frozen *run* rebuilt from what its Target declares now; ``None`` if it cannot run.

    The run keeps its frozen Test Variant and seed. When the declaration
    changed, the rebuilt run has another fingerprint and is no longer the
    frozen one. A run whose Target, Test or Test Variant is gone, whose Test
    is skipped now, or whose seed the Target can no longer apply has none.
    """
    if plan is None or run.test in plan.skipped:
        return None
    try:
        return plan.run(run.test, run.variant, run.seed)
    except (UnknownTestError, UnknownVariantError, UnseededTargetError):
        return None


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
        return matching_test_run_rows(rows, self.runs)

    def passed_by(self, matched: Sequence[Mapping[str, object]]) -> bool:
        """Whether *matched* rows alone show every required Test Run passing.

        One invocation's own verdict on a simulation Goal. The Goal's
        recorder decides the Goal from every matching run recorded so far,
        which may pass runs this invocation did not run and holds a failure
        an earlier one recorded.
        """
        outcomes = fold_test_run_outcomes(matched, self.runs)
        passed = all(run["outcome"] == "passed" for run in outcomes)
        return self.complete and bool(self.runs) and passed

    def own_verdict(self, matched: Sequence[Mapping[str, object]]) -> bool:
        """What a change that carries only *matched* runs' outcomes claims for this Goal.

        A simulation Goal's runs are its whole verdict; a Cycle Count or
        coverage Goal needs its metric too, which such a change does not carry.
        """
        return self.kind == "simulation" and self.passed_by(matched)

    def outcomes_detail(self, matched: Sequence[Mapping[str, object]]) -> dict[str, object]:
        """The detail of a change that only enters *matched* runs' outcomes for this Goal."""
        rows = [dict(row) for row in matched]
        if self.kind == "simulation":
            return {"test_runs": rows}
        return {"test_runs": rows, OUTCOMES_ONLY_DETAIL_KEY: True}

    def measured_by(self, rows: Sequence[Mapping[str, object]]) -> bool:
        """Whether an invocation that ran exactly *rows* measured this Goal's metric.

        *rows* are all the Test Runs of one invocation. A simulation Goal has
        no metric beyond its runs' outcomes. A Cycle Count is the count of
        the Goal's one frozen run, so that run must be among *rows*. Coverage
        is merged over an invocation's runs, so *rows* must be exactly the
        frozen runs: fewer, more or other runs measured something else. The
        metric of any other invocation says nothing about the Goal. Every
        frozen run must have run to a pass or a fail; which of the two does
        not matter here.
        """
        if self.kind == "simulation":
            return True
        graded = [row for row in self.matching(rows) if row.get("grade") in {"pass", "fail"}]
        covered = {row.get("name") for row in graded} == {run.name for run in self.runs}
        if not (self.complete and self.runs and covered):
            return False
        return self.kind == "cycle_count" or len(rows) == len(graded) == len(self.runs)


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
    rebuilt = (current_declaration(plan, run) for run in frozen)
    runs = [run for run in rebuilt if run is not None]
    known = {run.test for run in frozen}
    runs.extend(run for run in plan.default_runs() if run.test not in known)
    return tuple(dict.fromkeys(runs))
