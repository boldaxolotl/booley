"""Source-fingerprint evidence attached to verification criteria."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.flows.source_fingerprint import compute_source_fingerprint


@dataclass(frozen=True)
class CriterionFreshness:
    """Serializable freshness evidence for one verification criterion."""

    target: str | None
    categories: tuple[str, ...]
    fingerprint: Mapping[str, Any]

    def to_detail(self) -> dict[str, Any]:
        """Return the established criterion-detail representation."""
        return {
            "categories": list(self.categories),
            "fingerprint": dict(self.fingerprint),
            "target": self.target,
        }


def build_criterion_freshness(
    work_dir: Path,
    *,
    target: str | None,
    categories: Sequence[str],
) -> CriterionFreshness:
    """Build freshness evidence for targeted and project-wide criteria."""
    fingerprint = compute_source_fingerprint(work_dir, target=target)
    return CriterionFreshness(
        target=target,
        categories=tuple(sorted(set(categories))),
        fingerprint=fingerprint,
    )
