"""Shared validation for authored Coverage Criterion thresholds."""

from __future__ import annotations

from collections.abc import Mapping

COVERAGE_METRICS = frozenset({"line", "branch", "expression", "toggle", "cover_property"})


def validate_coverage_metrics(value: object, *, field: str) -> dict[str, dict[str, int | float]]:
    """Return one validated Coverage metric mapping."""
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{field}: Coverage metrics must be a nonempty mapping")
    result: dict[str, dict[str, int | float]] = {}
    for metric, policy in value.items():
        if metric not in COVERAGE_METRICS:
            raise ValueError(f"{field}: unknown metrics include {metric!r}")
        if not isinstance(policy, Mapping) or set(policy) != {"min_pct"}:
            raise ValueError(f"{field}: {metric} requires min_pct")
        threshold = policy["min_pct"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0 < threshold <= 100
        ):
            raise ValueError(f"{field}: {metric}.min_pct must be greater than 0 and at most 100")
        result[str(metric)] = {"min_pct": threshold}
    return result
