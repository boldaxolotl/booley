"""Evidence of a Goal's frozen Test Runs: matching, the ledger, and its check (ADR 0072).

A Test Run is evidence for a Goal only when its resolved name and its
declared-inputs fingerprint equal one of the Goal's frozen Test Runs. Every
observation of such a Goal carries

- ``test_runs``: the matching runs of its own invocation (immutable facts);
- ``test_run_ledger``: one entry per frozen run, ``passed``, ``failed`` or
  ``missing``, **materialized when the observation is recorded** by one pure
  fold (:func:`fold_test_run_outcomes`) over every earlier observation of the
  Goal at the same specification revision and the same dependent file
  contents, plus the new invocation. A failure is absorbing: a later pass at
  the same contents never cancels it. The earlier observations that
  contributed are linked by ledger sequence and digest (``source_evidence``).

Goal status keeps judging one selected observation, but it **recomputes** the
fold over the ledger rows up to that observation and compares
(:func:`ledger_defect`). A Simulation Campaign transaction is frozen before
it is published and can be recovered later at a higher sequence; the ledger
inside it may predate a failure recorded in between. Such an observation no
longer equals its recomputation and the Goal is unmet until the Flow runs
again, which folds the failure in.

The Goal is met only when its resolution is complete, it requires at least
one Test Run, and every required run is ``passed``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from booley.criteria.evidence_ledger import canonical_json
from booley.criteria.state import CriterionChange
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.evidence.test_run_ledger import (
    LEDGER_DETAIL_KEY,
    LEDGER_SCHEMA,
    LedgerFormatError,
    decode_ledger,
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
REASON_PENDING = "goal-test-run-pending"
REASON_EMPTY = "goal-simulation-suite-empty"
REASON_FAILED = "goal-test-run-failed"
REASON_INCOMPLETE = "goal-test-run-incomplete"
REASON_OUTDATED = "goal-test-run-ledger-outdated"
REASON_NO_LEDGER = "goal-test-run-ledger-missing"
REASON_MISSING = "goal-test-run-missing"
REASON_CHANGED = "goal-test-run-changed"
REASON_NOT_MEASURED = "goal-test-run-metric-missing"
REASON_MALFORMED = "goal-test-run-ledger-malformed"
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


def _member(value: object, name: str) -> object:
    """``value[name]`` when *value* is a mapping, else ``None``."""
    return cast("Mapping[str, object]", value).get(name) if isinstance(value, Mapping) else None


@dataclass(frozen=True)
class LedgerScope:
    """Which observations accumulate with one another: one Goal at one revision."""

    record_id: str
    key: str
    spec_revision: int

    def accumulating(
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


def row_digest(row: Mapping[str, Any]) -> str:
    """The digest that links a ledger row as contributing evidence."""
    return hashlib.sha256(canonical_json(row)).hexdigest()


def build_ledger(
    earlier: Sequence[Mapping[str, Any]],
    own: Sequence[Mapping[str, Any]],
    required: Sequence[TestRun],
) -> dict[str, Any]:
    """The ledger of an observation with matching rows *own*, after *earlier* observations.

    Every earlier observation that observed a required run contributed and
    is linked in ``source_evidence``.
    """
    contributors = [
        (row, graded)
        for row in earlier
        if (graded := _graded(matching_rows(row["detail"], required)))
    ]
    rows = [item for _, graded in contributors for item in graded]
    return {
        "schema": LEDGER_SCHEMA,
        "runs": fold_test_run_outcomes([*rows, *own], required),
        "source_evidence": [
            {"sequence": row["sequence"], "sha256": row_digest(row)} for row, _ in contributors
        ],
    }


def _graded(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("grade") in {"pass", "fail"}]


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


def ledger_runs(detail: Mapping[str, Any]) -> list[dict[str, str]] | None:
    """The per-run outcomes *detail* stores, or ``None`` when it stores no ledger.

    Raises :class:`LedgerFormatError` for a malformed ledger.
    """
    ledger = decode_ledger(detail)
    return None if ledger is None else list(ledger.runs)


def judged_change(
    change: CriterionChange,
    goal: RecordedGoal,
    record: GoalRecord,
    history: Sequence[Mapping[str, Any]],
) -> CriterionChange:
    """*change* with its matching Test Run rows, its ledger, and the verdict they allow.

    A simulation Goal is met exactly when every frozen run is ``passed`` in
    the ledger: what one invocation ran does not decide it. A Cycle Count or
    coverage Goal additionally needs its producer's verdict on thresholds,
    and that verdict counts only when this same change carries the frozen
    runs that produced the metric (:meth:`GoalTestRuns.measured_by`). A
    change that does not measure the metric still enters its matching runs'
    outcomes into the ledger; it never supplies a verdict.
    """
    key = goal.spec.key
    resolution = record.test_run_resolution(key)
    frozen = goal_test_runs(goal, resolution)
    detail = dict(change.detail)
    measured = not detail.get(OUTCOMES_ONLY_DETAIL_KEY) and frozen.measured_by(_rows(detail))
    own = matching_rows(detail, frozen.runs)
    detail["test_runs"] = own
    scope = LedgerScope(record.id, key, goal.spec_revision)
    earlier = scope.accumulating(history, freshness_stamp(key, detail), before=None)
    ledger = build_ledger(earlier, _graded(own), frozen.runs)
    restated = None if measured else _restated(change, key, history, earlier, ledger, frozen.runs)
    if restated is not None:
        return restated
    detail[LEDGER_DETAIL_KEY] = ledger
    defect = requirement_defect(resolution) or outcome_defect(ledger["runs"])
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


def _restated(
    change: CriterionChange,
    key: str,
    history: Sequence[Mapping[str, Any]],
    earlier: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Any],
    required: Sequence[TestRun],
) -> CriterionChange | None:
    """The Goal's selected observation again, when *change* adds nothing to it.

    A change that does not measure the Goal's metric would otherwise replace
    a measured observation and unmeet the Goal although nothing was learnt:
    its runs' outcomes are already what the selected observation's ledger
    holds, at the same specification revision and file contents. Then the
    selected observation is recorded again, with its own rows and verdict.
    ``None`` when the change does add an outcome, or there is nothing at
    these contents to restate.
    """
    candidates = [row for row in history if row["criterion"] == key and row["role"] == "candidate"]
    latest = max(candidates, key=lambda row: row["sequence"], default=None)
    if latest is None or not earlier or earlier[-1]["sequence"] != latest["sequence"]:
        return None
    selected = cast("Mapping[str, Any]", latest["detail"])
    try:
        unchanged = ledger_runs(selected) == ledger["runs"]
    except LedgerFormatError:
        unchanged = False  # a ledger that cannot be read is never restated
    if not unchanged:
        return None
    again = build_ledger(earlier, _graded(matching_rows(selected, required)), required)
    detail = {**selected, LEDGER_DETAIL_KEY: again}
    return replace(change, met=bool(latest["met"]), reason="outcome", detail=detail)


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
    """Why a ledger with *runs* does not meet its Goal: (reason, text)."""
    failed = [run["name"] for run in runs if run["outcome"] == "failed"]
    if failed:
        return REASON_FAILED, f"Test Runs failed at these file contents: {', '.join(failed)}"
    missing = [run["name"] for run in runs if run["outcome"] == "missing"]
    if missing:
        return REASON_INCOMPLETE, f"Test Runs have not passed yet: {', '.join(missing)}"
    return None


def ledger_defect(
    row: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    scope: LedgerScope,
    required: Sequence[TestRun],
) -> tuple[str, str] | None:
    """Why the ledger stored in observation *row* cannot be trusted: (reason, text).

    The stored ledger must equal the fold recomputed over the ledger rows up
    to *row*, and every contributor it links must still be the row it names.
    """
    detail = cast("Mapping[str, Any]", row["detail"])
    try:
        stored = decode_ledger(detail)
    except LedgerFormatError as exc:
        return REASON_MALFORMED, f"the recorded Test Run outcomes cannot be read: {exc}"
    if stored is None:
        return REASON_NO_LEDGER, (
            "the evidence predates Goals that name Test Runs; run the Simulation Flow again"
        )
    stamp = freshness_stamp(scope.key, detail)
    earlier = scope.accumulating(history, stamp, before=row["sequence"])
    recomputed = build_ledger(earlier, _graded(matching_rows(detail, required)), required)
    indexed = {item["sequence"]: item for item in history}
    substituted = any(
        sequence not in indexed or row_digest(indexed[sequence]) != digest
        for sequence, digest in stored.source_evidence
    )
    if substituted or recomputed["runs"] != list(stored.runs):
        return REASON_OUTDATED, (
            "the recorded Test Run outcomes no longer equal the recorded evidence; "
            "rerun the Simulation Flow"
        )
    return None
