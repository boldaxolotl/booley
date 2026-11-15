"""Record a Coverage Analyst run's screened Waiver Candidates for its Goal Record (ADR 0066).

The Analyst's model never writes. After the deterministic screen, this adapter
binds each ``ready_for_human_review`` candidate to the exact persisted Coverage Record
and records it in the Goal Record's candidate store. It then reports the strict and
Provisional Coverage Verdicts for that Coverage Record, so the Developer Agent sees both.

Recording is best-effort: a store failure is reported, never raised, because
the advisory analysis itself succeeded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from booley.core.boundary import BoundaryError
from booley.flows.execution_persistence import AcceptanceRecorder, CandidateStoreProvider
from booley.flows.sim import waiver_candidates as store
from booley.flows.sim.coverage_provisional import (
    ProvisionalCoverageError,
    criterion_from_evaluation,
    evaluate_provisional_coverage,
)
from booley.flows.sim.coverage_record import CoverageRecord
from booley.flows.sim.coverage_record_store import (
    COVERAGE_RECORD_SCHEMA,
    CoverageRecordSummary,
)
from booley.runtime.sandbox_layout import canonical_project_alias_path


@dataclass(frozen=True)
class GoalCandidateContext:
    """Where one Goal Record's candidates live and how Coverage Record paths are anchored."""

    flow_reports_root: Path
    record_dir: Path

    @classmethod
    def from_recorder(cls, recorder: AcceptanceRecorder) -> GoalCandidateContext | None:
        """Receive paths from the admission owner, never reinterpret environment roots."""
        if isinstance(recorder, CandidateStoreProvider):
            return cls(*recorder.candidate_store_paths())
        return None


def _binding(
    coverage_record: CoverageRecord,
    summary: CoverageRecordSummary | None,
    coverage_record_path: Path,
    flow_reports_root: Path,
) -> store.CoverageRecordBinding:
    if summary is None or summary.source_schema != COVERAGE_RECORD_SCHEMA:
        raise ValueError(
            "only point-store Coverage Records (storage v5) can bind Waiver Candidates"
        )
    assert summary.point_store is not None  # a stored Coverage Record always carries a point store
    try:
        relative = canonical_project_alias_path(coverage_record_path.absolute()).relative_to(
            canonical_project_alias_path(flow_reports_root.absolute())
        )
    except ValueError as exc:
        raise ValueError(
            "Coverage Record is not under this Goal Record's flow-reports directory"
        ) from exc
    return store.CoverageRecordBinding(
        campaign_id=coverage_record.coverage_record_id,
        manifest_sha256=summary.manifest_sha256,
        point_store_sha256=summary.point_store.sha256,
        campaign_path=relative.as_posix(),
        target_identity=coverage_record.target.identity,
        target_selector=coverage_record.target.selector,
    )


def _proposals(
    screened: Sequence[Mapping[str, object]], coverage_record: CoverageRecord
) -> list[store.WaiverProposal]:
    """Turn the ready-for-review screen results into store proposals."""
    points = {point.id: point for point in coverage_record.points}
    proposals: list[store.WaiverProposal] = []
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
    coverage_record: CoverageRecord,
    binding: store.CoverageRecordBinding,
    record: store.CandidateRecord,
) -> dict[str, object]:
    """Strict and provisional verdicts over every candidate recorded for this Coverage Record."""
    candidates = [
        item.as_provisional() for item in record.candidates.values() if item.binding == binding
    ]
    strict = str(coverage_record.evaluation.get("status"))
    if strict not in {"pass", "fail"}:
        return {"strict": strict, "provisional": strict, "candidates": []}
    verdict = evaluate_provisional_coverage(
        coverage_record, criterion_from_evaluation(coverage_record), candidates
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


def record_goal_candidates(
    context: GoalCandidateContext,
    coverage_record: CoverageRecord,
    summary: CoverageRecordSummary | None,
    coverage_record_path: Path,
    screened: Sequence[Mapping[str, object]],
    *,
    invocation_id: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Record screened candidates and return the report's candidate section."""
    try:
        binding = _binding(
            coverage_record, summary, coverage_record_path, context.flow_reports_root
        )
        outcome = store.record_proposals(
            context.record_dir,
            binding,
            _proposals(screened, coverage_record),
            invocation_id=invocation_id,
            now=now or datetime.now(UTC),
        )
        record = store.load(context.record_dir)
        verdicts = _provisional_block(coverage_record, binding, record)
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
    """One summary line describing the candidate section."""
    if section.get("status") != "recorded":
        return f"Waiver Candidates not recorded: {section.get('reason', 'unknown reason')}"
    verdicts = section["verdicts"]
    assert isinstance(verdicts, Mapping)
    return (
        f"Waiver Candidates recorded {section['recorded']} "
        f"(filtered by rejection {section['filtered_by_rejection']}); "
        f"coverage strict {verdicts['strict']} · provisional {verdicts['provisional']}"
    )
