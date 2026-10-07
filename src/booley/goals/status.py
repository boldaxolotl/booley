"""Read declared Goals with protected, checkout, specification, and source freshness checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from booley.criteria.evidence_ledger import validated_evidence_records
from booley.goals.checkout import CheckoutError, GoalCheckout, branch_ref
from booley.goals.format import format_criterion_metric
from booley.goals.freshness import (
    DEFAULT_RESOLVERS,
    GoalFreshnessResolvers,
    evaluate_goal_freshness,
)
from booley.goals.model import GoalRecord, RecordedGoal
from booley.goals.paths import record_paths
from booley.goals.recorder import GOAL_SCOPE
from booley.goals.state_store import load_goal_state
from booley.goals.store import GoalStore, GoalStoreError
from booley.goals.warnings import goal_warnings, protected_warnings


@dataclass(frozen=True)
class GoalStatus:
    """One declared Goal, including missing evidence."""

    key: str
    status: Literal["met", "unmet", "stale"]
    evidence_summary: str
    reason: str = ""


@dataclass(frozen=True)
class GoalStatusView:
    """A point-in-time read; proposals arrive in Phase 4."""

    record: GoalRecord
    goals: tuple[GoalStatus, ...]
    warning: str = ""
    pending_proposals: int = 0

    @property
    def met(self) -> int:
        """Number met with current evidence."""
        return sum(goal.status == "met" for goal in self.goals)


def checkout_violation(store: GoalStore, record: GoalRecord, work_dir: Path) -> str:
    """Require the Worktree Identity and symbolic Goal Branch recorded at entry."""
    try:
        if store.identify_worktree(work_dir) != record.worktree:
            return "the checkout no longer has the recorded Worktree Identity"
        ref = GoalCheckout(work_dir).head_ref()
        if ref != branch_ref(record.branch):
            return (
                f"the checkout is on {ref or 'a detached HEAD'}, not Goal Branch {record.branch}"
            )
    except (GoalStoreError, CheckoutError) as exc:
        return f"the checkout cannot be inspected: {exc}"
    return ""


def build_status(
    store: GoalStore,
    record: GoalRecord,
    *,
    work_dir: Path | None = None,
    resolvers: GoalFreshnessResolvers = DEFAULT_RESOLVERS,
) -> GoalStatusView:
    """Load fail-closed, then protected inputs, checkout, evidence revision, and freshness."""
    root = work_dir or Path(record.worktree_path)
    try:
        checkout = GoalCheckout(root).containing_repository()
    except CheckoutError:
        checkout = None  # protected/checkout checks report an inaccessible worktree
    if checkout is not None:
        root = checkout[0]
    state = load_goal_state(store, record)
    if state.slug != record.id:
        raise GoalStoreError(f"Goal state belongs to {state.slug}, not {record.id}")
    protected = protected_warnings(record, store.project_dir, root)
    drift = protected[0] if protected else checkout_violation(store, record, root)
    records = validated_evidence_records(
        GOAL_SCOPE, record_paths(store.project_dir, record.id).logs_dir, state, {}
    )
    latest = {
        payload["criterion"]: payload for payload in records if payload["role"] == "candidate"
    }
    goals = tuple(
        _goal_status(
            goal,
            state.criteria.get(goal.spec.key),
            latest.get(goal.spec.key),
            record,
            root,
            drift,
            resolvers,
        )
        for goal in record.goals
    )
    return GoalStatusView(record, goals, goal_warnings(record, store.project_dir, work_dir=root))


def _goal_status(
    goal: RecordedGoal,
    entry: Any,
    evidence: dict[str, Any] | None,
    record: GoalRecord,
    root: Path,
    drift: str,
    resolvers: GoalFreshnessResolvers,
) -> GoalStatus:
    key = goal.spec.key
    if entry is None or not entry.met:
        metric = (
            "no evidence"
            if entry is None or not entry.detail
            else format_criterion_metric(key, entry)
        )
        return GoalStatus(key, "unmet", metric or "unmet")
    reason = drift or _revision_violation(goal, evidence, record)
    if not reason:
        reason = evaluate_goal_freshness(
            key, entry, goal=goal.spec, work_dir=root, resolvers=resolvers
        ).reason
    if reason:
        return GoalStatus(key, "stale", "evidence is stale", reason)
    return GoalStatus(key, "met", format_criterion_metric(key, entry) or "evidence recorded")


def _revision_violation(
    goal: RecordedGoal, evidence: dict[str, Any] | None, record: GoalRecord
) -> str:
    if evidence is None:
        return "the evidence has no Goal specification identity"
    identity = evidence["goal_identity"]
    current = {item.spec.key: item.spec_revision for item in record.goals}
    if (
        identity["record_id"] != record.id
        or identity["spec_revisions"].get(goal.spec.key) != goal.spec_revision
        or any(
            current.get(key) != revision for key, revision in identity["spec_revisions"].items()
        )
    ):
        return "the evidence was recorded under another Goal specification revision"
    return ""


def status_views(store: GoalStore, work_dir: Path) -> tuple[GoalStatusView, ...]:
    """Show this worktree's occupant, otherwise all occupying records; report corruption."""
    from booley.goals.store import GoalRecordCorruptError

    scan = store.list_active()
    if scan.corrupt:
        raise GoalRecordCorruptError(scan.corrupt)
    record = (
        store.active_for_worktree(work_dir)
        if GoalCheckout(work_dir).containing_repository() is not None
        else None
    )
    records = (record,) if record is not None else scan.records
    return tuple(
        build_status(store, rec, work_dir=work_dir if rec is record else None) for rec in records
    )
