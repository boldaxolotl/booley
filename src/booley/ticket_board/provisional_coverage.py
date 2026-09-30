"""Decide which unmet Coverage Criteria a Ticket meets provisionally (ADR 0066).

A Coverage Criterion that fails strictly may still be met by a Provisional
Coverage Verdict: the Approved Waiver Set plus the Ticket's Waiver Candidates.
Such a Ticket goes to review, never to done, and the Human decides each
candidate there.

The candidate record is Sandbox-writable, so it is checked, not trusted:
- the Campaign is reloaded through the Criterion's authenticated reference and
  must still carry the evaluation the acceptance ledger recorded;
- only candidates bound to exactly that Campaign (path and both digests) count;
- each candidate is re-derived from the Campaign and from the RTL source bytes
  in the Ticket worktree.
Any doubt fails closed: the Criterion is simply not provisionally met.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.criteria.state import CriterionEntry, DevelopmentState
from booley.flows.sim.coverage_policy import _json_value
from booley.flows.sim.coverage_projection import project_coverage_criterion
from booley.flows.sim.coverage_provisional import (
    ProvisionalCandidate,
    ProvisionalCoverageError,
    ProvisionalVerdict,
    criterion_from_evaluation,
    evaluate_provisional_coverage,
)
from booley.flows.sim.coverage_reference import (
    CoverageCampaignReferenceError,
    ResolvedCoverageCampaign,
    resolve_persisted_coverage_campaign_reference,
)
from booley.ticket_board import waiver_candidates as store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProvisionalCriterion:
    """One mandatory Coverage Criterion met only by counting Waiver Candidates."""

    key: str
    campaign_id: str
    reference_path: str
    verdict: ProvisionalVerdict


@dataclass(frozen=True)
class TicketProvisionalCoverage:
    """Every provisionally met Criterion plus the record they were judged from."""

    criteria: tuple[ProvisionalCriterion, ...]
    record_sha256: str

    @property
    def met_keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.criteria)


@dataclass(frozen=True)
class TicketCoverageContext:
    """Where one Ticket's coverage evidence, candidates, and sources live."""

    slug: str
    tickets_dir: Path
    log_dir: Path
    worktree: Path

    @property
    def flow_reports_root(self) -> Path:
        return self.log_dir / ".runtime" / "flow-reports"


def _plain(value: object) -> object:
    return json.loads(json.dumps(_json_value(value)))  # type: ignore[arg-type]


def _source_digest(worktree: Path, source: str) -> str | None:
    path = worktree / source
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _bound_candidates(
    record: store.CandidateRecord, resolved: ResolvedCoverageCampaign, reference_path: str
) -> list[store.WaiverCandidate]:
    """Candidates recorded against exactly this persisted Campaign."""
    summary = resolved.loaded.summary
    point_store = summary.point_store.sha256 if summary.point_store is not None else None
    return [
        item
        for item in record.candidates.values()
        if item.binding.campaign_path == reference_path
        and item.binding.campaign_id == resolved.loaded.campaign.campaign_id
        and item.binding.manifest_sha256 == summary.manifest_sha256
        and item.binding.point_store_sha256 == point_store
    ]


def _as_provisional(item: store.WaiverCandidate) -> ProvisionalCandidate:
    return ProvisionalCandidate(
        candidate_id=item.candidate_id,
        campaign_id=item.binding.campaign_id,
        target_identity=item.binding.target_identity,
        point_id=item.proposal.point_id,
        source=item.proposal.source,
        source_sha256=item.proposal.source_sha256,
        reason=item.proposal.reason,
    )


@dataclass(frozen=True)
class _Judgement:
    """One Criterion's provisional evaluation over its bound candidates."""

    key: str
    met: bool
    campaign_id: str
    reference_path: str
    strict_evaluation: Mapping[str, Any]
    verdict: ProvisionalVerdict
    bound: tuple[store.WaiverCandidate, ...]
    metric: str | None = None  # the one metric an atomic Criterion projects


def _judge_criterion(
    key: str,
    entry: CriterionEntry,
    record: store.CandidateRecord,
    context: TicketCoverageContext,
) -> _Judgement | None:
    """Judge one Coverage Criterion, or return None when no candidate binds to it."""
    detail: Mapping[str, Any] = entry.detail or {}
    reference = detail.get("coverage_campaign_reference")
    if not isinstance(reference, Mapping):
        return None
    resolved = resolve_persisted_coverage_campaign_reference(context.flow_reports_root, reference)
    campaign = resolved.loaded.campaign
    if _plain(campaign.evaluation) != _plain(detail.get("evaluation")):
        raise ProvisionalCoverageError("Campaign evaluation disagrees with acceptance evidence")
    bound = _bound_candidates(record, resolved, str(reference["path"]))
    if "criterion_metric" in detail:
        # An atomic Criterion projects one metric; other metrics' candidates are not its concern.
        metrics = {point.id: point.identity.metric for point in campaign.points}
        bound = [
            item
            for item in bound
            if metrics.get(item.proposal.point_id) == detail["criterion_metric"]
        ]
    if not bound:
        return None
    sources = {item.proposal.source for item in bound}
    current = {
        source: digest
        for source in sources
        if (digest := _source_digest(context.worktree, source)) is not None
    }
    verdict = evaluate_provisional_coverage(
        campaign,
        criterion_from_evaluation(campaign),
        [_as_provisional(item) for item in bound],
        current_sources=current,
    )
    met, _ = project_coverage_criterion(
        entry, verdict.evaluation, detail, atomic="criterion_metric" in detail
    )
    return _Judgement(
        key,
        met and bool(verdict.offered_ids),
        campaign.campaign_id,
        str(reference["path"]),
        campaign.evaluation,
        verdict,
        tuple(bound),
        detail.get("criterion_metric"),
    )


def _judgements(
    criteria: Mapping[str, CriterionEntry],
    keys: Sequence[str],
    record: store.CandidateRecord,
    context: TicketCoverageContext,
) -> list[_Judgement]:
    """Judge each named Coverage Criterion; a failure is logged and skipped."""
    judged: list[_Judgement] = []
    for key in keys:
        entry = criteria.get(key)
        if entry is None or not key.startswith("coverage_"):
            continue
        try:
            outcome = _judge_criterion(key, entry, record, context)
        except (CoverageCampaignReferenceError, ProvisionalCoverageError, OSError) as exc:
            logger.warning("Provisional coverage for %s/%s failed: %s", context.slug, key, exc)
            continue
        if outcome is not None:
            judged.append(outcome)
    return judged


def _record_sha256(record: store.CandidateRecord) -> str:
    return "sha256:" + hashlib.sha256(record.to_bytes()).hexdigest()


def evaluate_ticket_provisional_coverage(
    state: DevelopmentState, unmet: Sequence[str], context: TicketCoverageContext
) -> TicketProvisionalCoverage:
    """Judge each unmet mandatory Criterion against the Ticket's candidates.

    Failures are logged and treated as "not provisionally met": provisional
    acceptance may only ever widen the path to human review, never hide an error.
    """
    try:
        record = store.load(context.tickets_dir, context.slug)
    except store.WaiverCandidateRecordError as exc:
        logger.warning("Waiver Candidate record for %s is unusable: %s", context.slug, exc)
        return TicketProvisionalCoverage((), "")
    met = [
        ProvisionalCriterion(item.key, item.campaign_id, item.reference_path, item.verdict)
        for item in _judgements(state.criteria, unmet, record, context)
        if item.met
    ]
    return TicketProvisionalCoverage(tuple(met), _record_sha256(record))


@dataclass(frozen=True)
class CandidateView:
    """One Waiver Candidate as the review presents it."""

    candidate: store.WaiverCandidate
    status: str
    reason: str
    needed: bool
    criteria: tuple[str, ...]
    metrics: tuple[Mapping[str, Any], ...]


_STATUS_RANK = {"offered": 0, "not_needed": 1, "stale": 2, "invalid": 3}


def _metric_delta(judgement: _Judgement) -> tuple[Mapping[str, Any], ...]:
    strict = {
        row["metric"]: row.get("actual_percent")
        for row in judgement.strict_evaluation.get("metrics", ())
    }
    return tuple(
        {
            "metric": row["metric"],
            "strict_percent": strict.get(row["metric"]),
            "provisional_percent": row.get("actual_percent"),
            "minimum_percent": row.get("minimum_percent"),
        }
        for row in judgement.verdict.evaluation.get("metrics", ())
        if judgement.metric is None or row["metric"] == judgement.metric
    )


def _merge_view(views: dict[str, CandidateView], view: CandidateView) -> None:
    """Keep one row per candidate: best status, any-needed, every Criterion."""
    key = view.candidate.candidate_id
    prior = views.get(key)
    if prior is None:
        views[key] = view
        return
    best = min((prior, view), key=lambda item: _STATUS_RANK[item.status])
    views[key] = CandidateView(
        best.candidate,
        best.status,
        best.reason,
        prior.needed or view.needed,
        tuple(sorted({*prior.criteria, *view.criteria})),
        prior.metrics or view.metrics,
    )


def describe_waiver_candidates(
    criteria: Mapping[str, CriterionEntry], context: TicketCoverageContext
) -> tuple[tuple[CandidateView, ...], str]:
    """Every recorded candidate with its re-derived review status (ADR 0066).

    Candidates bound to no current Criterion's Campaign are stale. Returns the
    views and the record digest they were derived from.
    """
    record = store.load(context.tickets_dir, context.slug)
    views: dict[str, CandidateView] = {}
    for judgement in _judgements(criteria, sorted(criteria), record, context):
        screens = {item.candidate_id: item for item in judgement.verdict.screens}
        for candidate in judgement.bound:
            screen = screens[candidate.candidate_id]
            view = CandidateView(
                candidate,
                screen.status,
                screen.reason,
                screen.needed,
                (judgement.key,),
                _metric_delta(judgement),
            )
            _merge_view(views, view)
    for candidate in record.candidates.values():
        views.setdefault(
            candidate.candidate_id,
            CandidateView(
                candidate,
                "stale",
                "Bound to a Coverage Campaign that is no longer a Criterion's evidence.",
                False,
                (),
                (),
            ),
        )
    ordered = sorted(views.values(), key=lambda item: item.candidate.candidate_id)
    return tuple(ordered), _record_sha256(record)


__all__ = [
    "CandidateView",
    "ProvisionalCriterion",
    "TicketCoverageContext",
    "TicketProvisionalCoverage",
    "describe_waiver_candidates",
    "evaluate_ticket_provisional_coverage",
]
