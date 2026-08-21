"""Normalize persisted review findings for deterministic user-facing reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _entry_detail(entry: Any) -> Mapping[str, Any]:
    if isinstance(entry, Mapping):
        detail = entry.get("detail")
    else:
        detail = getattr(entry, "detail", None)
    return detail if isinstance(detail, Mapping) else {}


def _finding_row(
    criterion: str,
    finding: Mapping[str, Any],
    disposition: str,
) -> dict[str, Any]:
    return {
        "criterion": criterion,
        "finding_id": str(finding.get("finding_id", "")),
        "severity": str(finding.get("severity", "UNKNOWN")),
        "file": str(finding.get("file", "")),
        "line": finding.get("line", 0),
        "summary": str(finding.get("summary", "")),
        "disposition": disposition,
        "evidence": str(finding.get("evidence", "")),
        "justification": str(finding.get("justification", "")),
        "exclusion_reason": str(finding.get("exclusion_reason", "")),
        "actor": str(finding.get("disposition_actor", "")),
    }


def collect_review_dispositions(criteria: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return normalized findings from new and legacy review criterion detail."""
    rows: list[dict[str, Any]] = []
    for criterion, entry in criteria.items():
        if not criterion.startswith("review_"):
            continue
        detail = _entry_detail(entry)
        if criterion.endswith("_done"):
            issues = detail.get("issue_list", [])
            if isinstance(issues, list):
                rows.extend(
                    _finding_row(criterion, finding, "reported")
                    for finding in issues
                    if isinstance(finding, Mapping)
                )
            continue

        pending = detail.get("pending", detail.get("issue_list", []))
        if isinstance(pending, list):
            rows.extend(
                _finding_row(criterion, finding, "open")
                for finding in pending
                if isinstance(finding, Mapping)
            )
        resolved = detail.get("resolved", [])
        if not isinstance(resolved, list):
            continue
        for finding in resolved:
            if not isinstance(finding, Mapping):
                continue
            status = str(finding.get("status", "fixed"))
            normalized_finding = finding
            if status in {"project_policy", "out_of_diff_scope"}:
                status = "excluded"
            elif status == "impasse_deferred":
                status = "waived"
                normalized_finding = {
                    **finding,
                    "justification": (
                        finding.get("justification")
                        or "Legacy automatic impasse disposition; reassess before relying on it."
                    ),
                }
            rows.append(_finding_row(criterion, normalized_finding, status))
    return rows


def review_report_required(criteria: Mapping[str, Any]) -> bool:
    """Return whether review evidence requires a user-facing run report."""
    if any(key.startswith("review_") and key.endswith("_done") for key in criteria):
        return True
    return any(row["disposition"] == "waived" for row in collect_review_dispositions(criteria))
