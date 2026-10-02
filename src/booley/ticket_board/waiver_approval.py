"""The Human's per-candidate waiver decisions at ``board approve`` (ADR 0066).

For a Ticket that reached review on a Provisional Coverage Verdict, approval:

1. re-derives the offered candidates from evidence (the record is only checked,
   never trusted) and requires a decision on each, nothing defaulting to accept;
2. predicts the strict verdict by loading the Approved Waiver Set the accepted
   candidates would produce, through the real loader on an overlay copy;
3. on failure, records the rejections and refuses: the Ticket stays in review;
4. on success, writes a write-once promotion plan (the commit point), records
   the rejections, and publishes the strict coverage re-evaluation as new
   Criterion evidence. Normal approval then freezes acceptance for the
   unchanged Ticket heads, and the Acceptance Journal commits the plan in the
   merge candidate, so the waivers reach the destination with their RTL.

A retry before frozen acceptance requires the same explicit Human decisions,
re-derives the candidates, and resumes with the same stamps. A plan alone is
never approval authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.config.coverage_waiver_inputs import parse_coverage_waiver_config
from booley.core.config_paths import resolve_toml
from booley.criteria.state import CriterionChange, CriterionEntry, DevelopmentState
from booley.flows.sim.coverage_campaign import DurableTargetIdentity
from booley.flows.sim.coverage_projection import project_coverage_criterion
from booley.flows.sim.coverage_reference import resolve_persisted_coverage_campaign_reference
from booley.flows.sim.coverage_waiver_application import (
    WaiverPlanError,
    WaiverPromotionPlan,
    evaluation_json,
    load_promoted_waiver_set,
    strict_reevaluation,
)
from booley.flows.sim.coverage_waiver_promotion import (
    ApprovalStamps,
    WaiverCandidateEvidence,
    WaiverPromotionError,
)
from booley.flows.sim.coverage_waivers import (
    CoverageRepositoryRoots,
    CoverageWaiverValidationError,
)
from booley.runtime.incontainer_git_identity import load_git_identity
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.timefmt import rfc3339_from_epoch

from . import waiver_candidates as store
from .acceptance_ledger import read_acceptance, record_changes
from .persistence import atomic_write_once
from .provisional_coverage import (
    CandidateView,
    TicketCoverageContext,
    describe_waiver_candidates,
)

PROMOTION_DETAIL_KEY = "waiver_promotion"


class WaiverDecisionError(ValueError):
    """Waiver decisions are missing, unknown, or cannot produce strict acceptance."""


@dataclass(frozen=True)
class WaiverDecisions:
    """The Human's explicit per-candidate answers from ``board approve``."""

    accepted: frozenset[str] = field(default_factory=frozenset)
    rejected: frozenset[str] = field(default_factory=frozenset)
    approval_ref: str | None = None

    @property
    def given(self) -> bool:
        return bool(self.accepted or self.rejected or self.approval_ref)


def promotion_plan_path(log_dir: Path) -> Path:
    """The write-once record of one approval's accepted candidates."""
    return log_dir / "acceptance" / "waiver-promotion.json"


def read_promotion_plan(log_dir: Path) -> WaiverPromotionPlan | None:
    path = promotion_plan_path(log_dir)
    if not path.exists():
        return None
    try:
        return WaiverPromotionPlan.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise WaiverPlanError(f"waiver promotion plan is unreadable: {exc}") from exc


def promotion_plan_for_completion(log_dir: Path) -> WaiverPromotionPlan | None:
    """The plan the frozen Criteria Satisfaction Record names, verified by digest."""
    plan = read_promotion_plan(log_dir)
    snapshot = read_acceptance(log_dir).snapshot
    recorded = {
        detail[PROMOTION_DETAIL_KEY]["plan_sha256"]
        for entry in (snapshot.criteria.values() if snapshot is not None else ())
        if isinstance(detail := entry.get("detail"), Mapping)
        and isinstance(detail.get(PROMOTION_DETAIL_KEY), Mapping)
    }
    if plan is None and not recorded:
        return None
    if plan is None or recorded != {plan.sha256()}:
        raise WaiverPlanError(
            "the waiver promotion plan disagrees with the Criteria Satisfaction Record"
        )
    return plan


@dataclass(frozen=True)
class _ApprovalInputs:
    slug: str
    log_dir: Path
    state_path: Path
    worktree: Path
    project_root: Path
    context: TicketCoverageContext
    inspection: Mapping[str, Any]


def _git_config(checkout: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "config", "--get", key],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def approver_identity(project_root: Path) -> str:
    """``Name <email>`` of the Project checkout, refusing the Developer Agent's identity."""
    name = _git_config(project_root, "user.name")
    email = _git_config(project_root, "user.email")
    if not name or not email:
        raise WaiverDecisionError(
            f"set git user.name and user.email in {project_root} before approving waivers"
        )
    mounted = os.environ.get("BOOLEY_PROJECT_DIR")
    interactive = os.environ.get("BOOLEY_MCP_MODE") == "interactive"
    project_data = (
        Path(mounted) if interactive and mounted else resolve_checkout_project_dir(project_root)
    )
    agent = load_git_identity(project_data)
    if (name, email) == (agent.name, agent.email):
        raise WaiverDecisionError(
            "the Project checkout's Git identity is the Developer Agent's [agent.git] "
            "identity; approve waivers as yourself"
        )
    return f"{name} <{email}>"


def _offered(views: tuple[CandidateView, ...]) -> dict[str, CandidateView]:
    return {view.candidate.candidate_id: view for view in views if view.status == "offered"}


def _validate_decisions(decisions: WaiverDecisions, views: tuple[CandidateView, ...]) -> None:
    """Every offered candidate decided exactly once; nothing else named."""
    offered = _offered(views)
    known = {view.candidate.candidate_id: view for view in views}
    both = decisions.accepted & decisions.rejected
    if both:
        raise WaiverDecisionError(f"accepted and rejected at once: {', '.join(sorted(both))}")
    for candidate_id in sorted((decisions.accepted | decisions.rejected) - set(offered)):
        view = known.get(candidate_id)
        why = f"it is {view.status}: {view.reason}" if view else "no such candidate"
        raise WaiverDecisionError(f"{candidate_id} cannot be decided ({why})")
    undecided = set(offered) - decisions.accepted - decisions.rejected
    if undecided:
        raise WaiverDecisionError(
            "decide every offered Waiver Candidate with --accept-waivers or "
            f"--reject-waivers; undecided: {', '.join(sorted(undecided))}"
        )


def _evidence(view: CandidateView) -> WaiverCandidateEvidence:
    item = view.candidate
    return WaiverCandidateEvidence(
        waiver_id=item.candidate_id,
        campaign_id=item.binding.campaign_id,
        manifest_sha256=item.binding.manifest_sha256,
        point_store_sha256=item.binding.point_store_sha256,
        target=item.binding.target_identity,
        point_id=item.proposal.point_id,
        source=item.proposal.source,
        source_sha256=item.proposal.source_sha256,
        reason=item.proposal.reason,  # type: ignore[arg-type]
        justification=item.proposal.justification,
        evidence_refs=item.proposal.evidence_refs,
        proposal_count=item.proposal_count,
    )


def _now() -> str:
    return rfc3339_from_epoch(datetime.now(UTC).timestamp())


def _build_plan(
    inputs: _ApprovalInputs, views: tuple[CandidateView, ...], decisions: WaiverDecisions
) -> WaiverPromotionPlan:
    config_path = resolve_toml(resolve_checkout_project_dir(inputs.worktree))
    config = parse_coverage_waiver_config(
        tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    )
    if config is None:
        raise WaiverDecisionError("[coverage.waivers] is not configured; nothing can be promoted")
    capture = str(inputs.inspection.get("capture_sha") or "")
    stamps = ApprovalStamps(
        approved_by=approver_identity(inputs.project_root),
        approved_at=_now(),
        approval_ref=decisions.approval_ref or f"ticket:{inputs.slug}@{capture}",
    )
    offered = _offered(views)
    return WaiverPromotionPlan(
        config.anchor,
        config.directory,
        stamps,
        tuple(_evidence(offered[item]) for item in sorted(decisions.accepted)),
    )


def _known_targets(worktree: Path) -> tuple[DurableTargetIdentity, ...]:
    from booley.targets.catalog import TargetCatalog

    return tuple(
        DurableTargetIdentity(item.identity) for item in TargetCatalog.build(worktree).list()
    )


def _strict_changes(
    inputs: _ApprovalInputs, state: DevelopmentState, plan: WaiverPromotionPlan
) -> list[CriterionChange]:
    """Re-evaluate every Coverage Criterion whose Campaign an accepted candidate binds."""
    roots = CoverageRepositoryRoots(
        rtl_repository=inputs.worktree,
        project_data_repository=resolve_checkout_project_dir(inputs.worktree),
    )
    with tempfile.TemporaryDirectory(prefix="booley-waiver-approval-") as overlay:
        waivers = load_promoted_waiver_set(
            plan, roots, Path(overlay), _known_targets(inputs.worktree)
        )
    promotion = {"plan_sha256": plan.sha256(), "waiver_ids": list(plan.waiver_ids)}
    campaigns = {item.campaign_id for item in plan.candidates}
    changes: list[CriterionChange] = []
    for key, entry in sorted(state.criteria.items()):
        reference = (entry.detail or {}).get("coverage_campaign_reference")
        if not key.startswith("coverage_") or not isinstance(reference, Mapping):
            continue
        resolved = resolve_persisted_coverage_campaign_reference(
            inputs.context.flow_reports_root, reference
        )
        if resolved.loaded.campaign.campaign_id not in campaigns:
            continue
        evaluated = strict_reevaluation(resolved.loaded.campaign, waivers)
        changes.extend(_criterion_change(state, key, entry, evaluated, promotion))
    return changes


def _criterion_change(
    state: DevelopmentState,
    key: str,
    entry: CriterionEntry,
    evaluated: Any,
    promotion: Mapping[str, Any],
) -> list[CriterionChange]:
    evaluation = evaluation_json(evaluated)
    detail = {
        **(entry.detail or {}),
        "evaluation": evaluation,
        "criterion_fingerprint": evaluation.get("criterion_fingerprint"),
        PROMOTION_DETAIL_KEY: dict(promotion),
    }
    met, projected = project_coverage_criterion(
        entry, evaluation, detail, atomic="criterion_metric" in detail
    )
    return state.set_criterion(key, met, detail=projected)


def _unmet_mandatory(state: DevelopmentState) -> list[str]:
    return sorted(
        key
        for key, entry in state.criteria.items()
        if not key.startswith("_") and entry.mandatory and not entry.met
    )


def _rejections(
    views: tuple[CandidateView, ...], rejected: frozenset[str], approver: str
) -> list[store.Rejection]:
    stamp = _now()
    return [
        store.Rejection(
            view.candidate.binding.target_identity,
            view.candidate.proposal.point_id,
            view.candidate.proposal.source_sha256,
            stamp,
            approver,
        )
        for view in views
        if view.candidate.candidate_id in rejected
    ]


def _publish(
    inputs: _ApprovalInputs, state: DevelopmentState, changes: list[CriterionChange], plan_sha: str
) -> None:
    """Record the strict re-evaluation as evidence, then the mutable projection.

    Like a Flow publication, the evidence counts only once its transaction is
    selected in the state (``acceptance_transactions``).
    """
    transaction = hashlib.sha256(plan_sha.encode()).hexdigest()
    record_changes(
        inputs.log_dir,
        state,
        changes,
        invocation_id=f"waiver-approval:{plan_sha}",
        producer="waiver-approval",
        execution_id=str(inputs.inspection["execution_id"]),
        ticket_identity=inputs.inspection.get("ticket_identity"),
        transaction_id=transaction,
    )
    if transaction not in state.acceptance_transactions:
        state.acceptance_transactions.append(transaction)
    state.save()


def _already_published(state: DevelopmentState, plan: WaiverPromotionPlan) -> bool:
    return any(
        (entry.detail or {}).get(PROMOTION_DETAIL_KEY, {}).get("plan_sha256") == plan.sha256()
        for entry in state.criteria.values()
    )


def _resume(
    inputs: _ApprovalInputs, plan: WaiverPromotionPlan, decisions: WaiverDecisions
) -> None:
    """Finish an approval interrupted after its plan became durable."""
    if not decisions.given:
        raise WaiverDecisionError("retry unfinished approval with explicit --accept-waivers")
    if decisions.accepted != set(plan.waiver_ids) or decisions.rejected:
        raise WaiverDecisionError(
            "waivers were already approved as "
            f"{', '.join(plan.waiver_ids)}; repeat those explicit --accept-waivers decisions"
        )
    _validate_resumed_plan(inputs, plan, decisions)
    state = DevelopmentState.load(inputs.state_path)
    if _already_published(state, plan):
        return
    changes = _strict_changes(inputs, state, plan)
    _publish(inputs, state, changes, plan.sha256())


def _validate_resumed_plan(
    inputs: _ApprovalInputs, plan: WaiverPromotionPlan, decisions: WaiverDecisions
) -> None:
    """Re-derive a Sandbox-writable plan from the reviewed state and current sources."""
    raw = inputs.inspection.get("state", {}).get("criteria")
    if not isinstance(raw, Mapping):
        raise WaiverDecisionError("reviewed Criteria are required to retry waiver approval")
    criteria = {key: CriterionEntry.from_dict(value) for key, value in raw.items()}
    views, _ = describe_waiver_candidates(criteria, inputs.context)
    _validate_decisions(decisions, views)
    derived = _build_plan(inputs, views, decisions)
    if (
        derived.candidates != plan.candidates
        or derived.config != plan.config
        or derived.stamps.approved_by != plan.stamps.approved_by
        or derived.stamps.approval_ref != plan.stamps.approval_ref
    ):
        raise WaiverDecisionError("waiver promotion plan disagrees with the reviewed candidates")


def apply_waiver_decisions(
    inputs: _ApprovalInputs, decisions: WaiverDecisions, *, merge: bool
) -> WaiverPromotionPlan | None:
    """Validate, predict, and record one provisional approval (see module docstring)."""
    existing = read_promotion_plan(inputs.log_dir)
    if existing is not None:
        _resume(inputs, existing, decisions)
        return existing
    state = DevelopmentState.load(inputs.state_path)
    views, _digest = describe_waiver_candidates(state.criteria, inputs.context)
    _validate_decisions(decisions, views)
    if decisions.accepted and not merge:
        raise WaiverDecisionError("accepting Waiver Candidates requires merge (drop --no-merge)")
    approver = approver_identity(inputs.project_root)
    rejections = _rejections(views, decisions.rejected, approver)
    if not decisions.accepted:
        store.record_rejections(inputs.context.tickets_dir, inputs.slug, rejections)
        raise WaiverDecisionError(_still_unmet(_unmet_mandatory(state)))
    try:
        plan = _build_plan(inputs, views, decisions)
        changes = _strict_changes(inputs, state, plan)
    except (WaiverPlanError, WaiverPromotionError, CoverageWaiverValidationError) as exc:
        store.record_rejections(inputs.context.tickets_dir, inputs.slug, rejections)
        raise WaiverDecisionError(f"accepted waivers cannot be promoted: {exc}") from exc
    unmet = _unmet_mandatory(state)
    store.record_rejections(inputs.context.tickets_dir, inputs.slug, rejections)
    if unmet:
        raise WaiverDecisionError(_still_unmet(unmet))
    atomic_write_once(promotion_plan_path(inputs.log_dir), plan.to_bytes())
    _publish(inputs, state, changes, plan.sha256())
    return plan


def _still_unmet(unmet: list[str]) -> str:
    return (
        f"strict acceptance would still fail ({', '.join(unmet) or 'no change'}); rejections "
        "are recorded and nothing was promoted. The Ticket stays in review: fix here, "
        "reset, or archive."
    )


def approval_inputs(
    tio: Any, slug: str, inspection: Mapping[str, Any], worktree: Path
) -> _ApprovalInputs:
    """Resolve where one Ticket's approval reads and writes."""
    log_dir = tio.logs_dir / slug
    return _ApprovalInputs(
        slug=slug,
        log_dir=log_dir,
        state_path=log_dir / ".runtime" / "booley_state.json",
        worktree=worktree,
        project_root=Path(tio._project_root),
        context=TicketCoverageContext(slug, tio.tickets_dir, log_dir, worktree),
        inspection=inspection,
    )


__all__ = [
    "WaiverDecisionError",
    "WaiverDecisions",
    "apply_waiver_decisions",
    "approval_inputs",
    "approver_identity",
    "promotion_plan_for_completion",
    "promotion_plan_path",
    "read_promotion_plan",
]
