"""Advisory interpretation of immutable native Coverage Campaign evidence."""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from booley.core.boundary import BoundaryError, as_str, require_dict, require_list, require_str
from booley.flows.sim.coverage_analysis_input import (
    CoverageAnalysisError,
    CoverageSourceClosure,
    verified_source_snapshot,
)
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoveragePoint,
    FrozenJson,
    decode_coverage_point_id,
    encode_coverage_campaign,
    encode_coverage_point,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_campaign_store import (
    CAMPAIGN_SCHEMA_V3,
    CoverageCampaignSummary,
)
from booley.flows.sim.coverage_evidence import is_coverage_point_reference


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
    point_references: Mapping[str, str]


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
    campaign: CoverageCampaign, summary: CoverageCampaignSummary | None
) -> tuple[_AnalysisEnvelope, dict[str, object]]:
    target: dict[str, object] = {
        "identity": campaign.target.identity,
        "selector": campaign.target.selector,
    }
    storage_schema = "booley.coverage-campaign/v1"
    point_store = None
    manifest = None
    if summary is not None:
        storage_schema = summary.source_schema
        manifest = cast(dict[str, object], _json_value(summary.document))
        point_store = manifest.get("point_store")
    reference = {
        "campaign_reference": {
            "campaign_id": campaign.campaign_id,
            "target": target,
            "point_count": len(campaign.points),
            "storage_schema": storage_schema,
            **({"point_store": point_store} if point_store is not None else {}),
        }
    }
    if summary is None or summary.source_schema != CAMPAIGN_SCHEMA_V3:
        observed = encode_coverage_campaign(campaign)
        return _AnalysisEnvelope("booley.coverage-analysis/v1", reference, observed), target
    assert manifest is not None
    assert summary.point_store is not None
    return (
        _AnalysisEnvelope(
            "booley.coverage-analysis/v2",
            reference,
            {
                "campaign_manifest": manifest,
                "point_store_sha256": summary.point_store.sha256,
            },
        ),
        target,
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
        envelope, target = _analysis_envelope(campaign, summary)
        eligibility, limitations = _eligibility(campaign)
        sources = verified_source_snapshot(campaign, sources)
        if sources is None:
            limitations.append(
                "Sources unavailable, unsafe, or stale; analysis uses normalized report evidence only."
            )
        response, analysis_scope = _model_response(
            self._model(_analysis_prompt(envelope, sources, instruction)), campaign
        )
        points = {point.id: point for point in campaign.points}
        response = _validate_response(response, points)
        response["waiver_candidates"] = _screen_candidates(
            response["waiver_candidates"], campaign, points, sources
        )
        report = {
            "$schema": envelope.schema,
            "campaign_id": campaign.campaign_id,
            "target": target,
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
                "excerpts. Begin with overview, retrieve only evidence needed for the "
                "analysis, and cite only point_ref values returned by the tool."
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
    response = _normalize_scoped_response(
        model_result.response, campaign, model_result.point_references, scope
    )
    return response, scope


@dataclass(frozen=True)
class _ReferenceIssue:
    pointer: str
    category: str
    fingerprint: str


def _normalize_scoped_response(
    value: object,
    campaign: CoverageCampaign,
    point_references: Mapping[str, str],
    analysis_scope: Mapping[str, object],
) -> dict[str, object]:
    response = require_dict(value)
    if set(response) != {"hypotheses", "recommendations", "waiver_candidates"}:
        raise CoverageAnalysisError(
            "Model must return only hypotheses, recommendations and waiver_candidates"
        )
    points = {point.id for point in campaign.points}
    delivered = set(cast(list[str], analysis_scope.get("point_ids", [])))
    references = _validated_reference_map(point_references, points, delivered)
    issues: list[_ReferenceIssue] = []
    normalized: dict[str, object] = {}
    for category, text_key in (("hypotheses", "explanation"), ("recommendations", "action")):
        normalized[category] = _normalize_scoped_records(
            response[category], category, text_key, references, points, delivered, issues
        )
    normalized["waiver_candidates"] = _normalize_scoped_candidates(
        response["waiver_candidates"], references, points, delivered, issues
    )
    _raise_reference_issues(issues)
    return normalized


def _validated_reference_map(
    references: Mapping[str, str], points: set[str], delivered: set[str]
) -> dict[str, str]:
    valid = {}
    for point_ref, point_id in references.items():
        if (
            not is_coverage_point_reference(point_ref)
            or not isinstance(point_id, str)
            or point_id not in points
            or point_id not in delivered
        ):
            raise CoverageAnalysisError("Malformed Coverage Point reference audit")
        valid[point_ref] = point_id
    return valid


def _normalize_scoped_records(
    value: object,
    category: str,
    text_key: str,
    references: Mapping[str, str],
    points: set[str],
    delivered: set[str],
    issues: list[_ReferenceIssue],
) -> list[dict[str, object]]:
    normalized = []
    for index, item in enumerate(require_list(value)):
        record = require_dict(item)
        reference_key = _model_reference_key(record, text_key)
        if reference_key is None:
            raise CoverageAnalysisError(f"Malformed model {category}")
        text = require_str(record, text_key)
        pointer = f"/{category}/{index}/{reference_key}"
        point_ids = _resolve_references(
            record[reference_key], pointer, references, points, delivered, issues
        )
        normalized.append({"point_ids": point_ids, text_key: text})
    return normalized


def _model_reference_key(record: Mapping[str, object], text_key: str) -> str | None:
    for reference_key in ("point_refs", "point_ids"):
        if set(record) == {reference_key, text_key}:
            return reference_key
    return None


def _normalize_scoped_candidates(
    value: object,
    references: Mapping[str, str],
    points: set[str],
    delivered: set[str],
    issues: list[_ReferenceIssue],
) -> list[dict[str, object]]:
    normalized = []
    fields = {"reason", "evidence", "proof_reference"}
    for index, item in enumerate(require_list(value)):
        record = require_dict(item)
        reference_key = next(
            (key for key in ("point_ref", "point_id") if set(record) == fields | {key}), None
        )
        if reference_key is None:
            raise CoverageAnalysisError("Malformed waiver candidate")
        values = _candidate_strings(record, fields)
        point_ids = _resolve_references(
            [record[reference_key]],
            f"/waiver_candidates/{index}/{reference_key}",
            references,
            points,
            delivered,
            issues,
        )
        normalized.append({"point_id": point_ids[0] if point_ids else "", **values})
    return normalized


def _candidate_strings(record: Mapping[str, object], keys: set[str]) -> dict[str, str]:
    values = {key: as_str(record.get(key)) for key in keys}
    if any(value is None for value in values.values()):
        raise CoverageAnalysisError("Candidate fields must be strings")
    return cast(dict[str, str], values)


def _resolve_references(
    value: object,
    pointer: str,
    references: Mapping[str, str],
    points: set[str],
    delivered: set[str] | None,
    issues: list[_ReferenceIssue],
) -> list[str]:
    resolved = []
    values = require_list(value)
    for index, reference in enumerate(values):
        point_id, category = _resolve_reference(reference, references, points, delivered)
        if category is None:
            assert point_id is not None
            resolved.append(point_id)
        else:
            item_pointer = (
                pointer
                if len(values) == 1 and pointer.endswith("point_ref")
                else f"{pointer}/{index}"
            )
            issues.append(
                _ReferenceIssue(item_pointer, category, _reference_fingerprint(reference))
            )
    return resolved


def _resolve_reference(
    value: object,
    references: Mapping[str, str],
    points: set[str],
    delivered: set[str] | None,
) -> tuple[str | None, str | None]:
    point_id = None
    category = None
    if not isinstance(value, str):
        category = "non_string_reference"
    elif value.startswith("point:"):
        if not is_coverage_point_reference(value):
            category = "malformed_point_ref"
        elif value in references:
            point_id = references[value]
        else:
            category = "unknown_point_ref"
    elif value.startswith("cp1:"):
        if decode_coverage_point_id(value) is None:
            category = "malformed_cp1"
        elif value not in points:
            category = "cp1_not_in_campaign"
        elif delivered is not None and value not in delivered:
            category = "cp1_not_delivered"
        else:
            point_id = value
    else:
        category = "malformed_point_ref"
    return point_id, category


def _reference_fingerprint(value: object) -> str:
    raw = value.encode() if isinstance(value, str) else type(value).__name__.encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()[:12]}"


def _raise_reference_issues(issues: list[_ReferenceIssue]) -> None:
    if not issues:
        return
    detail = "; ".join(
        f"{issue.pointer}: {issue.category} ({issue.fingerprint})" for issue in issues
    )
    raise CoverageAnalysisError(f"Invalid Coverage Point references: {detail}")


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


def _validate_response(value: object, points: Mapping[str, CoveragePoint]) -> dict[str, object]:
    try:
        response = require_dict(value)
        if set(response) != {"hypotheses", "recommendations", "waiver_candidates"}:
            raise CoverageAnalysisError(
                "Model must return only hypotheses, recommendations and waiver_candidates"
            )
        known_points = set(points)
        issues: list[_ReferenceIssue] = []
        for category, text_key in (("hypotheses", "explanation"), ("recommendations", "action")):
            for item_index, item in enumerate(require_list(response[category])):
                record = require_dict(item)
                if set(record) != {"point_ids", text_key}:
                    raise CoverageAnalysisError(f"Malformed model {category}")
                require_str(record, text_key)
                references = require_list(record["point_ids"])
                _resolve_references(
                    references,
                    f"/{category}/{item_index}/point_ids",
                    {},
                    known_points,
                    None,
                    issues,
                )
        _raise_reference_issues(issues)
        require_list(response["waiver_candidates"])
        return dict(response)
    except BoundaryError as exc:
        raise CoverageAnalysisError(f"Malformed model response: {exc}") from exc


def _screen_candidates(
    value: object,
    campaign: CoverageCampaign,
    points: Mapping[str, CoveragePoint],
    sources: CoverageSourceClosure | None,
) -> list[dict[str, object]]:
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
                "point_identity": encode_coverage_point(point)["identity"] if point else None,
                "source_fingerprint": rtl.get(_point_source(point)) if point else None,
                "screening": screening,
                "screening_reason": detail,
                "approval": "not_approved",
            }
        )
    return screened


def _candidate_screen(item, point, rtl, sources) -> tuple[str, str]:
    if point is None or _point_source(point) not in rtl:
        return "forbidden", "Candidate must identify an exact RTL Coverage Point"
    if point.disposition["kind"] != "eligible" or item["reason"] not in {
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
        not item["proof_reference"].strip() or point.hits_by_run
    ):
        return (
            "investigate",
            "Unreachability requires a proof reference and no contradictory observed hits",
        )
    return (
        "ready_for_human_review",
        "Advisory only; a human must verify evidence and author any approval",
    )


def _point_source(point: CoveragePoint) -> str:
    return str(point.identity.location["source"])
