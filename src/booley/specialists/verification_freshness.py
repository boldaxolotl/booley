"""Refresh verification evidence for unbound Specialist calls."""

from __future__ import annotations

import logging
from pathlib import Path

from booley.criteria.freshness import (
    evaluate_verification_freshness,
    verification_freshness_eligible,
)
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)
_REPORT_CRITERION = "_report_submitted"


def _refresh_verification_entry(
    key: str,
    entry,
    *,
    work_dir: Path,
    fingerprints: dict[str | None, dict],
    now: str,
) -> bool:
    """Refresh one passing criterion against its receipt and source evidence."""
    result = evaluate_verification_freshness(
        key,
        entry,
        work_dir=work_dir,
        fingerprint_provider=compute_source_fingerprint,
        fingerprints=fingerprints,
    )
    assert result is not None
    if not result.stale:
        return False
    _mark_verification_stale(
        entry,
        now=now,
        reason=result.reason,
        changed_categories=result.changed_categories,
        current=result.current_source_fingerprint,
    )
    if result.review_dimensions:
        entry.detail["stale_review_dimensions"] = list(result.review_dimensions)
    return True


def refresh_verification_freshness(
    state, *, work_dir: Path | None, persist: bool = True
) -> list[str]:
    """Persistently invalidate passing checks whose source fingerprint drifted."""
    resolved_work_dir = work_dir
    if resolved_work_dir is None and getattr(state, "work_dir", ""):
        resolved_work_dir = Path(state.work_dir)
    if resolved_work_dir is None:
        return []

    now = utc_now_rfc3339()
    stale_keys: list[str] = []
    fingerprints: dict[str | None, dict] = {}
    for key, entry in state.criteria.items():
        if not verification_freshness_eligible(
            key,
            entry,
            include_unobserved_review=True,
        ):
            continue
        if _refresh_verification_entry(
            key,
            entry,
            work_dir=resolved_work_dir,
            fingerprints=fingerprints,
            now=now,
        ):
            stale_keys.append(key)

    if stale_keys:
        _invalidate_submitted_report(state, now=now)
        logger.warning(
            "Marked stale verification criteria unmet for %s: %s",
            state.slug,
            ", ".join(stale_keys),
        )
        if persist:
            state.save()
    return stale_keys


def _invalidate_submitted_report(state, *, now: str) -> None:
    """Require a new report after newly discovered stale verification evidence."""
    report_entry = state.criteria.get(_REPORT_CRITERION)
    if report_entry is None or not report_entry.met or report_entry.locked:
        return
    report_entry.met = False
    report_entry.stale = True
    report_entry.updated_at = now
    report_entry.detail = dict(report_entry.detail or {})
    report_entry.detail["stale_reason"] = (
        "Verification evidence became stale after this report was submitted."
    )


def _mark_verification_stale(
    entry,
    *,
    now: str,
    reason: str,
    changed_categories: set[str] | list[str] | tuple[str, ...],
    current: dict,
) -> None:
    """Mark a passing verification entry unmet with a stale diagnostic."""
    entry.met = False
    entry.stale = True
    entry.updated_at = now
    entry.detail = dict(entry.detail or {})
    entry.detail["stale_reason"] = reason
    entry.detail["stale_source_categories"] = sorted(changed_categories)
    entry.detail["current_source_fingerprint"] = current
