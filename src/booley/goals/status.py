"""Read declared Goals with protected, checkout, specification, and source freshness checks."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from booley.core.boundary import as_str
from booley.criteria.evidence_ledger import AcceptanceLedgerError
from booley.criteria.state import DevelopmentState
from booley.flows.campaign_results import CampaignResultsReader
from booley.goals import campaign_accounting
from booley.goals.apply_barrier import pending_applies
from booley.goals.change_policy import conflict
from booley.goals.checkout import CheckoutError, GoalCheckout, branch_ref
from booley.goals.evidence_projection import selected_candidates
from booley.goals.format import format_criterion_metric, format_met_goal_metric
from booley.goals.freshness import (
    DEFAULT_RESOLVERS,
    GoalFreshnessResolvers,
    evaluate_goal_freshness,
)
from booley.goals.model import GoalFamily, GoalRecord, RecordedGoal
from booley.goals.paths import record_paths
from booley.goals.presentation import GoalPresentation, goal_presentation
from booley.goals.proposals import ProposalView, list_proposals
from booley.goals.state_store import load_goal_state_and_evidence
from booley.goals.store import GoalStore, GoalStoreError
from booley.goals.synth_baseline import is_relative_synth
from booley.goals.test_run_freezing import TargetPlans, ensure_test_run_resolutions
from booley.goals.test_run_goals import names_test_runs
from booley.goals.test_run_status import TestRunJudgement, judge_test_runs
from booley.goals.warnings import goal_warnings, protected_warnings
from booley.runtime.git_facts import repository_facts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoalStatus:
    """One declared Goal, including missing evidence."""

    key: str
    status: Literal["met", "unmet", "stale"]
    evidence_summary: str
    reason: str = ""
    #: The Goal's frozen Test Runs with their outcomes (ADR 0072); ``None``
    #: for a Goal that is not judged by Test Runs.
    test_runs: TestRunJudgement | None = None
    #: The Goal state differs from its selected immutable observation.
    state_conflict: bool = False
    presentation: GoalPresentation | None = None
    sim_transitions: tuple[dict[str, Any], ...] | None = None
    #: ``{"status", "reason"}`` of a relative SYNTH Goal's baseline Job, else ``None``.
    synth_baseline: dict[str, str] | None = None


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


@dataclass(frozen=True)
class GoalStatusSelection:
    """Selected views and whether they represent the Project-wide fallback."""

    views: tuple[GoalStatusView, ...]
    project_wide: bool


@repository_facts()
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
    observational: bool = False,
    campaign_reader: CampaignResultsReader | None = None,
) -> GoalStatusView:
    """Read one coherent record/state/lifecycle projection without recovering it."""
    with store.record_lock(record.id, existing_only=observational):
        return _build_status(
            store,
            store.load(record.id),
            work_dir=work_dir,
            resolvers=resolvers,
            campaign_reader=campaign_reader,
        )


@repository_facts()
def _build_status(
    store: GoalStore,
    record: GoalRecord,
    *,
    work_dir: Path | None,
    resolvers: GoalFreshnessResolvers,
    campaign_reader: CampaignResultsReader | None,
) -> GoalStatusView:
    root = work_dir or Path(record.worktree_path)
    try:
        checkout = GoalCheckout(root).containing_repository()
    except CheckoutError:
        checkout = None  # protected/checkout checks report an inaccessible worktree
    if checkout is not None:
        root = checkout[0]
    paths = record_paths(store.booley_dir, record.id)
    proposals = list_proposals(paths.root)
    interrupted = pending_applies(paths.root)
    state, history = load_goal_state_and_evidence(store, record)
    if state.slug != record.id:
        raise GoalStoreError(f"Goal state belongs to {state.slug}, not {record.id}")
    protected = protected_warnings(record, store.booley_dir, root)
    drift = ("Goal apply is interrupted; recovery is required" if interrupted else "") or (
        protected[0] if protected else checkout_violation(store, record, root)
    )
    unaccounted = campaign_accounting.unaccounted_goals(
        store, record, state, history, campaign_reader
    )
    goals = _goal_statuses(record, state, history, store, root, drift, resolvers, unaccounted)
    return GoalStatusView(
        record,
        goals,
        goal_warnings(record, store.booley_dir, work_dir=root),
        sum(view.state == "pending" and not view.closed_by_abandonment for view in proposals),
        proposals,
        _proposal_conflicts(proposals, record),
        interrupted,
    )


def _goal_statuses(
    record: GoalRecord,
    state: DevelopmentState,
    history: list[dict[str, Any]],
    store: GoalStore,
    root: Path,
    drift: str,
    resolvers: GoalFreshnessResolvers,
    unaccounted: Mapping[str, str],
) -> tuple[GoalStatus, ...]:
    """Every Goal judged by its selected observation, then by its frozen Test Runs.

    A Goal named in *unaccounted* has durable evidence that is not in its
    ledger yet (ADR 0072): it is unmet with that reason whatever the ledger
    says, because what is missing may be a failure.
    """
    latest = selected_candidates(record, history)
    plans = TargetPlans(root)
    statuses: list[GoalStatus] = []
    for goal in record.goals:
        key = goal.spec.key
        evidence = latest.get(key)
        entry = state.criteria.get(key)
        status = _goal_status(goal, entry, evidence, record, root, drift, resolvers)
        status = _with_test_runs(status, goal, record, evidence, history, plans)
        status = replace(status, presentation=goal_presentation(goal.spec, _entry_facts(entry)))
        if key in unaccounted and (status.status != "unmet" or evidence is None):
            status = replace(
                status,
                status="unmet",
                evidence_summary="evidence is incomplete",
                reason=unaccounted[key],
            )
        if goal.spec.family is GoalFamily.SIM:
            from booley.goals.sim_transitions import goal_transitions

            transitions = goal_transitions(
                store.booley_dir, record, goal, evidence, history, root, resolvers
            )
            status = replace(status, sim_transitions=tuple(transitions))
        if is_relative_synth(goal):
            from booley.goals.sim_baseline_store import SYNTH_BASELINE
            from booley.goals.sim_transitions import baseline_observations

            seen = baseline_observations(store.booley_dir, record, goal, SYNTH_BASELINE)
            status = replace(
                status,
                synth_baseline={"status": seen["status"], "reason": str(seen.get("reason", ""))},
            )
        statuses.append(status)
    return tuple(statuses)


def _entry_facts(entry: Any) -> dict[str, Any] | None:
    return None if entry is None else entry.to_dict()


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


def _with_test_runs(
    status: GoalStatus,
    goal: RecordedGoal,
    record: GoalRecord,
    evidence: dict[str, Any] | None,
    history: list[dict[str, Any]],
    plans: TargetPlans,
) -> GoalStatus:
    """*status* judged by the Goal's frozen Test Runs as well (ADR 0072).

    A Goal that is judged by Test Runs is met only when they allow it; when
    they do not, it is unmet with the named reason. Stale evidence keeps its
    own reason unless the frozen runs themselves can no longer be judged
    (pending, gone or changed): rerunning cannot cure that.
    """
    if not names_test_runs(goal.spec):
        return status
    judgement = judge_test_runs(goal, record, evidence, history, plans)
    stale = status.status == "stale" and not judgement.blocking
    if not judgement.reason or stale:
        return replace(status, test_runs=judgement)
    summary = status.evidence_summary if status.status == "unmet" else "Test Runs do not pass"
    return GoalStatus(status.key, "unmet", summary, judgement.reason, judgement)


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
        violation = entry.detail.get("goal_contract_violation") if entry and entry.detail else None
        reason = drift or as_str(violation, "") or ""
        if evidence is None and not reason:
            producer = {
                GoalFamily.ELAB: "sim mode=elab_only",
                GoalFamily.REVIEW: "Reviewer",
                GoalFamily.CYCLE_COUNT: "sim",
                GoalFamily.COVERAGE: "sim",
                GoalFamily.MUTATION: "mutation_tester",
            }.get(goal.spec.family, goal.spec.family.value.lower())
            reason = (
                f"{goal_presentation(goal.spec).name} has no current immutable observation "
                f"for met, params and detail; rerun {producer} for this Goal"
            )
        return GoalStatus(key, "unmet", metric or "unmet", reason)
    reason = drift or _revision_violation(goal, evidence, record)
    if not reason:
        reason = evaluate_goal_freshness(
            key, entry, goal=goal.spec, work_dir=root, resolvers=resolvers
        ).reason
    if reason:
        return GoalStatus(key, "stale", "evidence is stale", reason)
    return GoalStatus(key, "met", format_met_goal_metric(key, entry))


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


def select_status(
    store: GoalStore, work_dir: Path, *, campaign_reader: CampaignResultsReader | None = None
) -> GoalStatusSelection:
    """Show this worktree's occupant, otherwise all occupying records; report corruption.

    *campaign_reader* reads the finished results of Simulation Campaigns that
    never published; the entry point supplies the Simulation Flow's. Without
    it such campaigns stay unaccounted and their Goals read unmet.
    """
    from booley.goals.store import GoalRecordCorruptError

    if not work_dir.is_dir():
        raise GoalStoreError(f"Goal worktree missing or unavailable: {work_dir}")
    scan = store.list_active()
    if scan.corrupt:
        raise GoalRecordCorruptError(scan.corrupt)
    found = GoalCheckout(work_dir).containing_repository()
    record = store.active_for_worktree(work_dir) if found is not None else None
    if record is not None and found is not None:
        record = _brought_up_to_date(store, record, found[0], campaign_reader)
    records = (record,) if record is not None else scan.records
    views = tuple(
        build_status(
            store,
            rec,
            work_dir=work_dir if rec is record else None,
            campaign_reader=campaign_reader,
        )
        for rec in records
    )
    return GoalStatusSelection(views, project_wide=record is None)


def _brought_up_to_date(
    store: GoalStore, record: GoalRecord, root: Path, campaign_reader: CampaignResultsReader | None
) -> GoalRecord:
    """Do what a status read of the worktree's own record may write (ADR 0072).

    Two things, each under the record lock and each one atomic step at a
    time: pending Test Run slots that can be resolved now are frozen in
    ``record.json``, so status shows what a run would be judged against; and
    evidence that earlier Simulation Campaigns durably observed but never
    published is brought into the ledger and the Goal state. Neither adds a
    way for a status read to fail: when one of them cannot be done the slots
    read pending, the evidence unaccounted, and the next read tries again.
    The read itself (:func:`build_status`) still takes the record lock and
    raises when it cannot have it, as it always did.
    """
    try:
        record = ensure_test_run_resolutions(store, record.id, root)
    except GoalStoreError as exc:
        logger.warning("Test Runs of Goal Mode %s were not resolved now: %s", record.id, exc)
    try:
        campaign_accounting.account_for_campaigns(store, record.id, reader=campaign_reader)
    except (GoalStoreError, AcceptanceLedgerError) as exc:
        logger.warning("evidence of Goal Mode %s was not accounted for now: %s", record.id, exc)
    return record


def status_views(
    store: GoalStore, work_dir: Path, *, campaign_reader: CampaignResultsReader | None = None
) -> tuple[GoalStatusView, ...]:
    """The status of the worktree's Goal Mode, or of every occupying record.

    Not a pure read for the worktree's own record: see :func:`select_status`,
    which may freeze pending Test Run slots and account for unpublished
    campaign evidence before it reads.
    """
    return select_status(store, work_dir, campaign_reader=campaign_reader).views
