"""Bounded model-facing evidence views over one validated Coverage Campaign."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool,
    require_dict,
    require_int,
    require_list,
    require_opt_str,
    require_str,
)

from .coverage_analysis_input import CoverageSourceClosure
from .coverage_campaign import (
    CoverageCampaign,
    CoverageFinding,
    CoverageRollup,
    decode_coverage_point_id,
    encode_coverage_point,
)

MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TOTAL_BUDGET_BYTES = 384 * 1024
MAX_POINT_LIMIT = 100
MAX_SOURCE_POINTS = 10
MAX_CONTEXT_LINES = 20
COVERAGE_POINT_REFERENCE_PATTERN = r"^point:[1-9][0-9]*$"


@dataclass(frozen=True)
class CoverageEvidenceAudit:
    """Validated result of one model-facing evidence session."""

    analysis_scope: Mapping[str, object]
    point_references: Mapping[str, str]
    terminal_error: str | None
    budget_exhausted: bool


class CoverageEvidenceError(ValueError):
    """A query cannot be answered safely from the active Campaign."""


def is_coverage_point_reference(value: object) -> bool:
    """Return whether a value is one canonical model-facing point handle."""
    return (
        isinstance(value, str)
        and re.fullmatch(COVERAGE_POINT_REFERENCE_PATTERN, value) is not None
    )


def decode_coverage_evidence_audit(value: object) -> CoverageEvidenceAudit:
    """Validate a persisted evidence audit before it authorizes model references."""
    document = require_dict(value, field="coverage evidence audit")
    fields = {
        "total_points",
        "points_retrieved",
        "point_ids",
        "point_references",
        "evidence_bytes_delivered",
        "evidence_budget_bytes",
        "budget_exhausted",
        "queries",
    }
    unknown = set(document) - fields - {"terminal_error"}
    if unknown:
        raise BoundaryError(f"coverage evidence audit has unknown fields: {sorted(unknown)}")
    terminal_error = _audit_terminal_error(document)
    budget_exhausted = require_bool(document, "budget_exhausted")
    if not (set(document) & (fields - {"budget_exhausted"})):
        return CoverageEvidenceAudit({}, {}, terminal_error, budget_exhausted)
    if not fields <= set(document):
        missing = sorted(fields - set(document))
        raise BoundaryError(f"coverage evidence audit is missing fields: {missing}")
    return _decode_complete_audit(document, terminal_error, budget_exhausted)


def _audit_terminal_error(document: Mapping[str, object]) -> str | None:
    if "terminal_error" not in document:
        return None
    return require_str(document, "terminal_error")


def _decode_complete_audit(
    document: Mapping[str, object], terminal_error: str | None, budget_exhausted: bool
) -> CoverageEvidenceAudit:
    total_points = _audit_count(document.get("total_points"), "total_points")
    points_retrieved = _audit_count(document.get("points_retrieved"), "points_retrieved")
    point_ids = _audit_point_ids(document.get("point_ids"), "point_ids")
    references = _audit_references(document.get("point_references"), set(point_ids))
    queries, queried_ids = _audit_queries(document.get("queries"))
    delivered_bytes = _audit_count(
        document.get("evidence_bytes_delivered"), "evidence_bytes_delivered"
    )
    budget_bytes = _audit_count(document.get("evidence_budget_bytes"), "evidence_budget_bytes")
    if points_retrieved != len(set(point_ids)) or set(point_ids) != queried_ids:
        raise BoundaryError("coverage evidence audit point counts disagree with its queries")
    if points_retrieved > total_points or delivered_bytes > budget_bytes or budget_bytes == 0:
        raise BoundaryError("coverage evidence audit counts are inconsistent")
    scope = {
        "total_points": total_points,
        "points_retrieved": points_retrieved,
        "point_ids": point_ids,
        "evidence_bytes_delivered": delivered_bytes,
        "evidence_budget_bytes": budget_bytes,
        "budget_exhausted": budget_exhausted,
        "queries": queries,
    }
    return CoverageEvidenceAudit(scope, references, terminal_error, budget_exhausted)


def _audit_count(value: object, field: str) -> int:
    count = require_int(value, field=field)
    if count < 0:
        raise BoundaryError(f"{field} must be non-negative")
    return count


def _audit_point_ids(value: object, field: str) -> list[str]:
    point_ids = require_list(value, field=field)
    if any(decode_coverage_point_id(point_id) is None for point_id in point_ids):
        raise BoundaryError(f"{field} must contain canonical Coverage Point IDs")
    return cast(list[str], point_ids)


def _audit_references(value: object, delivered: set[str]) -> dict[str, str]:
    raw = require_dict(value, field="point_references")
    references: dict[str, str] = {}
    for point_ref, point_id in raw.items():
        if (
            not is_coverage_point_reference(point_ref)
            or decode_coverage_point_id(point_id) is None
        ):
            raise BoundaryError("point_references must map canonical handles to point IDs")
        if point_id not in delivered:
            raise BoundaryError("point_references contains an undelivered Coverage Point")
        references[cast(str, point_ref)] = cast(str, point_id)
    return references


def _audit_queries(value: object) -> tuple[list[dict[str, object]], set[str]]:
    queries = require_list(value, field="queries")
    validated = []
    queried_ids: set[str] = set()
    for index, item in enumerate(queries):
        query = require_dict(item, field=f"queries[{index}]")
        if set(query) != {"view", "returned", "point_ids"}:
            raise BoundaryError(f"queries[{index}] has unexpected fields")
        view = require_str(query, "view")
        if view not in {"overview", "points", "source"}:
            raise BoundaryError(f"queries[{index}].view is invalid")
        returned = _audit_count(query.get("returned"), f"queries[{index}].returned")
        point_ids = _audit_point_ids(query.get("point_ids"), f"queries[{index}].point_ids")
        if returned != len(point_ids):
            raise BoundaryError(f"queries[{index}] returned count is inconsistent")
        queried_ids.update(point_ids)
        validated.append({"view": view, "returned": returned, "point_ids": point_ids})
    return validated, queried_ids


class CoverageEvidenceSession:
    """Filter, page and audit evidence without exposing Campaign storage."""

    def __init__(
        self,
        campaign: CoverageCampaign,
        sources: CoverageSourceClosure | None,
        *,
        total_budget_bytes: int = DEFAULT_TOTAL_BUDGET_BYTES,
    ) -> None:
        if total_budget_bytes <= 0:
            raise CoverageEvidenceError("Evidence budget must be positive")
        self._campaign = campaign
        self._sources = sources
        self._points = tuple(
            sorted((encode_coverage_point(p) for p in campaign.points), key=_point_id)
        )
        self._by_id = {str(point["id"]): point for point in self._points}
        self._ref_by_id = {
            str(point["id"]): f"point:{index}" for index, point in enumerate(self._points, start=1)
        }
        self._delivered_references: dict[str, str] = {}
        self._total_budget_bytes = total_budget_bytes
        self._delivered_bytes = 0
        self._budget_exhausted = False
        self._queries: list[dict[str, object]] = []

    @property
    def delivered_bytes(self) -> int:
        return self._delivered_bytes

    def query(self, value: Mapping[str, object]) -> dict[str, object]:
        """Return one bounded view and charge it to the session evidence budget."""
        try:
            request = require_dict(value)
            view = require_str(request, "view")
            if view == "overview":
                result = self._overview(request)
            elif view == "points":
                result = self._point_page(request)
            elif view == "source":
                result = self._source_excerpts(request)
            else:
                raise CoverageEvidenceError("view must be overview, points, or source")
        except BoundaryError as exc:
            raise CoverageEvidenceError(f"Malformed evidence query: {exc}") from exc
        return self._deliver(view, request, result)

    def analysis_scope(self) -> dict[str, object]:
        """Describe exactly how much Campaign evidence was delivered."""
        point_ids = sorted(
            {
                point_id
                for query in self._queries
                for point_id in cast(list[str], query.get("point_ids", []))
            }
        )
        return {
            "total_points": len(self._points),
            "points_retrieved": len(point_ids),
            "point_ids": point_ids,
            "point_references": dict(sorted(self._delivered_references.items())),
            "evidence_bytes_delivered": self._delivered_bytes,
            "evidence_budget_bytes": self._total_budget_bytes,
            "budget_exhausted": self._budget_exhausted,
            "queries": list(self._queries),
        }

    def _overview(self, request: Mapping[str, object]) -> dict[str, object]:
        _closed(request, {"view"})
        disposition = Counter(str(_disposition(point)["kind"]) for point in self._points)
        uncovered_metrics = Counter(
            str(_identity(point)["metric"])
            for point in self._points
            if not _covered(point) and _disposition(point)["kind"] == "eligible"
        )
        uncovered_sources = Counter(
            str(cast(Mapping[str, object], _identity(point)["location"])["source"])
            for point in self._points
            if not _covered(point) and _disposition(point)["kind"] == "eligible"
        )
        source_groups = sorted(uncovered_sources.items(), key=lambda item: (-item[1], item[0]))
        return {
            "campaign_id": self._campaign.campaign_id,
            "target": {
                "identity": self._campaign.target.identity,
                "selector": self._campaign.target.selector,
            },
            "collection": _plain_json(self._campaign.collection),
            "normalization": {
                "status": self._campaign.normalization["status"],
                "unrecognized_record_count": len(
                    cast(tuple[object, ...], self._campaign.normalization["unrecognized_records"])
                ),
            },
            "evaluation": _plain_json(self._campaign.evaluation),
            "rollups": [_rollup(rollup) for rollup in self._campaign.rollups],
            "point_counts": dict(sorted(disposition.items())),
            "uncovered_eligible_by_metric": dict(sorted(uncovered_metrics.items())),
            "uncovered_eligible_by_source": [
                {"source": source, "points": count} for source, count in source_groups[:100]
            ],
            "uncovered_source_groups_omitted": max(0, len(source_groups) - 100),
            "tests": _tests(self._campaign),
            "findings": [_finding(item) for item in self._campaign.findings[:100]],
            "findings_omitted": max(0, len(self._campaign.findings) - 100),
            "source_access": "verified" if self._sources is not None else "report_only",
        }

    def _point_page(self, request: Mapping[str, object]) -> dict[str, object]:
        allowed = {
            "view",
            "metric",
            "source",
            "covered",
            "disposition",
            "point_ids",
            "point_refs",
            "cursor",
            "limit",
        }
        _closed(request, allowed)
        _validate_point_filters(request)
        limit = _bounded_int(request.get("limit", 50), "limit", 1, MAX_POINT_LIMIT)
        point_ids = self._query_point_ids(request, MAX_POINT_LIMIT)
        selected = [point for point in self._points if _matches(point, request, point_ids)]
        offset = _cursor_offset(request, len(selected))
        records, end = _bounded_records(selected, offset, limit, "points")
        return {
            "matched_points": len(selected),
            "points": records,
            "next_cursor": _cursor(request, end) if end < len(selected) else None,
        }

    def _source_excerpts(self, request: Mapping[str, object]) -> dict[str, object]:
        _closed(request, {"view", "point_ids", "point_refs", "context_lines"})
        point_ids = self._query_point_ids(request, MAX_SOURCE_POINTS, required=True)
        context = _bounded_int(
            request.get("context_lines", 8), "context_lines", 0, MAX_CONTEXT_LINES
        )
        if self._sources is None:
            return {"source_access": "report_only", "excerpts": []}
        excerpts = [self._excerpt(point_id, context) for point_id in point_ids]
        return {"source_access": "verified", "excerpts": excerpts}

    def _query_point_ids(
        self, request: Mapping[str, object], maximum: int, *, required: bool = False
    ) -> list[str] | None:
        point_ids = _string_list(request.get("point_ids"), "point_ids", maximum)
        point_refs = _string_list(request.get("point_refs"), "point_refs", maximum)
        if point_ids is not None and point_refs is not None:
            raise CoverageEvidenceError("Use point_ids or point_refs, not both")
        if point_refs is not None:
            return [self._resolve_point_ref(value) for value in point_refs]
        if point_ids is None and required:
            raise CoverageEvidenceError("point_ids or point_refs is required")
        return point_ids

    def _resolve_point_ref(self, point_ref: str) -> str:
        point_id = self._delivered_references.get(point_ref)
        if point_id is None:
            raise CoverageEvidenceError("Unknown Coverage Point reference")
        return point_id

    def _excerpt(self, point_id: str, context: int) -> dict[str, object]:
        point = self._by_id.get(point_id)
        if point is None:
            raise CoverageEvidenceError(f"Unknown Coverage Point: {point_id}")
        location = cast(Mapping[str, Any], point["identity"])["location"]
        source_path = str(location["source"])
        source = self._sources.files.get(source_path) if self._sources is not None else None
        if not isinstance(source, Mapping) or not isinstance(source.get("text"), str):
            raise CoverageEvidenceError(
                f"Verified source unavailable for Coverage Point: {point_id}"
            )
        lines = str(source["text"]).splitlines(keepends=True)
        first = max(1, int(location["start"]["line"]) - context)
        last = min(len(lines), int(location["end"]["line"]) + context)
        return {
            "point_id": point_id,
            "path": source_path,
            "sha256": source.get("sha256"),
            "start_line": first,
            "end_line": last,
            "text": "".join(lines[first - 1 : last]),
        }

    def _deliver(
        self, view: str, request: Mapping[str, object], result: dict[str, object]
    ) -> dict[str, object]:
        presented, references = self._present_result(view, result)
        encoded = _encoded_size(presented)
        if encoded > MAX_RESPONSE_BYTES and view == "overview":
            presented = _minimal_overview(presented)
            encoded = _encoded_size(presented)
        if encoded > MAX_RESPONSE_BYTES:
            raise CoverageEvidenceError("Evidence response is too large; narrow the query")
        if self._delivered_bytes + encoded > self._total_budget_bytes:
            self._budget_exhausted = True
            return {
                "error": "evidence_budget_exhausted",
                "delivered_bytes": self._delivered_bytes,
                "total_budget_bytes": self._total_budget_bytes,
            }
        self._delivered_bytes += encoded
        self._delivered_references.update(references)
        records = result.get("points", result.get("excerpts", []))
        self._queries.append(
            {
                "view": view,
                "returned": len(records) if isinstance(records, list) else 0,
                "point_ids": [
                    str(item["id" if view == "points" else "point_id"]) for item in records
                ]
                if isinstance(records, list)
                else [],
            }
        )
        return presented

    def _present_result(
        self, view: str, result: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, str]]:
        key = "points" if view == "points" else "excerpts" if view == "source" else None
        if key is None:
            return result, {}
        records = cast(list[dict[str, object]], result.get(key, []))
        id_key = "id" if view == "points" else "point_id"
        references: dict[str, str] = {}
        presented = []
        for item in records:
            point_id = str(item[id_key])
            point_ref = self._ref_by_id[point_id]
            references[point_ref] = point_id
            presented.append(
                {
                    **{name: value for name, value in item.items() if name != id_key},
                    "point_ref": point_ref,
                }
            )
        return {**result, key: presented}, references


def _point_id(point: Mapping[str, object]) -> str:
    return str(point["id"])


def _closed(value: Mapping[str, object], allowed: set[str]) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise CoverageEvidenceError(f"Unknown evidence query fields: {sorted(unknown)}")


def _bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    number = require_int(value, field=field)
    if not minimum <= number <= maximum:
        raise CoverageEvidenceError(f"{field} must be between {minimum} and {maximum}")
    return number


def _string_list(
    value: object, field: str, maximum: int, *, required: bool = False
) -> list[str] | None:
    if value is None and not required:
        return None
    items = require_list(value, field=field)
    if not items or len(items) > maximum or any(not isinstance(item, str) for item in items):
        raise CoverageEvidenceError(f"{field} must contain 1-{maximum} strings")
    return list(items)


def _covered(point: Mapping[str, object]) -> bool:
    return sum(cast(Mapping[str, int], point["hits_by_run"]).values()) > 0


def _identity(point: Mapping[str, object]) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], point["identity"])


def _disposition(point: Mapping[str, object]) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], point["disposition"])


def _matches(
    point: Mapping[str, object], request: Mapping[str, object], point_ids: list[str] | None
) -> bool:
    identity = _identity(point)
    checks = (
        point_ids is None or point["id"] in point_ids,
        request.get("metric") is None or identity["metric"] == request["metric"],
        request.get("source") is None or identity["location"]["source"] == request["source"],
        request.get("covered") is None or _covered(point) is request["covered"],
        request.get("disposition") is None
        or _disposition(point)["kind"] == request["disposition"],
    )
    return all(checks)


def _validate_point_filters(request: Mapping[str, object]) -> None:
    require_opt_str(request, "metric")
    require_opt_str(request, "source")
    disposition = require_opt_str(request, "disposition")
    if disposition is not None and disposition not in {"eligible", "waived", "unscored"}:
        raise CoverageEvidenceError("disposition must be eligible, waived, or unscored")
    if "covered" in request:
        require_bool(request, "covered")


def _query_fingerprint(request: Mapping[str, object]) -> str:
    filters = {key: value for key, value in request.items() if key not in {"cursor", "limit"}}
    raw = json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:12]


def _cursor(request: Mapping[str, object], offset: int) -> str:
    return f"{_query_fingerprint(request)}:{offset}"


def _cursor_offset(request: Mapping[str, object], count: int) -> int:
    value = request.get("cursor")
    if value is None:
        return 0
    if not isinstance(value, str) or value.count(":") != 1:
        raise CoverageEvidenceError("Malformed points cursor")
    fingerprint, raw_offset = value.split(":")
    if fingerprint != _query_fingerprint(request):
        raise CoverageEvidenceError("Points cursor does not match this query")
    try:
        offset = int(raw_offset)
    except ValueError as exc:
        raise CoverageEvidenceError("Malformed points cursor") from exc
    if not 0 <= offset <= count:
        raise CoverageEvidenceError("Points cursor is outside the result set")
    return offset


def _bounded_records(
    values: list[dict[str, object]], offset: int, limit: int, key: str
) -> tuple[list[dict[str, object]], int]:
    records: list[dict[str, object]] = []
    for value in values[offset : offset + limit]:
        if _encoded_size({key: [*records, value]}) > MAX_RESPONSE_BYTES - 1024:
            break
        records.append(value)
    if not records and offset < len(values):
        raise CoverageEvidenceError("One Coverage Point exceeds the response limit")
    return records, offset + len(records)


def _rollup(value: CoverageRollup) -> dict[str, object]:
    return {
        "metric": value.metric,
        "semantics": value.semantics,
        "total_points": value.total_points,
        "eligible_points": value.eligible_points,
        "covered_points": value.covered_points,
        "waived_points": value.waived_points,
        "percent": value.percent,
    }


def _tests(campaign: CoverageCampaign) -> dict[str, object]:
    runs = [
        {
            "id": run.id,
            "test": run.test,
            "simulation_verdict": run.simulation_verdict,
            "collection": run.collection,
        }
        for run in campaign.runs[:100]
    ]
    return {
        "declared_count": len(campaign.declared_tests),
        "selected_count": len(campaign.selected_tests),
        "runs": runs,
        "runs_omitted": max(0, len(campaign.runs) - 100),
    }


def _finding(value: CoverageFinding) -> dict[str, object]:
    return {
        "severity": value.severity,
        "code": value.code,
        "pointer": value.pointer,
        "message": value.message[:1000],
        "message_truncated": len(value.message) > 1000,
    }


def _encoded_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


def _plain_json(value: object) -> object:
    return json.loads(json.dumps(value, default=dict))


def _minimal_overview(value: Mapping[str, object]) -> dict[str, object]:
    """Retain decision-driving rollups when verbose diagnostics exceed one response."""
    tests = cast(Mapping[str, object], value.get("tests", {}))
    sources = cast(list[Mapping[str, object]], value.get("uncovered_eligible_by_source", []))
    source_groups_omitted = value.get("uncovered_source_groups_omitted", 0)
    runs_omitted = tests.get("runs_omitted", 0)
    return {
        "campaign_id": str(value.get("campaign_id", ""))[:256],
        "target": value.get("target"),
        "normalization": value.get("normalization"),
        "rollups": value.get("rollups"),
        "point_counts": value.get("point_counts"),
        "uncovered_eligible_by_metric": value.get("uncovered_eligible_by_metric"),
        "uncovered_eligible_by_source": [
            {"source": str(item.get("source", ""))[:1000], "points": item.get("points")}
            for item in sources[:20]
        ],
        "uncovered_source_groups_omitted": max(
            source_groups_omitted if isinstance(source_groups_omitted, int) else 0,
            len(sources) - 20,
        ),
        "tests": {
            "declared_count": tests.get("declared_count"),
            "selected_count": tests.get("selected_count"),
            "run_count": len(cast(list[object], tests.get("runs", [])))
            + (runs_omitted if isinstance(runs_omitted, int) else 0),
        },
        "source_access": value.get("source_access"),
        "overview_truncated": True,
        "overview_limitation": "Verbose collection, evaluation, or finding details omitted; query exact Coverage Points next.",
    }
