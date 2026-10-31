"""Match and fold a Goal's frozen Test Runs over applicable evidence history (ADR 0072).

Observations retain only their own matching runs. Simulation and Cycle Count
failures are sticky within one specification revision and dependent-contents
fingerprint. Coverage shows only its selected measurement's runs; its floors
alone decide whether the Goal is met.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from booley.criteria.evidence_ledger import canonical_json
from booley.criteria.state import CriterionChange
from booley.evidence.fields import (
    GOAL_TEST_RUN_NOT_MEASURED_REASON,
    GOAL_TEST_RUN_PENDING_REASON,
    SOURCE_FINGERPRINT_DETAIL_KEY,
)
from booley.flows.frozen_test_runs import (
    OUTCOMES_ONLY_DETAIL_KEY,
    FrozenTestRuns,
    GoalRunsKind,
    GoalTestRuns,
    SeedGroup,
    fold_test_run_outcomes,
    matching_test_run_rows,
)
from booley.flows.test_runs import TestRun
from booley.goals.freshness import goal_required_categories
from booley.goals.model import GoalFamily, GoalRecord, RecordedGoal
from booley.goals.test_run_freezing import goal_slots, seed_groups
from booley.goals.test_run_goals import names_test_runs
from booley.goals.test_run_resolution import SUITE_SLOT, TestRunResolution

# Named reasons a Goal that is judged by Test Runs is unmet.
REASON_PENDING = GOAL_TEST_RUN_PENDING_REASON
REASON_EMPTY = "goal-simulation-suite-empty"
REASON_FAILED = "goal-test-run-failed"
REASON_INCOMPLETE = "goal-test-run-incomplete"
REASON_MISSING = "goal-test-run-missing"
REASON_CHANGED = "goal-test-run-changed"
REASON_NOT_MEASURED = GOAL_TEST_RUN_NOT_MEASURED_REASON
REASON_UNRESOLVABLE = "goal-test-run-unresolvable"

# What to run so that one invocation measures a Goal's metric, by family.
_MEASURING_RUN: Mapping[GoalFamily, str] = {
    GoalFamily.CYCLE_COUNT: "run the Goal's frozen Test Run to measure its Cycle Count",
    GoalFamily.COVERAGE: (
        "run exactly the Goal's frozen Test Runs in one coverage campaign to measure coverage"
    ),
}

_KINDS: Mapping[GoalFamily, GoalRunsKind] = {
    GoalFamily.SIM: "simulation",
    GoalFamily.CYCLE_COUNT: "cycle_count",
    GoalFamily.COVERAGE: "coverage",
}


# ---------------------------------------------------------------------------
# What a Flow is handed
# ---------------------------------------------------------------------------


def flow_test_runs(record: GoalRecord, revisions: Mapping[str, int]) -> FrozenTestRuns:
    """The frozen Test Runs of *record*'s Goals a run admitted at *revisions* is judged by.

    A Goal whose specification revision moved since the run was admitted is
    left out: its evidence would be discarded anyway.
    """
    goals: list[GoalTestRuns] = []
    for goal in record.goals:
        spec = goal.spec
        if not names_test_runs(spec) or revisions.get(spec.key) != goal.spec_revision:
            continue
        goals.append(goal_test_runs(goal, record.test_run_resolution(spec.key)))
    return FrozenTestRuns(tuple(goals))


def goal_test_runs(goal: RecordedGoal, resolution: TestRunResolution | None) -> GoalTestRuns:
    """The frozen Test Runs of *goal* as a Flow reads them; nothing while unresolved."""
    spec = goal.spec
    assert spec.target is not None
    if resolution is None:
        return GoalTestRuns(spec.key, _KINDS[spec.family], spec.target, (), False, False)
    return GoalTestRuns(
        spec.key,
        _KINDS[spec.family],
        spec.target,
        resolution.test_runs,
        suite=SUITE_SLOT in goal_slots(spec),
        complete=not resolution.pending,
        seed_groups=tuple(
            SeedGroup(test, variant, seeds)
            for (test, variant, _count), seeds in seed_groups(resolution).items()
        ),
    )


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


def _rows(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Test Run rows *detail* carries: every entry that is a mapping."""
    raw = detail.get("test_runs")
    rows = cast("list[object]", raw) if isinstance(raw, list) else []
    return [dict(cast("Mapping[str, Any]", row)) for row in rows if isinstance(row, Mapping)]


def matching_rows(detail: Mapping[str, Any], required: Sequence[TestRun]) -> list[dict[str, Any]]:
    """The Test Run rows of *detail* that are one of *required*, well formed or not at all."""
    return matching_test_run_rows(_rows(detail), required)


def freshness_stamp(key: str, detail: Mapping[str, Any]) -> str | None:
    """What identifies the dependent file contents an observation was recorded at.

    The digests of the categories Goal *key* must stamp, ``target_surface``
    included. ``None`` when the observation does not stamp them all: such an
    observation accumulates with nothing.
    """
    fingerprint = _member(detail.get(SOURCE_FINGERPRINT_DETAIL_KEY), "fingerprint")
    digests: dict[str, str] = {}
    for category in sorted(goal_required_categories(key)):
        digest = _member(_member(fingerprint, category), "digest")
        if not isinstance(digest, str):
            return None
        digests[category] = digest
    return hashlib.sha256(canonical_json(digests)).hexdigest()


def outcomes_decide(goal: RecordedGoal) -> bool:
    """Whether the pass/fail outcomes of *goal*'s frozen Test Runs decide it.

    They do not for a coverage Goal: a Test Run that fails its checks is a
    simulation concern, and the Goal is met when a campaign of exactly its
    frozen runs reaches the coverage floors.
    """
    return goal.spec.family is not GoalFamily.COVERAGE


def _member(value: object, name: str) -> object:
    """``value[name]`` when *value* is a mapping, else ``None``."""
    return cast("Mapping[str, object]", value).get(name) if isinstance(value, Mapping) else None


@dataclass(frozen=True)
class HistoryScope:
    """Which observations accumulate with one another: one Goal at one revision.

    ``accumulates`` is false for a Goal its runs' outcomes do not decide
    (:func:`outcomes_decide`): each observation folds one invocation only.
    """

    record_id: str
    key: str
    spec_revision: int
    accumulates: bool = True

    @classmethod
    def of(cls, record: GoalRecord, goal: RecordedGoal) -> HistoryScope:
        """The scope of *goal* in *record* at its current specification revision."""
        return cls(record.id, goal.spec.key, goal.spec_revision, outcomes_decide(goal))

    def accumulating(
        self, history: Sequence[Mapping[str, Any]], stamp: str | None, *, before: int | None
    ) -> list[Mapping[str, Any]]:
        """The earlier observations an observation at *stamp* folds: none unless it accumulates."""
        return self.at_contents(history, stamp, before=before) if self.accumulates else []

    def at_contents(
        self, history: Sequence[Mapping[str, Any]], stamp: str | None, *, before: int | None
    ) -> list[Mapping[str, Any]]:
        """Candidate rows of this Goal at *stamp* with a sequence below *before*, in order."""
        if stamp is None:
            return []
        return [
            row
            for row in sorted(history, key=lambda item: item["sequence"])
            if row["criterion"] == self.key
            and row["role"] == "candidate"
            and (before is None or row["sequence"] < before)
            and row["goal_identity"]["record_id"] == self.record_id
            and row["goal_identity"]["spec_revisions"].get(self.key) == self.spec_revision
            and freshness_stamp(self.key, row["detail"]) == stamp
        ]


def run_outcomes(
    scope: HistoryScope,
    required: Sequence[TestRun],
    detail: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    *,
    before: int | None = None,
) -> list[dict[str, str]]:
    """Fold applicable earlier observations and the selected/new observation's own rows."""
    earlier = scope.accumulating(history, freshness_stamp(scope.key, detail), before=before)
    rows = [item for row in earlier for item in _rows(row["detail"])]
    return fold_test_run_outcomes([*rows, *_rows(detail)], required)


def contributing_observations(
    row: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    scope: HistoryScope,
    required: Sequence[TestRun] | None = None,
) -> list[Mapping[str, Any]]:
    """Earlier graded observations in the row's scope, optionally matched to current runs."""
    earlier = scope.accumulating(
        history, freshness_stamp(scope.key, row["detail"]), before=row["sequence"]
    )
    return [
        item
        for item in earlier
        if any(
            run.get("grade") in {"pass", "fail"}
            for run in (
                _rows(item["detail"])
                if required is None
                else matching_rows(item["detail"], required)
            )
        )
    ]


def judged_change(
    change: CriterionChange,
    goal: RecordedGoal,
    record: GoalRecord,
    history: Sequence[Mapping[str, Any]],
) -> CriterionChange:
    """*change* with its own matching Test Run rows and the verdict history allows.

    A simulation Goal is met exactly when every frozen run is ``passed`` in
    the history fold: what one invocation ran does not decide it. A Cycle Count
    Goal additionally needs its producer's verdict on thresholds. A coverage
    Goal needs only its producer's verdict on the coverage floors: its runs'
    outcomes are recorded and decide nothing (:func:`outcomes_decide`).

    A producer's verdict counts only when this same change carries the
    frozen runs that produced the metric (:meth:`GoalTestRuns.measured_by`).
    A change that does not measure the metric never supplies a verdict. When
    it adds nothing to the Goal's selected observation, that observation is
    recorded again, subject to the same history defects; otherwise the Goal is unmet.
    """
    key = goal.spec.key
    resolution = record.test_run_resolution(key)
    frozen = goal_test_runs(goal, resolution)
    detail = dict(change.detail)
    detail.pop("test_run_ledger", None)
    measured = _measured(detail, frozen)
    own = matching_rows(detail, frozen.runs)
    detail["test_runs"] = own
    scope = HistoryScope.of(record, goal)
    here = scope.at_contents(history, freshness_stamp(key, detail), before=None)
    outcomes = run_outcomes(scope, frozen.runs, detail, history)
    if not measured:
        selected = _selected(key, history, here)
        if selected is not None and _adds_nothing(selected, outcomes, scope, frozen, history):
            change = _restated(change, selected)
            detail, measured = change.detail, True
    defect = requirement_defect(resolution)
    if defect is None and scope.accumulates:
        defect = outcome_defect(outcomes)
    if defect is None and not measured:
        how = _MEASURING_RUN[goal.spec.family]
        defect = REASON_NOT_MEASURED, f"this run did not measure the Goal; {how}"
    produced = frozen.kind == "simulation" or change.met
    if defect is None:
        return replace(change, met=produced, reason="outcome", detail=detail)
    detail["goal_contract_violation"] = defect[1]
    # A measured threshold the producer found unmet keeps the producer's reason.
    reason = defect[0] if produced or not measured else change.reason
    return replace(change, met=False, reason=reason, detail=detail)


def _selected(
    key: str, history: Sequence[Mapping[str, Any]], here: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """Goal *key*'s selected observation, when it is the newest of *here*.

    *here* are the Goal's observations at the specification revision and file
    contents of the change being judged. ``None`` when the selected
    observation is not one of them, or there is none.
    """
    candidates = [row for row in history if row["criterion"] == key and row["role"] == "candidate"]
    latest = max(candidates, key=lambda row: row["sequence"], default=None)
    if latest is None or not here or here[-1]["sequence"] != latest["sequence"]:
        return None
    return latest


def _adds_nothing(
    selected: Mapping[str, Any],
    outcomes: list[dict[str, str]],
    scope: HistoryScope,
    frozen: GoalTestRuns,
    history: Sequence[Mapping[str, Any]],
) -> bool:
    """An unmeasured change must preserve an unchanged verdict or coverage measurement."""
    detail = selected["detail"]
    if not _measured(detail, frozen):
        # Restating a non-measurement would record it as an observed outcome.
        return False
    if scope.accumulates:
        return outcomes == run_outcomes(
            scope, frozen.runs, detail, history, before=selected["sequence"]
        )
    return "goal_contract_violation" not in detail


def _measured(detail: Mapping[str, Any], frozen: GoalTestRuns) -> bool:
    """Whether the observation *detail* itself measured the Goal's metric."""
    return not detail.get(OUTCOMES_ONLY_DETAIL_KEY) and frozen.measured_by(_rows(detail))


def _restated(change: CriterionChange, selected: Mapping[str, Any]) -> CriterionChange:
    """Record the selected measurement's own rows and verdict again."""
    detail = dict(selected["detail"])
    detail.pop("test_run_ledger", None)
    return replace(change, met=bool(selected["met"]), reason="outcome", detail=detail)


def requirement_defect(resolution: TestRunResolution | None) -> tuple[str, str] | None:
    """Why a Goal with *resolution* cannot be met by any evidence yet: (reason, text)."""
    if resolution is None:
        return REASON_PENDING, "the Goal's Test Runs are not resolved yet"
    if resolution.pending:
        slots = ", ".join(
            "every Test of the Target" if s == SUITE_SLOT else s for s in resolution.pending
        )
        return (
            REASON_PENDING,
            f"the Goal's Test Runs are not resolved yet ({slots} does not exist)",
        )
    if not resolution.runs:
        return REASON_EMPTY, "the Goal requires no Test Run: the Target's suite is empty"
    return None


def outcome_defect(runs: Sequence[Mapping[str, str]]) -> tuple[str, str] | None:
    """Why a fold with *runs* does not meet a Goal its outcomes decide: (reason, text)."""
    failed = [run["name"] for run in runs if run["outcome"] == "failed"]
    if failed:
        return REASON_FAILED, f"Test Runs failed at these file contents: {', '.join(failed)}"
    missing = [run["name"] for run in runs if run["outcome"] == "missing"]
    if missing:
        return REASON_INCOMPLETE, f"Test Runs have not passed yet: {', '.join(missing)}"
    return None
