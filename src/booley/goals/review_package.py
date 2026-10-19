"""Freeze Goal-owned evidence and decisions into neutral completion presentation facts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from booley.criteria.evidence_ledger import canonical_json, validated_evidence_records
from booley.criteria.state import DevelopmentState
from booley.goals.campaign_accounting import unsettled_campaigns
from booley.goals.changes import read_change_log
from booley.goals.derivation import selected_observations
from booley.goals.format import format_met_goal_metric
from booley.goals.generated_artifacts import inventory_files
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import GoalRecord
from booley.goals.paths import record_paths
from booley.goals.presentation import goal_presentation
from booley.goals.proposals import digest, list_proposals
from booley.goals.recorder import GOAL_SCOPE
from booley.goals.store import GoalStore
from booley.goals.test_run_evidence import HistoryScope, contributing_observations, outcomes_decide
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
    context: GoalReviewContext,
    record: GoalRecord,
    state: DevelopmentState,
    booley_dir: Path,
    *,
    omitted_inputs: frozenset[str] = frozenset(),
) -> GoalCompletionPackage:
    """Freeze exact selected rows, linked originals and all proposal decisions."""
    paths = record_paths(booley_dir, record.id)
    selected = selected_observations(record, state, booley_dir)
    records = validated_evidence_records(GOAL_SCOPE, Path(context.log_dir), state, {})
    indexed = {row["sequence"]: row for row in records}
    rows = _package_rows(context, record, state, selected, indexed)
    _add_baseline_reports(rows, record, selected, records, booley_dir)
    transactions = _transactions(
        Path(context.log_dir), state, indexed, omitted_inputs=omitted_inputs
    )
    _link_transactions(rows, transactions)
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
    return GoalCompletionPackage.from_json(facts | _unsettled(booley_dir, record))


def _link_transactions(rows: list[dict[str, Any]], transactions: list[dict[str, Any]]) -> None:
    for row in rows:
        row["selected_transaction"] = _selected_transaction(
            row["selected_observation"], transactions
        )


def _add_baseline_reports(
    rows: list[dict[str, Any]],
    record: GoalRecord,
    selected: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    booley_dir: Path,
) -> None:
    """Freeze observational SIM reports without contributing completion authority."""
    from booley.goals.freshness import DEFAULT_RESOLVERS
    from booley.goals.sim_baseline_store import sim_goals
    from booley.goals.sim_transitions import goal_transitions, transition_summary

    for goal in sim_goals(record):
        transitions = goal_transitions(
            booley_dir,
            record,
            goal,
            selected.get(goal.spec.key),
            records,
            Path(record.worktree_path),
            DEFAULT_RESOLVERS,
        )
        row = next(row for row in rows if row["key"] == goal.spec.key)
        row["simulation_baseline"] = {
            "summary": transition_summary(transitions),
            "runs": transitions,
            "attention": [
                run["name"]
                for run in transitions
                if run["transition"] in {"pass -> fail", "new (never seen failing) -> pass"}
            ],
        }


def _unsettled(booley_dir: Path, record: GoalRecord) -> dict[str, Any]:
    """The Simulation Campaigns the reviewer should see were never fully accounted for.

    Open ones, and registrations a human-approved change set aside because
    nobody could read them (ADR 0072). No key at all when there is none.
    """
    listed = unsettled_campaigns(GoalStore(booley_dir), record)
    return {"unsettled_campaigns": listed} if listed else {}


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
            raise LifecycleError(
                f"selected evidence missing for {goal_presentation(goal.spec).text}"
            )
        originals = original_observations(observation, indexed, record)
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
                "metric": format_met_goal_metric(goal.spec.key, state.criteria[goal.spec.key]),
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
    row: dict[str, Any], indexed: dict[int, dict[str, Any]], record: GoalRecord
) -> list[dict[str, Any]]:
    """History contributors and digest-verified approved derivations, in sequence order."""
    originals: dict[int, dict[str, Any]] = {}
    pending = [row]
    seen: set[int] = set()
    goals = {goal.spec.key: goal for goal in record.goals}
    while pending:
        current = pending.pop(0)
        if current["sequence"] in seen:
            continue
        seen.add(current["sequence"])
        resolution = record.test_run_resolution(current.get("criterion", ""))
        sources = [current]
        if resolution is not None:
            goal = goals[current["criterion"]]
            revision = current["goal_identity"]["spec_revisions"][goal.spec.key]
            scope = HistoryScope(
                current["goal_identity"]["record_id"],
                goal.spec.key,
                revision,
                outcomes_decide(goal),
            )
            required = resolution.test_runs if revision == goal.spec_revision else None
            for contributor in contributing_observations(
                current, list(indexed.values()), scope, required
            ):
                original = indexed[contributor["sequence"]]
                originals[original["sequence"]] = original
                sources.append(original)
        references = [
            reference
            for source in sources
            for reference in source["detail"].get("goal_derivation", {}).get("source_evidence", [])
        ]
        for reference in references:
            original = indexed.get(reference["sequence"])
            if original is None or _row_digest(original) != reference["sha256"]:
                raise LifecycleError(
                    "derived Goal evidence has missing or substituted original observation"
                )
            originals[original["sequence"]] = original
            pending.append(original)
    originals.pop(row["sequence"], None)
    return [originals[sequence] for sequence in sorted(originals)]


def _transactions(
    logs: Path,
    state: DevelopmentState,
    indexed: dict[int, dict[str, Any]],
    *,
    omitted_inputs: frozenset[str] = frozenset(),
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
        manifest_sha256 = None if manifest is None else _row_digest(manifest)
        inline = None if _contains_omitted_inventory(manifest, omitted_inputs) else manifest
        result.append(
            {
                "transaction_id": identity,
                "manifest": inline,
                "manifest_sha256": manifest_sha256,
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


def presentation_observations(
    record: GoalRecord, state: DevelopmentState, project: Path
) -> tuple[dict[str, Any], ...]:
    """Exact selected/original observations whose independent input proof is retained."""
    paths = record_paths(project, record.id)
    rows = validated_evidence_records(GOAL_SCOPE, paths.logs_dir, state, {})
    indexed = {row["sequence"]: row for row in rows}
    selected = selected_observations(record, state, project)
    return tuple(
        row
        for value in selected.values()
        for row in (value, *original_observations(value, indexed, record))
    )


def _contains_omitted_inventory(value: Any, omitted: frozenset[str]) -> bool:
    """Recognize source inventory fields, preserving every authenticated manifest byte."""
    if not omitted:
        return False
    if isinstance(value, dict):
        document = cast("dict[str, Any]", value)
        stamp: Any = document.get("_source_fingerprint")
        if isinstance(stamp, dict):
            fingerprint: Any = cast("dict[str, Any]", stamp).get("fingerprint", {})
            for category in ("rtl", "tb", "workload", "campaign", "target_surface"):
                if any(name in omitted for name in inventory_files(fingerprint, category)):
                    return True
        return any(_contains_omitted_inventory(item, omitted) for item in document.values())
    if isinstance(value, list):
        return any(_contains_omitted_inventory(item, omitted) for item in cast("list[Any]", value))
    return False
