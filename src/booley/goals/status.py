"""Read declared Goals with protected, checkout, specification, and source freshness checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from booley.criteria.evidence_ledger import validated_evidence_records
from booley.goals.apply_barrier import pending_applies
from booley.goals.change_policy import conflict
from booley.goals.checkout import CheckoutError, GoalCheckout, branch_ref
from booley.goals.format import format_criterion_metric
from booley.goals.freshness import (
    DEFAULT_RESOLVERS,
    GoalFreshnessResolvers,
    evaluate_goal_freshness,
)
from booley.goals.model import GoalRecord, RecordedGoal
from booley.goals.paths import record_paths
from booley.goals.proposals import ProposalView, list_proposals
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
    proposals: tuple[ProposalView, ...] = ()
    proposal_conflicts: tuple[tuple[str, str], ...] = ()
    interrupted_applies: tuple[str, ...] = ()

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
    """Read one coherent record/state/lifecycle projection without recovering it."""
    with store.record_lock(record.id):
        return _build_status(store, store.load(record.id), work_dir=work_dir, resolvers=resolvers)


def _build_status(
    store: GoalStore,
    record: GoalRecord,
    *,
    work_dir: Path | None,
    resolvers: GoalFreshnessResolvers,
) -> GoalStatusView:
    root = work_dir or Path(record.worktree_path)
    try:
        checkout = GoalCheckout(root).containing_repository()
    except CheckoutError:
        checkout = None  # protected/checkout checks report an inaccessible worktree
    if checkout is not None:
        root = checkout[0]
    paths = record_paths(store.project_dir, record.id)
    proposals = list_proposals(paths.root)
    interrupted = pending_applies(paths.root)
    state = load_goal_state(store, record)
    if state.slug != record.id:
        raise GoalStoreError(f"Goal state belongs to {state.slug}, not {record.id}")
    protected = protected_warnings(record, store.project_dir, root)
    drift = ("Goal apply is interrupted; recovery is required" if interrupted else "") or (
        protected[0] if protected else checkout_violation(store, record, root)
    )
    latest = _latest_evidence(paths.logs_dir, state)
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
    return GoalStatusView(
        record,
        goals,
        goal_warnings(record, store.project_dir, work_dir=root),
        sum(view.state == "pending" and not view.closed_by_abandonment for view in proposals),
        proposals,
        _proposal_conflicts(proposals, record),
        interrupted,
    )


def _proposal_conflicts(
    proposals: tuple[ProposalView, ...], record: GoalRecord
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (view.proposal.id, conflict(view.proposal, record))
        for view in proposals
        if view.state == "pending"
        and not view.closed_by_abandonment
        and conflict(view.proposal, record)
    )


def _latest_evidence(logs_dir: Path, state: Any) -> dict[str, dict[str, Any]]:
    records = validated_evidence_records(GOAL_SCOPE, logs_dir, state, {})
    return {payload["criterion"]: payload for payload in records if payload["role"] == "candidate"}


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
    projection = "" if entry is None else _projection_violation(entry, evidence)
    if entry is not None and projection:
        return GoalStatus(
            key,
            "stale" if entry.met else "unmet",
            "state conflicts with immutable evidence",
            projection,
        )
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


def _projection_violation(entry: Any, evidence: dict[str, Any] | None) -> str:
    if evidence is not None and (
        entry.met != evidence["met"]
        or (entry.params or {}) != evidence["params"]
        or entry.detail != evidence["detail"]
    ):
        return "Goal state differs from its selected immutable producer observation"
    return ""


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
