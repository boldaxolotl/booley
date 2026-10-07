"""Freeze Goal-owned evidence and decisions into neutral completion presentation facts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from booley.criteria.evidence_ledger import canonical_json, validated_evidence_records
from booley.criteria.state import DevelopmentState
from booley.goals.changes import read_change_log
from booley.goals.derivation import selected_observations
from booley.goals.format import format_criterion_metric
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import GoalRecord
from booley.goals.paths import record_paths
from booley.goals.proposals import digest, list_proposals
from booley.goals.recorder import GOAL_SCOPE
from booley.review.goal_package import GoalCompletionPackage, GoalReviewContext


def completion_authority_digest(record: GoalRecord, state: DevelopmentState, project: Path) -> str:
    """Activity is observational; Criteria, exact evidence and proposal audit are authority."""
    criteria = state.to_dict()["criteria"]
    paths = record_paths(project, record.id)
    return digest(
        {
            "criteria": {goal.spec.key: criteria.get(goal.spec.key) for goal in record.goals},
            "selected": selected_observations(record, state, project),
            "proposals": _proposal_audit(paths.root),
            "changes": [entry.to_json() for entry in read_change_log(paths.changes_file).applied],
        }
    )


def build_goal_package(
    context: GoalReviewContext, record: GoalRecord, state: DevelopmentState, project_dir: Path
) -> GoalCompletionPackage:
    """Freeze exact selected rows, linked originals and all proposal decisions."""
    paths = record_paths(project_dir, record.id)
    selected = selected_observations(record, state, project_dir)
    records = validated_evidence_records(GOAL_SCOPE, Path(context.log_dir), state, {})
    indexed = {row["sequence"]: row for row in records}
    rows = _package_rows(context, record, state, selected, indexed)
    transactions = _transactions(Path(context.log_dir), state, indexed)
    for row in rows:
        row["selected_transaction"] = _selected_transaction(
            row["selected_observation"], transactions
        )
    decisions = _proposal_audit(paths.root)
    facts = {
        "purpose": "booley.goal-completion/v1",
        "record": {**record.to_json(), "project_snapshot": None},
        "base_sha": context.base_sha,
        "head_sha": context.head_sha,
        "branch": context.branch,
        "goals": rows,
        "input_proof": context.input_proof,
        "change_log": [entry.to_json() for entry in read_change_log(paths.changes_file).applied],
        "proposal_decisions": decisions,
        "applied_proposal_decisions": [row for row in decisions if row["state"] == "applied"],
        "rejected_proposal_decisions": [row for row in decisions if row["state"] == "rejected"],
        "agent_recorded_decisions": [
            row
            for row in decisions
            if row["decision"] is not None and row["decision"]["source"] == "agent-recorded"
        ],
        "evidence_transactions": transactions,
        "target_changes": context.target_changes,
        "diff_summary": context.diff_summary,
        "constraint_edits": [
            row
            for row in context.diff_summary
            if Path(row["path"]).suffix.lower() in {".sdc", ".xdc"}
        ],
        "session_summary": context.session_summary,
    }
    return GoalCompletionPackage.from_json(facts)


def _proposal_audit(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "proposal": view.proposal.to_json(),
            "payload_digest": view.proposal.payload_digest,
            "state": view.state,
            "decision": None if view.decision is None else view.decision.to_json(),
            "transaction_digest": view.transaction_digest,
            "closed_by_abandonment": view.closed_by_abandonment,
        }
        for view in list_proposals(root)
    ]


def _package_rows(
    context: GoalReviewContext,
    record: GoalRecord,
    state: DevelopmentState,
    selected: dict[str, dict[str, Any]],
    indexed: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for goal in record.goals:
        observation = selected.get(goal.spec.key)
        if observation is None:
            raise LifecycleError(f"selected evidence missing for {goal.spec.key}")
        originals = original_observations(observation, indexed)
        modified = any(
            item["change"] == "modified"
            and item["target"] == context.target_identities.get(goal.spec.target or "")
            for item in context.target_changes
        )
        rows.append(
            {
                "record_id": record.id,
                "key": goal.spec.key,
                "spec_revision": goal.spec_revision,
                "spec": goal.spec.to_json(),
                "met": True,
                "fresh": True,
                "metric": format_criterion_metric(goal.spec.key, state.criteria[goal.spec.key]),
                "modified_target": modified,
                "selected_observation": observation,
                "observation_sha256": _row_digest(observation),
                "original_observations": originals,
            }
        )
    return rows


def _row_digest(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(row)).hexdigest()


def original_observations(
    row: dict[str, Any], indexed: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Digest-verified linked producer observations, including chained derivations."""
    originals: list[dict[str, Any]] = []
    pending = list(row["detail"].get("goal_derivation", {}).get("source_evidence", []))
    seen = {row["sequence"]}
    while pending:
        reference = pending.pop(0)
        original = indexed.get(reference["sequence"])
        if original is None or _row_digest(original) != reference["sha256"]:
            raise LifecycleError(
                "derived Goal evidence has missing or substituted original observation"
            )
        if original["sequence"] not in seen:
            seen.add(original["sequence"])
            originals.append(original)
            pending.extend(
                original["detail"].get("goal_derivation", {}).get("source_evidence", [])
            )
    return originals


def _transactions(
    logs: Path, state: DevelopmentState, indexed: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Validated readers already proved the manifests or legacy transaction rows."""
    result: list[dict[str, Any]] = []
    for identity in state.acceptance_transactions:
        path = logs / "acceptance" / "transactions" / (identity + ".json")
        manifest = json.loads(path.read_bytes()) if path.exists() else None
        rows = [
            row
            for sequence, row in indexed.items()
            if row.get("transaction_id") == identity
            or (
                logs / "acceptance" / "evidence" / f"{sequence:09d}.tx.{identity}" / "record.json"
            ).is_file()
        ]
        result.append(
            {
                "transaction_id": identity,
                "manifest": manifest,
                "manifest_sha256": None if manifest is None else _row_digest(manifest),
                "observations": [
                    {"sequence": row["sequence"], "sha256": _row_digest(row)} for row in rows
                ],
            }
        )
    return result


def _selected_transaction(
    row: dict[str, Any], transactions: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for transaction in transactions:
        if any(item["sequence"] == row["sequence"] for item in transaction["observations"]):
            return {
                "transaction_id": transaction["transaction_id"],
                "manifest_sha256": transaction["manifest_sha256"],
            }
    return None
