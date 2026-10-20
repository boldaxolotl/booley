"""What Goal status says about a Goal's frozen Test Runs (ADR 0072).

:func:`judge_test_runs` is the read side of
:mod:`booley.goals.test_run_evidence`. For one Goal that is judged by Test
Runs it decides, from the Goal Record, the selected observation and the
ledger rows, whether the frozen runs let the Goal be met, and names the
reason when they do not:

- a slot still pending, or no Test Run required at all;
- a frozen run whose Test or Test Variant is gone from the Target, or whose
  declared arguments or environment changed: only an approved retarget
  freezes the Goal again;
- a required run that failed, or has not passed yet. Not for a coverage
  Goal: it is judged by its coverage floors alone
  (:func:`~booley.goals.test_run_evidence.outcomes_decide`).

It also gives each frozen run its outcome and the command that runs it
again, which status, the review package and the Dashboard show. A coverage
Goal's failing runs are shown the same way while the Goal is met.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from booley.flows.frozen_test_runs import current_declaration
from booley.flows.test_runs import TestRun, rerun_command
from booley.goals.model import GoalRecord, RecordedGoal
from booley.goals.test_run_evidence import (
    REASON_CHANGED,
    REASON_MISSING,
    REASON_UNRESOLVABLE,
    HistoryScope,
    outcome_defect,
    outcomes_decide,
    requirement_defect,
    run_outcomes,
)
from booley.goals.test_run_freezing import (
    TargetPlanError,
    TargetPlans,
    resolution_obstacle,
    unresolved,
)
from booley.goals.test_run_resolution import TestRunResolution


@dataclass(frozen=True)
class TestRunView:
    """One frozen Test Run of a Goal: its outcome and how to run it again."""

    __test__ = False

    name: str
    outcome: str
    rerun: str
    deferred: bool = False


@dataclass(frozen=True)
class TestRunJudgement:
    """Whether a Goal's frozen Test Runs let it be met, and what to show of them."""

    __test__ = False

    #: The named reason the Goal is unmet (``goal-test-run-…``); empty when its
    #: frozen runs allow it, and when nothing was recorded yet.
    code: str = ""
    #: What that reason says, for a person.
    text: str = ""
    runs: tuple[TestRunView, ...] = ()
    pending: tuple[str, ...] = ()
    #: The frozen runs cannot be judged at all (pending, none, gone or
    #: changed); no rerun of the Flow can meet the Goal as it is.
    blocking: bool = False

    @property
    def reason(self) -> str:
        """``code: text`` as status shows it; empty when the frozen runs allow the Goal."""
        return f"{self.code}: {self.text}" if self.code else self.text

    @property
    def failed(self) -> tuple[TestRunView, ...]:
        """The frozen runs that failed at the current file contents."""
        return tuple(run for run in self.runs if run.outcome == "failed")


def judge_test_runs(
    goal: RecordedGoal,
    record: GoalRecord,
    evidence: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]],
    plans: TargetPlans,
) -> TestRunJudgement:
    """Judge *goal*'s frozen Test Runs against its selected observation *evidence*.

    *history* is every validated observation of the record. The reason is
    empty only when the Goal's resolution is complete and non-empty, every
    frozen run still is what the Target declares, every applicable run passed. Whether the runs passed is left
    out for a Goal their outcomes do not decide (a coverage Goal).
    """
    key, target = goal.spec.key, goal.spec.target
    assert target is not None
    resolution = record.test_run_resolution(key)
    required = () if resolution is None else resolution.test_runs
    runs = (
        []
        if evidence is None
        else run_outcomes(
            HistoryScope.of(record, goal),
            required,
            evidence["detail"],
            history,
            before=evidence["sequence"],
        )
    )
    outcomes = {(run["name"], run["fingerprint"]): run["outcome"] for run in runs}
    defect = (
        _pending_defect(goal, resolution, plans)
        or requirement_defect(resolution)
        or _declaration_defect(required, target, plans)
    )
    blocking = defect is not None
    if defect is None and evidence is not None and outcomes_decide(goal):
        defect = outcome_defect(runs)
    code, text = defect or ("", "")
    if defect is None and evidence is None:
        text = "no Test Run of the Goal has been recorded"
    return TestRunJudgement(
        code,
        text,
        _views(resolution, target, outcomes),
        () if resolution is None else resolution.pending,
        blocking,
    )


def _pending_defect(
    goal: RecordedGoal, resolution: TestRunResolution | None, plans: TargetPlans
) -> tuple[str, str] | None:
    """Why pending slots cannot be resolved, when it is not that they do not exist yet.

    A ``tests.toml`` that cannot be applied, or a seed on a Target that
    cannot apply seeds, is its own reason: waiting will not resolve it.
    """
    held = resolution or unresolved(goal.spec, goal.spec_revision)
    if not held.pending:
        return None
    obstacle = resolution_obstacle(goal.spec, held, plans)
    return (REASON_UNRESOLVABLE, obstacle) if obstacle else None


def _views(
    resolution: TestRunResolution | None, target: str, outcomes: Mapping[tuple[str, str], str]
) -> tuple[TestRunView, ...]:
    if resolution is None:
        return ()
    return tuple(
        TestRunView(
            item.run.name,
            outcomes.get((item.run.name, item.run.fingerprint), "missing"),
            rerun_command(target, item.run),
            item.deferred,
        )
        for item in resolution.runs
    )


def _declaration_defect(
    required: Sequence[TestRun], target: str, plans: TargetPlans
) -> tuple[str, str] | None:
    """Why a frozen run no longer is what the Target declares: (reason, text)."""
    try:
        plan = plans.plan(target)
    except TargetPlanError as exc:
        return REASON_MISSING, str(exc)
    gone = [run.name for run in required if current_declaration(plan, run) is None]
    if gone:
        return REASON_MISSING, (
            f"Test Runs {', '.join(gone)} no longer exist on Target {target!r}; "
            "the Goal stays unmet until a human approves a retarget"
        )
    changed = [run.name for run in required if current_declaration(plan, run) != run]
    if changed:
        return REASON_CHANGED, (
            f"the declared arguments or environment of {', '.join(changed)} changed since "
            "the Goal was frozen; it stays unmet until a human approves a retarget"
        )
    return None
