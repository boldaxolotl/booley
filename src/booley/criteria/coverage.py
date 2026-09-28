"""Shared validation for authored Coverage Criterion thresholds."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

COVERAGE_METRICS = frozenset({"line", "branch", "expression", "toggle", "cover_property"})


def _describe_value(value: object) -> str:
    if isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, str):
        kind = "string"
    else:
        kind = type(value).__name__
    try:
        rendered = json.dumps(value)
    except (TypeError, ValueError):
        rendered = repr(value)
    return f"{kind} {rendered}"


def validate_coverage_metrics(value: object, *, field: str) -> dict[str, dict[str, int | float]]:
    """Return one validated Coverage metric mapping."""
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{field}: Coverage metrics must be a nonempty mapping")
    result: dict[str, dict[str, int | float]] = {}
    for metric, policy in value.items():
        if metric not in COVERAGE_METRICS:
            raise ValueError(f"{field}: unknown metrics include {metric!r}")
        if not isinstance(policy, Mapping):
            raise ValueError(f"{field}: {metric} policy must be a mapping")
        unexpected = set(policy) - {"min_pct"}
        if unexpected:
            rendered = ", ".join(repr(key) for key in sorted(unexpected, key=str))
            raise ValueError(
                f"{field}: {metric} unexpected policy keys include {rendered}; "
                "only 'min_pct' is allowed"
            )
        if "min_pct" not in policy:
            raise ValueError(f"{field}: {metric} requires min_pct")
        threshold = policy["min_pct"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError(
                f"{field}: {metric}.min_pct must be a number, got {_describe_value(threshold)}"
            )
        if not math.isfinite(threshold):
            raise ValueError(f"{field}: {metric}.min_pct must be finite")
        if not 0 < threshold <= 100:
            raise ValueError(f"{field}: {metric}.min_pct must be greater than 0 and at most 100")
        result[str(metric)] = {"min_pct": threshold}
    return result
