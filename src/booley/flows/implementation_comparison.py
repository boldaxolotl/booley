"""Target-pair selection for baseline-relative implementation Criteria."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from booley.dev_support.criteria import BASELINE_TARGET_PARAM


class ImplementationComparisonError(ValueError):
    """Persisted criterion metadata cannot define an executable Target pair."""


@dataclass(frozen=True)
class ImplementationTargetPair:
    """Candidate-selected implementation Target and its sealed baseline Target."""

    baseline: str
    candidate: str


def target_pairs_for_candidates(
    criteria: Mapping[str, Any],
    criterion_prefix: str,
    candidates: Sequence[str],
) -> tuple[ImplementationTargetPair, ...]:
    """Resolve selected candidates; missing metadata preserves equal-Target behavior."""
    pairs: list[ImplementationTargetPair] = []
    seen: dict[str, str] = {}
    for candidate in candidates:
        baseline = candidate
        entry = criteria.get(f"{criterion_prefix}{candidate}")
        params = getattr(entry, "params", None) if entry is not None else None
        if isinstance(params, dict) and BASELINE_TARGET_PARAM in params:
            raw = params[BASELINE_TARGET_PARAM]
            if not isinstance(raw, str) or not raw.strip():
                raise ImplementationComparisonError(
                    f"{criterion_prefix}{candidate} has invalid baseline Target metadata"
                )
            baseline = raw.strip()
        prior = seen.get(candidate)
        if prior is not None and prior != baseline:
            raise ImplementationComparisonError(
                f"candidate Target {candidate!r} has conflicting baselines "
                f"{prior!r} and {baseline!r}"
            )
        seen[candidate] = baseline
        pairs.append(ImplementationTargetPair(baseline, candidate))
    return tuple(pairs)
