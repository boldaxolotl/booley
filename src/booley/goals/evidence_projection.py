"""Canonical Goal evidence selection and observational state projection."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from booley.criteria.evidence_ledger import AcceptanceLedgerError, apply_observation
from booley.criteria.state import DevelopmentState
from booley.evidence.fields import SOURCE_INVALIDATED_REASON
from booley.goals.model import GoalRecord


def current_observations(
    record: GoalRecord, rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep complete current revision groups; refuse foreign records."""
    revisions = {goal.spec.key: goal.spec_revision for goal in record.goals}
    current = []
    for row in rows:
        group = row["goal_identity"]
        if group["record_id"] != record.id:
            raise AcceptanceLedgerError("Criterion evidence names another Goal Record")
        if row["criterion"] not in group["goal_keys"]:
            raise AcceptanceLedgerError("Goal evidence names a Criterion outside its identity")
        if all(
            revisions.get(key) == revision for key, revision in group["spec_revisions"].items()
        ):
            if row["criterion"] not in revisions or not row["mandatory"]:
                raise AcceptanceLedgerError("Goal evidence contradicts its declaration")
            current.append(row)
    return current


def selected_candidates(
    record: GoalRecord, rows: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The highest-sequence candidate of each current Goal."""
    return {
        row["criterion"]: row
        for row in current_observations(record, rows)
        if row["role"] == "candidate"
    }


def project_evidence(
    record: GoalRecord, state: DevelopmentState, log_dir: Path
) -> list[dict[str, Any]]:
    """Recover evidence fields in memory, without changing the ledger or taking a lock."""
    from booley.criteria.evidence_ledger import validated_evidence_records
    from booley.goals.recorder import GOAL_SCOPE

    history = validated_evidence_records(GOAL_SCOPE, log_dir, state, {})
    rows = current_observations(record, history)
    for goal in record.goals:
        entry = state.criteria.get(goal.spec.key)
        if entry is not None:
            entry.met = False
            entry.mandatory = True
            entry.detail = {}
    for row in rows:
        entry = state.criteria.get(row["criterion"])
        if entry is None:
            continue
        invalidated = row.get("reason") == SOURCE_INVALIDATED_REASON
        if row["role"] == "baseline" and not invalidated:
            # Baseline failures contribute transition history but never a candidate verdict.
            met, params, detail = entry.met, entry.params, entry.detail
            apply_observation(state, row)
            entry.met, entry.params, entry.detail = met, params, detail
        else:
            apply_observation(state, row)
            entry.updated_at = row["recorded_at"]
            entry.stale = invalidated
    return history
