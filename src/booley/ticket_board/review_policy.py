"""Compatibility facade for the Ticket Board's established Reviewer policy API."""

from __future__ import annotations

from pathlib import Path

from booley.criteria.freshness import review_policy_digest as _review_policy_digest


def review_policy_digest(work_dir: Path, category: str) -> str:
    """Preserve the public import path while delegating shared freshness policy."""
    return _review_policy_digest(work_dir, category)
