"""Resolve live Ticket review policy into dependency-neutral receipt values."""

from __future__ import annotations

from pathlib import Path

from booley.criteria.freshness import review_policy_digest as _review_policy_digest


def review_policy_digest(work_dir: Path, category: str) -> str:
    """Return the current policy identity used by one Reviewer invocation."""
    return _review_policy_digest(work_dir, category)
