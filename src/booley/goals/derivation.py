"""Approved-change derivations from selected immutable producer observations.

The old verdict is not producer success. Freshness is proved even for an unmet
observation; old source/receipt/time bytes stay intact. Each derived observation
names its original ledger identity and digest, never a fictitious new run.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from booley.criteria.evidence_ledger import canonical_json, validated_evidence_records
from booley.criteria.state import CriterionChange, CriterionEntry, DevelopmentState
from booley.evidence.review_receipt import REVIEW_DETAIL_VERSION
from booley.goals.freshness import (
    DEFAULT_RESOLVERS,
    GoalFreshnessResolvers,
    evaluate_goal_freshness,
)
from booley.goals.model import GoalFamily, GoalRecord, GoalSpec
from booley.goals.paths import record_paths
from booley.goals.proposals import Proposal
from booley.goals.recorder import GOAL_SCOPE

CoverageEvaluation = Callable[[GoalSpec, Mapping[str, Any]], tuple[bool, dict[str, Any]]]


@dataclass(frozen=True)
class Derivation:
    """A prepared change with the original producer timestamp."""

    change: CriterionChange
    recorded_at: str


def selected_observations(
    record: GoalRecord, state: DevelopmentState, project_dir: Path
) -> dict[str, dict[str, Any]]:
    """Latest selected producer observations under complete original identity groups."""
    revisions = {goal.spec.key: goal.spec_revision for goal in record.goals}
    records = validated_evidence_records(
        GOAL_SCOPE, record_paths(project_dir, record.id).logs_dir, state, {}
    )
    return {
        row["criterion"]: row
        for row in records
        if row["role"] == "candidate"
        and row["goal_identity"]["record_id"] == record.id
        and all(
            revisions.get(key) == revision
            for key, revision in row["goal_identity"]["spec_revisions"].items()
        )
    }


def fresh_observation(
    goal: GoalSpec,
    row: dict[str, Any],
    state: DevelopmentState,
    work_dir: Path,
    resolvers: GoalFreshnessResolvers = DEFAULT_RESOLVERS,
) -> bool:
    """Check correspondence and freshness without trusting the failed threshold verdict."""
    entry = state.criteria.get(goal.key)
    if (
        entry is None
        or entry.stale
        or entry.params != row["params"]
        or entry.detail != row["detail"]
        or entry.met != row["met"]
    ):
        return False
    probe = copy.deepcopy(entry)
    probe.met = True  # freshness eligibility only, never producer authority
    return not evaluate_goal_freshness(
        goal.key, probe, goal=goal, work_dir=work_dir, resolvers=resolvers
    ).stale


def derive_changes(
    proposal: Proposal,
    record: GoalRecord,
    state: DevelopmentState,
    project_dir: Path,
    *,
    coverage: CoverageEvaluation | None = None,
    resolvers: GoalFreshnessResolvers = DEFAULT_RESOLVERS,
) -> tuple[Derivation, ...]:
    """Derive the changed Goal and valid unchanged siblings of invalidated groups."""
    rows = selected_observations(record, state, project_dir)
    affected_groups = [
        row["goal_identity"]
        for row in rows.values()
        if proposal.goal_key in row["goal_identity"]["goal_keys"]
    ]
    goals = {goal.spec.key: goal.spec for goal in record.goals}
    derived: list[Derivation] = []
    for key, row in rows.items():
        is_changed = key == proposal.goal_key
        if not is_changed and row["goal_identity"] not in affected_groups:
            continue
        if not fresh_observation(goals[key], row, state, Path(record.worktree_path), resolvers):
            continue
        if is_changed and proposal.kind.value not in {"relax", "waiver"}:
            continue
        goal = proposal.after if is_changed else goals[key]
        params = proposal.runtime_params if is_changed else row["params"]
        detail = copy.deepcopy(row["detail"])
        met = _evaluate(goal, params, detail, row["met"], coverage) if is_changed else row["met"]
        derived.append(_derived(proposal, goal, params, detail, met, row))
    # A changed Goal without selected/fresh producer evidence stays unmet.
    return tuple(derived)


def _evaluate(
    goal: GoalSpec,
    params: dict[str, Any],
    detail: dict[str, Any],
    old_met: bool,
    coverage: CoverageEvaluation | None,
) -> bool:
    if goal.family is GoalFamily.COVERAGE:
        if coverage is None:
            return False
        met, evaluated = coverage(goal, detail)
        detail.update(evaluated)
        return met
    if goal.family is GoalFamily.REVIEW:
        completed = _review_completed(detail)
        if completed and "issue_list" not in detail:
            # Clean receipts retain finding lifecycle lists; done replay reads issue_list.
            detail["issue_list"] = [*detail["pending"], *detail.get("observations", [])]
        return completed
    success = _producer_success(goal.family, detail, old_met)
    if goal.family is GoalFamily.MUTATION:
        detail["min_detected"] = params["min_detected"]
        return success and detail["detected"] >= params["min_detected"]
    probe = DevelopmentState(criteria={goal.key: CriterionEntry(params=copy.deepcopy(params))})
    changes = probe.set_criterion(goal.key, success, detail=detail)
    detail.update(changes[-1].detail)
    return changes[-1].met


def _producer_success(family: GoalFamily, detail: dict[str, Any], old_met: bool) -> bool:
    if family is GoalFamily.CYCLE_COUNT:
        return (
            detail.get("cycle_observation") == "observed"
            and type(detail.get("cycles")) is int
            and detail["cycles"] >= 0
            and not detail.get("reason")
            and not detail.get("error")
            and not detail.get("timed_out")
        )
    if family in {GoalFamily.SYNTH, GoalFamily.FPGA}:
        implementation = cast("dict[str, Any]", detail.get("implementation") or {})
        status = cast("dict[str, Any]", implementation.get("status") or {})
        return (
            status.get("passed") is True
            and not status.get("infra_error")
            and not status.get("timed_out")
        )
    if family is GoalFamily.MUTATION:
        return (
            type(detail.get("detected")) is int
            and type(detail.get("total_valid")) is int
            and detail["total_valid"] > 0
            and not detail.get("error")
        )
    return old_met


def _review_completed(detail: dict[str, Any]) -> bool:
    return (
        detail.get("review_detail_version") == REVIEW_DETAIL_VERSION
        and isinstance(detail.get("pending", detail.get("issue_list")), list)
        and not detail.get("needs_discovery")
        and not detail.get("error")
        and isinstance(detail.get("receipt_id"), str)
    )


def _derived(
    proposal: Proposal,
    goal: GoalSpec,
    params: dict[str, Any],
    detail: dict[str, Any],
    met: bool,
    row: dict[str, Any],
) -> Derivation:
    detail["goal_derivation"] = {
        "schema": "booley.goal-derivation/v1",
        "proposal_id": proposal.id,
        "payload_digest": proposal.payload_digest,
        "source_evidence": [
            {
                "sequence": row["sequence"],
                "sha256": hashlib.sha256(canonical_json(row)).hexdigest(),
                "criterion": row["criterion"],
                "goal_identity": row["goal_identity"],
                "producer": row["producer"],
                "invocation_id": row["invocation_id"],
                "recorded_at": row["recorded_at"],
            }
        ],
    }
    return Derivation(
        CriterionChange(goal.key, met, "approved Goal change", detail, True, params),
        row["recorded_at"],
    )
