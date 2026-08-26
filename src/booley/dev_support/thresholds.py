"""Canonical metadata for acceptance-criterion numeric thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ThresholdUnit = Literal["cycles", "percent", "metric"]
ThresholdOperator = Literal["le", "ge"]


@dataclass(frozen=True)
class ThresholdDescriptor:
    """Meaning of one public threshold parameter."""

    param: str
    metric: str
    relative: bool
    unit: ThresholdUnit
    operator: ThresholdOperator


_CYCLE_RELATIVE: dict[str, tuple[ThresholdUnit, ThresholdOperator]] = {
    "cycle_count_increase_at_least": ("percent", "ge"),
    "cycle_count_increase_at_most": ("percent", "le"),
    "cycle_count_reduce_at_least": ("percent", "le"),
    "cycle_count_reduce_at_most": ("percent", "ge"),
    "cycle_count_increase_at_least_cycles": ("cycles", "ge"),
    "cycle_count_increase_at_most_cycles": ("cycles", "le"),
    "cycle_count_reduce_at_least_cycles": ("cycles", "le"),
    "cycle_count_reduce_at_most_cycles": ("cycles", "ge"),
}

CYCLE_COUNT_PARAMS: frozenset[str] = frozenset(
    {"cycle_count_max", "cycle_count_min", *_CYCLE_RELATIVE}
)

CYCLE_COUNT_DESCRIPTORS: dict[str, ThresholdDescriptor] = {
    "cycle_count_max": ThresholdDescriptor(
        "cycle_count_max", "cycle_count", False, "cycles", "le"
    ),
    "cycle_count_min": ThresholdDescriptor(
        "cycle_count_min", "cycle_count", False, "cycles", "ge"
    ),
    **{
        param: ThresholdDescriptor(param, "cycle_count", True, unit, operator)
        for param, (unit, operator) in _CYCLE_RELATIVE.items()
    },
}


def _cycle_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _unavailable_cycle_check(param: str, reason: str) -> dict[str, Any]:
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
        return _unavailable_cycle_check(param, f"unknown Cycle Count threshold {param!r}")
    current_count = _cycle_count(current)
    if current_count is None:
        return _unavailable_cycle_check(
            param, "current Cycle Count evidence is unavailable or invalid"
        )
    result = _cycle_check_result(param, threshold, current_count, descriptor)
    if not descriptor.relative:
        result["pass"] = _threshold_passes(current_count, threshold, descriptor.operator)
        return result
    return _evaluate_relative_cycle_check(result, descriptor, threshold, baseline)


def _cycle_check_result(
    param: str,
    threshold: int | float,
    current: int,
    descriptor: ThresholdDescriptor,
) -> dict[str, Any]:
    return {
        "param": param,
        "pass": False,
        "skipped": False,
        "current": current,
        "baseline": None,
        "delta_cycles": None,
        "pct": None,
        "threshold": threshold,
        "unit": descriptor.unit,
    }


def _evaluate_relative_cycle_check(
    result: dict[str, Any],
    descriptor: ThresholdDescriptor,
    threshold: int | float,
    baseline: int | None,
) -> dict[str, Any]:
    param = str(result["param"])
    baseline_count = _cycle_count(baseline)
    if baseline_count is None:
        return _unavailable_cycle_check(
            param, "baseline Cycle Count evidence is unavailable or invalid"
        )
    delta_cycles = int(result["current"]) - baseline_count
    result["baseline"] = baseline_count
    result["delta_cycles"] = delta_cycles
    measured: int | float = delta_cycles
    if descriptor.unit == "percent":
        if baseline_count == 0:
            return _unavailable_cycle_check(
                param, "zero baseline cannot define a Cycle Count percentage"
            )
        measured = delta_cycles / baseline_count * 100
        result["pct"] = measured
    bound = -threshold if "reduce_" in param else threshold
    result["pass"] = _threshold_passes(measured, bound, descriptor.operator)
    return result


def _threshold_passes(
    measured: int | float,
    bound: int | float,
    operator: ThresholdOperator,
) -> bool:
    return measured <= bound if operator == "le" else measured >= bound


_RELATIVE_SUFFIXES = (
    "_increase_at_least_cycles",
    "_increase_at_most_cycles",
    "_reduce_at_least_cycles",
    "_reduce_at_most_cycles",
    "_increase_at_least",
    "_increase_at_most",
    "_reduce_at_least",
    "_reduce_at_most",
)


def describe_threshold(param: str) -> ThresholdDescriptor | None:
    """Return canonical metadata for a supported threshold spelling."""
    cycle = CYCLE_COUNT_DESCRIPTORS.get(param)
    if cycle is not None:
        return cycle
    if param.endswith("_max"):
        return ThresholdDescriptor(param, param.removesuffix("_max"), False, "metric", "le")
    if param.endswith("_min"):
        return ThresholdDescriptor(param, param.removesuffix("_min"), False, "metric", "ge")
    if param.endswith("_increase_at_most"):
        return ThresholdDescriptor(
            param, param.removesuffix("_increase_at_most"), True, "percent", "le"
        )
    if param.endswith("_reduce_at_least"):
        return ThresholdDescriptor(
            param, param.removesuffix("_reduce_at_least"), True, "percent", "le"
        )
    return None


def is_relative_threshold(param: str) -> bool:
    """Whether *param* needs baseline evidence."""
    descriptor = describe_threshold(param)
    return descriptor.relative if descriptor is not None else param.endswith(_RELATIVE_SUFFIXES)


def has_relative_threshold(params: dict[str, object]) -> bool:
    """Whether any public parameter in *params* needs baseline evidence."""
    return any(is_relative_threshold(str(key)) for key in params if not str(key).startswith("_"))
