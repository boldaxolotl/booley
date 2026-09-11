"""Advisory interpretation of immutable native Coverage Campaign evidence."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from booley.core.boundary import BoundaryError, require_dict, require_list, require_str
from booley.flows.sim.coverage_analysis_input import (
    CoverageAnalysisError,
    CoverageSourceClosure,
    verified_source_snapshot,
)
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    DurableTargetIdentity,
    FrozenJson,
    decode_coverage_campaign,
    encode_coverage_campaign,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_campaign_store import (
    CAMPAIGN_SCHEMA_V3,
    CoverageCampaignSummary,
)


@dataclass(frozen=True)
class CoverageAnalysisReport:
    """Versioned advisory output, with immutable observations and hypotheses."""

    document: Mapping[str, FrozenJson]

    def to_dict(self) -> dict[str, object]:
        return json.loads(json.dumps(self.document, default=_json_value))


@dataclass(frozen=True)
class CoverageModelResult:
    """Validated-model candidate plus deterministic evidence-tool audit data."""

    response: object
    analysis_scope: Mapping[str, FrozenJson]


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Not JSON data: {type(value).__name__}")


@dataclass(frozen=True)
class _AnalysisEnvelope:
    schema: str
    model_campaign: Mapping[str, object]
    observed_evidence: object


def _analysis_envelope(
    observed: dict[str, object], summary: CoverageCampaignSummary | None
) -> _AnalysisEnvelope:
    storage_schema = "booley.coverage-campaign/v1"
    point_store = None
    manifest = None
    if summary is not None:
        storage_schema = summary.source_schema
        manifest = cast(dict[str, object], _json_value(summary.document))
        point_store = manifest.get("point_store")
    reference = {
        "campaign_reference": {
            "campaign_id": observed["campaign_id"],
            "target": observed["target"],
            "point_count": len(cast(list[object], observed["points"])),
            "storage_schema": storage_schema,
            **({"point_store": point_store} if point_store is not None else {}),
        }
    }
    if summary is None or summary.source_schema != CAMPAIGN_SCHEMA_V3:
        return _AnalysisEnvelope("booley.coverage-analysis/v1", reference, observed)
    assert manifest is not None
    assert summary.point_store is not None
    return _AnalysisEnvelope(
        "booley.coverage-analysis/v2",
        reference,
        {
            "campaign_manifest": manifest,
            "point_store_sha256": summary.point_store.sha256,
        },
    )


class CoverageAnalyzer:
    """Compose analysis with one external, text-only model call."""

    def __init__(self, model: Callable[[str], object]) -> None:
        self._model = model

    def analyze_coverage_campaign(
        self,
        campaign: CoverageCampaign,
        sources: CoverageSourceClosure | None,
        instruction: str,
        *,
        summary: CoverageCampaignSummary | None = None,
    ) -> CoverageAnalysisReport:
        observed = encode_coverage_campaign(campaign)
        decode_coverage_campaign(observed, DurableTargetIdentity(campaign.target.identity))
        envelope = _analysis_envelope(observed, summary)
        eligibility, limitations = _eligibility(campaign)
        sources = verified_source_snapshot(campaign, sources)
        if sources is None:
            limitations.append(
                "Sources unavailable, unsafe, or stale; analysis uses normalized report evidence only."
            )
        response, analysis_scope = _model_response(
            self._model(_analysis_prompt(envelope, sources, instruction)), campaign
        )
        response = _validate_response(response, campaign)
        response["waiver_candidates"] = _screen_candidates(
            response["waiver_candidates"], campaign, sources
        )
        report = {
            "$schema": envelope.schema,
            "campaign_id": campaign.campaign_id,
            "target": observed["target"],
            "eligibility": eligibility,
            "source_access": "report_only" if sources is None else "verified",
            "limitations": limitations,
            "closure_recommendation": _RECOMMENDATIONS[str(campaign.evaluation["status"])],
            "observed_evidence": envelope.observed_evidence,
            **response,
        }
        if analysis_scope is not None:
            report["analysis_scope"] = analysis_scope
        return CoverageAnalysisReport(freeze_coverage_mapping(report))


def _analysis_prompt(
    envelope: _AnalysisEnvelope,
    sources: CoverageSourceClosure | None,
    instruction: str,
) -> str:
    return json.dumps(
        {
            **envelope.model_campaign,
            "evidence_access": (
                "Use the coverage_evidence tool for overview, points, and verified source "
                "excerpts. Begin with overview and retrieve only evidence needed for exact "
                "point_ids."
            ),
            "source_access": "verified" if sources is not None else "report_only",
            "instruction": instruction,
        },
        default=_json_value,
    )


def _model_response(
    model_result: object, campaign: CoverageCampaign
) -> tuple[object, dict[str, object] | None]:
    if not isinstance(model_result, CoverageModelResult):
        return model_result, None
    scope = {
        "total_points": len(campaign.points),
        "points_retrieved": 0,
        "point_ids": [],
        "queries": [],
        **model_result.analysis_scope,
    }
    return model_result.response, scope


_RECOMMENDATIONS = {
    "pass": "coverage_ready",
    "fail": "coverage_not_ready",
    "blocked": "coverage_evidence_blocked",
    "not_requested": "ungated_no_recommendation",
}


def _eligibility(campaign: CoverageCampaign) -> tuple[str, list[str]]:
    if campaign.collector.native_format.get("compatibility") != "compatible":
        raise CoverageAnalysisError("Incompatible native evidence cannot be analyzed")
    if campaign.collection["status"] not in {"complete", "incomplete"}:
        raise CoverageAnalysisError("Campaign collection is not terminal")
    if campaign.normalization["status"] not in {
        "complete",
        "complete_with_unknown_records",
        "partial",
    }:
        raise CoverageAnalysisError("Campaign normalization is not usable")
    if not campaign.points:
        raise CoverageAnalysisError("Campaign has no normalized Coverage Points")
    limitations = []
    if (
        campaign.collection["status"] != "complete"
        or campaign.normalization["status"] == "partial"
    ):
        limitations.append("Collection is incomplete; gaps may reflect missing evidence.")
    if campaign.normalization.get("unrecognized_records"):
        limitations.append("Unknown native records are retained but cannot be interpreted.")
    return ("eligible_with_limits" if limitations else "eligible"), limitations


def _validate_response(value: object, campaign: CoverageCampaign) -> dict[str, object]:
    try:
        response = require_dict(value)
        if set(response) != {"hypotheses", "recommendations", "waiver_candidates"}:
            raise CoverageAnalysisError(
                "Model must return only hypotheses, recommendations and waiver_candidates"
            )
        points = {point.id for point in campaign.points}
        for category, text_key in (("hypotheses", "explanation"), ("recommendations", "action")):
            for item in require_list(response[category]):
                record = require_dict(item)
                if set(record) != {"point_ids", text_key}:
                    raise CoverageAnalysisError(f"Malformed model {category}")
                require_str(record, text_key)
                references = require_list(record["point_ids"])
                if any(
                    not isinstance(reference, str) or reference not in points
                    for reference in references
                ):
                    raise CoverageAnalysisError("Model referenced an unknown Coverage Point")
        require_list(response["waiver_candidates"])
        return dict(response)
    except BoundaryError as exc:
        raise CoverageAnalysisError(f"Malformed model response: {exc}") from exc


def _screen_candidates(
    value: object, campaign: CoverageCampaign, sources: CoverageSourceClosure | None
) -> list[dict[str, object]]:
    points = {
        point["id"]: point
        for point in cast(list[dict[str, Any]], encode_coverage_campaign(campaign)["points"])
    }
    rtl = {
        record["path"]: record["sha256"]
        for record in cast(tuple[Mapping[str, str], ...], campaign.source_closure["rtl"])
    }
    screened = []
    seen = set()
    for value_item in require_list(value):
        try:
            item = require_dict(value_item)
            if set(item) != {"point_id", "reason", "evidence", "proof_reference"}:
                raise CoverageAnalysisError("Malformed waiver candidate")
            if any(not isinstance(value, str) for value in item.values()):
                raise CoverageAnalysisError("Candidate fields must be strings")
        except BoundaryError as exc:
            raise CoverageAnalysisError(f"Malformed waiver candidate: {exc}") from exc
        point = points.get(item["point_id"])
        screening, detail = _candidate_screen(item, point, rtl, sources)
        if item["point_id"] in seen:
            screening, detail = "forbidden", "Duplicate candidate for the same exact point"
        seen.add(item["point_id"])
        screened.append(
            {
                **item,
                "target_identity": campaign.target.identity,
                "point_identity": point["identity"] if point else None,
                "source_fingerprint": rtl.get(point["identity"]["location"]["source"])
                if point
                else None,
                "screening": screening,
                "screening_reason": detail,
                "approval": "not_approved",
            }
        )
    return screened


def _candidate_screen(item, point, rtl, sources) -> tuple[str, str]:
    if point is None or point["identity"]["location"]["source"] not in rtl:
        return "forbidden", "Candidate must identify an exact RTL Coverage Point"
    if point["disposition"]["kind"] != "eligible" or item["reason"] not in {
        "excluded",
        "unreachable",
    }:
        return "forbidden", "Point is not eligible or reason is not an allowed approval reason"
    if sources is None or not item["evidence"].strip():
        return (
            "investigate",
            "Verified sources and supporting evidence are required for human review",
        )
    if item["reason"] == "unreachable" and (
        not item["proof_reference"].strip() or point["hits_by_run"]
    ):
        return (
            "investigate",
            "Unreachability requires a proof reference and no contradictory observed hits",
        )
    return (
        "ready_for_human_review",
        "Advisory only; a human must verify evidence and author any approval",
    )
