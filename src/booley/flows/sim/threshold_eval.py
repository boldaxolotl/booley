"""Pure numeric evaluation for per-test Cycle Count criteria."""

from __future__ import annotations

from typing import Any

from booley.dev_support.thresholds import CYCLE_COUNT_DESCRIPTORS


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _unavailable(param: str, reason: str) -> dict[str, Any]:
    return {"param": param, "pass": False, "skipped": False, "reason": reason}


def evaluate_cycle_threshold(
    param: str,
    threshold: int | float,
    *,
    current: int | None,
    baseline: int | None = None,
) -> dict[str, Any]:
    """Evaluate one Cycle Count threshold without rounding measured values."""
    descriptor = CYCLE_COUNT_DESCRIPTORS.get(param)
    if descriptor is None:
        return _unavailable(param, f"unknown Cycle Count threshold {param!r}")
    current_count = _count(current)
    if current_count is None:
        return _unavailable(param, "current Cycle Count evidence is unavailable or invalid")

    result: dict[str, Any] = {
        "param": param,
        "pass": False,
        "skipped": False,
        "current": current_count,
        "baseline": None,
        "delta_cycles": None,
        "pct": None,
        "threshold": threshold,
        "unit": descriptor.unit,
    }
    if not descriptor.relative:
        result["pass"] = (
            current_count <= threshold
            if descriptor.operator == "le"
            else current_count >= threshold
        )
        return result

    baseline_count = _count(baseline)
    if baseline_count is None:
        return _unavailable(param, "baseline Cycle Count evidence is unavailable or invalid")
    delta_cycles = current_count - baseline_count
    result["baseline"] = baseline_count
    result["delta_cycles"] = delta_cycles

    if descriptor.unit == "percent":
        if baseline_count == 0:
            return _unavailable(param, "zero baseline cannot define a Cycle Count percentage")
        measured = delta_cycles / baseline_count * 100
        result["pct"] = measured
    else:
        measured = delta_cycles

    bound = -threshold if "reduce_" in param else threshold
    result["pass"] = measured <= bound if descriptor.operator == "le" else measured >= bound
    return result
