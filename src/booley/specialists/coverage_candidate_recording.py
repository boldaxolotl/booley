"""Record a Coverage Analyst run's screened Waiver Candidates for its Ticket (ADR 0066).

The Analyst's model never writes. After the deterministic screen, this adapter
binds each ``ready_for_human_review`` candidate to the exact persisted Campaign
and records it in the Ticket's candidate store. It then reports the strict and
Provisional Coverage Verdicts for that Campaign, so the Developer sees both.

Recording is best-effort: a store failure is reported, never raised, because
the advisory analysis itself succeeded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from booley.core.boundary import BoundaryError
from booley.flows.sim.coverage_campaign import CoverageCampaign
from booley.flows.sim.coverage_campaign_store import (
    CAMPAIGN_SCHEMA_V3,
    CAMPAIGN_SCHEMA_V4,
    CoverageCampaignSummary,
)
from booley.flows.sim.coverage_provisional import (
    ProvisionalCandidate,
    ProvisionalCoverageError,
    criterion_from_evaluation,
    evaluate_provisional_coverage,
)
from booley.ticket_board import waiver_candidates as store
from booley.ticket_board.helpers import tickets_dir_from_project_root


@dataclass(frozen=True)
class TicketCandidateContext:
    """Where one Ticket's candidates live and how Campaign paths are anchored."""

    slug: str
    tickets_dir: Path
    flow_reports_root: Path

    @classmethod
    def from_environment(
        cls, slug: str, environment: Mapping[str, str]
    ) -> TicketCandidateContext | None:
        """Return the Ticket context, or None outside Ticket Mode."""
        control_root = environment.get("BOOLEY_CONTROL_PROJECT_ROOT", "")
        runtime_dir = environment.get("BOOLEY_RUNTIME_DIR", "")
        if not slug or not control_root or not runtime_dir:
            return None
        return cls(
            slug,
            tickets_dir_from_project_root(control_root),
            Path(runtime_dir) / "flow-reports",
        )


def _binding(
    campaign: CoverageCampaign,
    summary: CoverageCampaignSummary | None,
    campaign_path: Path,
    flow_reports_root: Path,
) -> store.CampaignBinding:
    if summary is None or summary.source_schema not in {CAMPAIGN_SCHEMA_V3, CAMPAIGN_SCHEMA_V4}:
        raise ValueError("only point-store Campaigns (V3/V4) can bind Waiver Candidates")
    assert summary.point_store is not None  # V3/V4 always carry a point store
    try:
        relative = campaign_path.absolute().relative_to(flow_reports_root.absolute())
    except ValueError as exc:
        raise ValueError("Campaign is not under this Ticket's flow-reports directory") from exc
    return store.CampaignBinding(
        campaign_id=campaign.campaign_id,
        manifest_sha256=summary.manifest_sha256,
        point_store_sha256=summary.point_store.sha256,
        campaign_path=relative.as_posix(),
        target_identity=campaign.target.identity,
        target_selector=campaign.target.selector,
    )


def _proposals(
    screened: Sequence[Mapping[str, object]], campaign: CoverageCampaign
) -> list[store.WaiverProposal]:
    """Turn the ready-for-review screen results into store proposals."""
    points = {point.id: point for point in campaign.points}
    proposals = []
    for item in screened:
        if item.get("screening") != "ready_for_human_review":
            continue
        point = points[str(item["point_id"])]
        proposals.append(
            store.WaiverProposal(
                point_id=point.id,
                source=str(point.identity.location["source"]),
                source_sha256=str(item["source_fingerprint"]),
                reason=str(item["reason"]),
                justification=str(item["evidence"]),
                proof_reference=str(item["proof_reference"]),
            )
        )
    return proposals


def _provisional_block(
    campaign: CoverageCampaign, binding: store.CampaignBinding, record: store.CandidateRecord
) -> dict[str, object]:
    """Strict and provisional verdicts over every candidate recorded for this Campaign."""
    candidates = [
        ProvisionalCandidate(
            candidate_id=item.candidate_id,
            campaign_id=item.binding.campaign_id,
            target_identity=item.binding.target_identity,
            point_id=item.proposal.point_id,
            source=item.proposal.source,
            source_sha256=item.proposal.source_sha256,
            reason=item.proposal.reason,
        )
        for item in record.candidates.values()
        if item.binding == binding
    ]
    strict = str(campaign.evaluation.get("status"))
    if strict not in {"pass", "fail"}:
        return {"strict": strict, "provisional": strict, "candidates": []}
    verdict = evaluate_provisional_coverage(
        campaign, criterion_from_evaluation(campaign), candidates
    )
    return {
        "strict": verdict.strict_status,
        "provisional": verdict.status,
        "candidates": [
            {
                "candidate_id": screen.candidate_id,
                "status": screen.status,
                "needed": screen.needed,
                "reason": screen.reason,
            }
            for screen in verdict.screens
        ],
    }


def record_ticket_candidates(
    context: TicketCandidateContext,
    campaign: CoverageCampaign,
    summary: CoverageCampaignSummary | None,
    campaign_path: Path,
    screened: Sequence[Mapping[str, object]],
    *,
    invocation_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Record screened candidates and return the report's candidate section."""
    try:
        binding = _binding(campaign, summary, campaign_path, context.flow_reports_root)
        outcome = store.record_proposals(
            context.tickets_dir,
            context.slug,
            binding,
            _proposals(screened, campaign),
            invocation_id=invocation_id,
            now=now or datetime.now(UTC),
        )
        record = store.load(context.tickets_dir, context.slug)
        verdicts = _provisional_block(campaign, binding, record)
    except (
        OSError,
        ValueError,
        BoundaryError,
        ProvisionalCoverageError,
        store.WaiverCandidateRecordError,
        store.WaiverCandidateLockError,
    ) as exc:
        return {"status": "failed", "reason": str(exc)}
    return {
        "status": "recorded",
        "recorded": outcome.recorded,
        "filtered_by_rejection": outcome.filtered_by_rejection,
        "candidate_ids": list(outcome.candidate_ids),
        "verdicts": verdicts,
    }


def candidate_summary_line(section: Mapping[str, object]) -> str:
    """One Console line describing the candidate section."""
    if section.get("status") != "recorded":
        return f"Waiver Candidates not recorded: {section.get('reason', 'unknown reason')}"
    verdicts = section["verdicts"]
    assert isinstance(verdicts, Mapping)
    return (
        f"Waiver Candidates recorded {section['recorded']} "
        f"(filtered by rejection {section['filtered_by_rejection']}); "
        f"coverage strict {verdicts['strict']} · provisional {verdicts['provisional']}"
    )
