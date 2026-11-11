"""Typed metric declarations shared by every Flow that measures something.

A Flow declares each metric it emits (name, unit, better direction, cost tier,
kind) next to the code that measures it. A run returns its numbers through
:func:`metric_values`, the one emission funnel, which refuses a metric the Flow
did not declare. Flow-neutral: it imports no concrete Flow. The cross-Flow
registry lives in :mod:`booley.measurement.flow_metrics`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from booley.evidence.timing import ClockTiming

MetricValue = int | float


class Better(StrEnum):
    """Which direction of a metric is an improvement."""

    LOWER = "lower"
    HIGHER = "higher"


class MetricKind(StrEnum):
    """Shape of a metric's value in one run."""

    PLAIN = "plain"  # one number per run
    PER_CLOCK = "per_clock"  # one number per clock name
    PER_TEST = "per_test"  # one number per test name


class CostTier(IntEnum):
    """Relative cost of measuring a Flow; lower tiers are measured first."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True)
class MetricDeclaration:
    """One metric a Flow emits."""

    name: str
    unit: str
    better: Better
    cost_tier: CostTier
    kind: MetricKind


@dataclass(frozen=True)
class FlowMetricValues:
    """Typed values of one run, keyed by declared metric name."""

    plain: dict[str, MetricValue]
    per_clock: dict[str, dict[str, float]]  # metric name -> clock name -> value


class UndeclaredMetricError(ValueError):
    """A Flow emitted a metric it did not declare."""


def metric_values(
    declarations: Sequence[MetricDeclaration],
    plain: Mapping[str, MetricValue | None],
    per_clock: Mapping[str, ClockTiming],
) -> FlowMetricValues:
    """Build the typed values of one run from the Flow's own numbers.

    ``plain`` maps declared ``PLAIN`` names to their values. Every ``PER_CLOCK``
    declaration is read from the same-named :class:`ClockTiming` field of each
    clock, so per-clock names exist only in the declarations. A value that is
    ``None`` was not measured by this run and is omitted.
    """
    plain_names = {d.name for d in declarations if d.kind is MetricKind.PLAIN}
    for name in plain:
        if name not in plain_names:
            raise UndeclaredMetricError(
                f"metric {name!r} is not a declared plain metric; declared: {sorted(plain_names)}"
            )
    clock_values: dict[str, dict[str, float]] = {}
    for declaration in declarations:
        if declaration.kind is not MetricKind.PER_CLOCK:
            continue
        by_clock: dict[str, float] = {}
        for clock, timing in per_clock.items():
            value: float | None = getattr(timing, declaration.name)
            if value is not None:
                by_clock[clock] = value
        if by_clock:
            clock_values[declaration.name] = by_clock
    return FlowMetricValues(
        plain={name: value for name, value in plain.items() if value is not None},
        per_clock=clock_values,
    )
