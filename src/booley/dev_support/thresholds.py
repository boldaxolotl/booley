"""Canonical metadata for acceptance-criterion numeric thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
