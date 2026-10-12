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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from booley.criteria.evidence_ledger import canonical_json
from booley.criteria.state import CriterionChange
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.frozen_test_runs import FrozenTestRuns, GoalRunsKind, GoalTestRuns, SeedGroup
from booley.flows.test_runs import SeedCount, TestRun, parse_test_selector
from booley.goals.freshness import goal_required_categories
from booley.goals.model import GoalFamily, GoalRecord, RecordedGoal
from booley.goals.test_run_goals import names_test_runs
from booley.goals.test_run_resolution import SUITE_SLOT, TestRunResolution

#: Detail key of the materialized per-run outcomes of a Goal's frozen Test Runs.
LEDGER_DETAIL_KEY = "test_run_ledger"
LEDGER_SCHEMA = "booley.goal-test-run-ledger/v1"

RunOutcome = Literal["passed", "failed", "missing"]

# Named reasons a Goal that is judged by Test Runs is unmet.
REASON_PENDING = "goal-test-run-pending"
REASON_EMPTY = "goal-simulation-suite-empty"
REASON_FAILED = "goal-test-run-failed"
REASON_INCOMPLETE = "goal-test-run-incomplete"
REASON_OUTDATED = "goal-test-run-ledger-outdated"
REASON_NO_LEDGER = "goal-test-run-ledger-missing"
REASON_MISSING = "goal-test-run-missing"
REASON_CHANGED = "goal-test-run-changed"

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
        assert spec.target is not None
        resolution = record.test_run_resolution(spec.key)
        if resolution is None:
            goals.append(
                GoalTestRuns(spec.key, _KINDS[spec.family], spec.target, (), False, False)
            )
            continue
        goals.append(
            GoalTestRuns(
                spec.key,
                _KINDS[spec.family],
                spec.target,
                resolution.test_runs,
                suite=any(item.slot == SUITE_SLOT for item in resolution.runs)
                or SUITE_SLOT in resolution.pending,
                complete=not resolution.pending,
                seed_groups=_seed_groups(resolution),
            )
        )
    return FrozenTestRuns(tuple(goals))


def _seed_groups(resolution: TestRunResolution) -> tuple[SeedGroup, ...]:
    """The frozen seeds of each multi-seed slot of *resolution*."""
    groups: list[SeedGroup] = []
    for slot in dict.fromkeys(item.slot for item in resolution.runs):
        if slot == SUITE_SLOT or not isinstance(parse_test_selector(slot).seed_form, SeedCount):
            continue
        runs = [item.run for item in resolution.runs if item.slot == slot]
        seeds = tuple(run.seed for run in runs if run.seed is not None)
        if runs[0].test is not None:
            groups.append(SeedGroup(runs[0].test, runs[0].variant, seeds))
    return tuple(groups)


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


def fold_test_run_outcomes(
    rows: Iterable[Mapping[str, Any]], required: Sequence[TestRun]
) -> list[dict[str, str]]:
    """The outcome of every run of *required* after *rows*, in required order.

    *rows* are Test Run rows (``name``, ``fingerprint``, ``grade``) in the
    order they were observed. A row counts only for the required run with
    its name and fingerprint. ``fail`` makes the run ``failed`` for good;
    ``pass`` makes a ``missing`` run ``passed``; ``error`` and
    ``inconclusive`` observed nothing and change nothing.
    """
    outcomes: dict[tuple[str, str], RunOutcome] = {
        (run.name, run.fingerprint): "missing" for run in required
    }
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


def matching_rows(detail: Mapping[str, Any], required: Sequence[TestRun]) -> list[dict[str, Any]]:
    """The Test Run rows of *detail* that are one of *required*, well formed or not at all."""
    wanted = {(run.name, run.fingerprint) for run in required}
    raw = detail.get("test_runs")
    rows = cast("list[Any]", raw) if isinstance(raw, list) else []
    return [
        dict(cast("Mapping[str, Any]", row))
        for row in rows
        if isinstance(row, Mapping) and (row.get("name"), row.get("fingerprint")) in wanted  # pyright: ignore[reportUnknownMemberType]
    ]


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
    """The per-run outcomes *detail* stores, or ``None`` when it stores no ledger."""
    runs = _member(detail.get(LEDGER_DETAIL_KEY), "runs")
    return cast("list[dict[str, str]]", runs) if isinstance(runs, list) else None


def judged_change(
    change: CriterionChange,
    goal: RecordedGoal,
    record: GoalRecord,
    history: Sequence[Mapping[str, Any]],
) -> CriterionChange:
    """*change* with its matching Test Run rows, its ledger, and the verdict they allow.

    A simulation Goal is met exactly when every frozen run is ``passed`` in
    the ledger: what one invocation ran does not decide it. A Cycle Count or
    coverage Goal additionally keeps its producer's verdict on thresholds.
    """
    key = goal.spec.key
    resolution = record.test_run_resolution(key)
    required = () if resolution is None else resolution.test_runs
    detail = dict(change.detail)
    own = matching_rows(detail, required)
    detail["test_runs"] = own
    scope = LedgerScope(record.id, key, goal.spec_revision)
    earlier = scope.accumulating(history, freshness_stamp(key, detail), before=None)
    ledger = build_ledger(earlier, _graded(own), required)
    detail[LEDGER_DETAIL_KEY] = ledger
    defect = requirement_defect(resolution) or outcome_defect(ledger["runs"])
    produced = change.met or goal.spec.family is GoalFamily.SIM
    if defect is None:
        return replace(change, met=produced, reason="outcome", detail=detail)
    detail["goal_contract_violation"] = defect[1]
    reason = defect[0] if produced else change.reason
    return replace(change, met=False, reason=reason, detail=detail)


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
    stored = ledger_runs(detail)
    if stored is None:
        return REASON_NO_LEDGER, (
            "the evidence predates Goals that name Test Runs; run the Simulation Flow again"
        )
    stamp = freshness_stamp(scope.key, detail)
    earlier = scope.accumulating(history, stamp, before=row["sequence"])
    recomputed = build_ledger(earlier, _graded(matching_rows(detail, required)), required)
    indexed = {item["sequence"]: item for item in history}
    links = cast("list[Mapping[str, Any]]", detail[LEDGER_DETAIL_KEY].get("source_evidence") or [])
    substituted = any(
        link.get("sequence") not in indexed
        or row_digest(indexed[link["sequence"]]) != link.get("sha256")
        for link in links
    )
    if substituted or recomputed["runs"] != stored:
        return REASON_OUTDATED, (
            "the recorded Test Run outcomes no longer equal the recorded evidence; "
            "rerun the Simulation Flow"
        )
    return None
