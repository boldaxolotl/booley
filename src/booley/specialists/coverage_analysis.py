"""Advisory interpretation of immutable native Coverage Campaign evidence."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from booley.core.boundary import BoundaryError, require_dict, require_list, require_str
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    DurableTargetIdentity,
    FrozenJson,
    decode_coverage_campaign,
    encode_coverage_campaign,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_campaign_store import (
    CAMPAIGN_SCHEMA_V2,
    CoverageCampaignSummary,
)


class CoverageAnalysisError(ValueError):
    """The input cannot support an advisory Coverage Analysis report."""


@dataclass(frozen=True)
class CoverageSourceClosure:
    """Verified text snapshot of the producing Target's complete source closure."""

    target_identity: str
    files: Mapping[str, FrozenJson]


@dataclass(frozen=True)
class CoverageAnalysisReport:
    """Versioned advisory output, with immutable observations and hypotheses."""

    document: Mapping[str, FrozenJson]

    def to_dict(self) -> dict[str, object]:
        return json.loads(json.dumps(self.document, default=_json_value))


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Not JSON data: {type(value).__name__}")


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
        analysis_schema = "booley.coverage-analysis/v1"
        model_campaign: dict[str, object] = {"campaign": observed}
        observed_evidence: object = observed
        if summary is not None and summary.source_schema == CAMPAIGN_SCHEMA_V2:
            manifest = _json_value(summary.document)
            points = observed["points"]
            digest = summary.point_store.sha256 if summary.point_store is not None else None
            analysis_schema = "booley.coverage-analysis/v2"
            model_campaign = {"campaign_manifest": manifest, "points": points}
            observed_evidence = {
                "campaign_manifest": manifest,
                "points": points,
                "point_store_sha256": digest,
            }
        eligibility, limitations = _eligibility(campaign)
        sources = _verified_snapshot(campaign, sources)
        if sources is None:
            limitations.append(
                "Sources unavailable, unsafe, or stale; analysis uses normalized report evidence only."
            )
        response = self._model(
            json.dumps(
                {
                    **model_campaign,
                    "sources": sources.files if sources else None,
                    "instruction": instruction,
                },
                default=_json_value,
            )
        )
        response = _validate_response(response, campaign)
        response["waiver_candidates"] = _screen_candidates(
            response["waiver_candidates"], campaign, sources
        )
        return CoverageAnalysisReport(
            freeze_coverage_mapping(
                {
                    "$schema": analysis_schema,
                    "campaign_id": campaign.campaign_id,
                    "target": observed["target"],
                    "eligibility": eligibility,
                    "source_access": "report_only" if sources is None else "verified",
                    "limitations": limitations,
                    "closure_recommendation": _RECOMMENDATIONS[str(campaign.evaluation["status"])],
                    "observed_evidence": observed_evidence,
                    **response,
                }
            )
        )


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


def _verified_snapshot(
    campaign: CoverageCampaign, sources: CoverageSourceClosure | None
) -> CoverageSourceClosure | None:
    from booley.flows.sim.coverage_provenance import content_digest, coverage_digest

    if sources is None or sources.target_identity != campaign.target.identity:
        return None
    expected = {}
    for category in ("rtl", "testbench"):
        records = cast(tuple[Mapping[str, str], ...], campaign.source_closure[category])
        if coverage_digest(records) != campaign.fingerprints[category + "_sources"]:
            return None
        for record in records:
            expected[record["path"]] = (category, record["sha256"])
    if set(expected) != set(sources.files):
        return None
    for path, (category, digest) in expected.items():
        item = sources.files[path]
        text = item.get("text") if isinstance(item, Mapping) else None
        if not isinstance(item, Mapping) or not isinstance(text, str):
            return None
        if (
            item.get("category") != category
            or item.get("sha256") != digest
            or content_digest(text.encode("utf-8")) != digest
        ):
            return None
    return CoverageSourceClosure(sources.target_identity, freeze_coverage_mapping(sources.files))
