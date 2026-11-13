"""Typed metric declarations shared by every Flow that measures something.

A Flow declares each metric it emits (name, unit, better direction, cost tier,
kind) next to the code that measures it. A run returns its numbers through
:func:`metric_values`, the one emission funnel, which refuses a metric the Flow
did not declare. Flow-neutral: it imports no concrete Flow. The cross-Flow
registry lives in :mod:`booley.measurement.flow_metrics`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from booley.core.boundary import as_dict
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
    per_test: dict[str, dict[str, MetricValue]] = field(
        default_factory=dict[str, dict[str, MetricValue]]
    )  # metric name -> Test Run name -> value
    # Every clock the run reported timing for, valued or not: a clock with no fmax is
    # absent from ``per_clock`` but still named here.
    clocks: tuple[str, ...] = ()


class UndeclaredMetricError(ValueError):
    """A Flow emitted a metric it did not declare."""


class MetricsUnavailableError(RuntimeError):
    """A run produced no metric value at all; the message says why.

    Raised instead of returning an empty or partial result when the run could not
    be measured (ADR 0092: an error means the number could not be produced).
    """


def metric_values(
    declarations: Sequence[MetricDeclaration],
    plain: Mapping[str, MetricValue | None],
    per_clock: Mapping[str, ClockTiming] | None = None,
    per_test: Mapping[str, Mapping[str, MetricValue | None]] | None = None,
) -> FlowMetricValues:
    """Build the typed values of one run from the Flow's own numbers.

    ``plain`` maps declared ``PLAIN`` names to their values. Every ``PER_CLOCK``
    declaration is read from the same-named :class:`ClockTiming` field of each
    clock, so per-clock names exist only in the declarations. A value that is
    ``None`` was not measured by this run and is omitted. ``per_test`` maps a
    declared ``PER_TEST`` name to its value by Test Run name; a metric left with no
    value is omitted.
    """
    per_clock = per_clock or {}
    per_test = per_test or {}
    plain_names = {d.name for d in declarations if d.kind is MetricKind.PLAIN}
    for name in plain:
        if name not in plain_names:
            raise UndeclaredMetricError(
                f"metric {name!r} is not a declared plain metric; declared: {sorted(plain_names)}"
            )
    per_test_names = {d.name for d in declarations if d.kind is MetricKind.PER_TEST}
    for name in per_test:
        if name not in per_test_names:
            raise UndeclaredMetricError(
                f"metric {name!r} is not a declared per-test metric; "
                f"declared: {sorted(per_test_names)}"
            )
    test_values: dict[str, dict[str, MetricValue]] = {}
    for name, by_name in per_test.items():
        by_test = {test: value for test, value in by_name.items() if value is not None}
        if by_test:
            test_values[name] = by_test
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
        per_test=test_values,
        clocks=tuple(per_clock),
    )


@dataclass(frozen=True)
class MetricsUnavailable:
    """A Target whose values the run could not produce; *reason* says why."""

    reason: str


def metrics_document(measure: Callable[[], FlowMetricValues]) -> dict[str, object]:
    """The ``report.json`` form of one Target's values, or of why it has none.

    Returns the four value maps of *measure*'s result, or ``{"unavailable": reason}``
    when *measure* raises :class:`MetricsUnavailableError`.
    """
    try:
        values = measure()
    except MetricsUnavailableError as error:
        return {"unavailable": str(error)}
    return {
        "plain": values.plain,
        "per_clock": values.per_clock,
        "per_test": values.per_test,
        "clocks": list(values.clocks),
    }


def metrics_from_document(raw: object) -> FlowMetricValues | MetricsUnavailable | None:
    """Read back what :func:`metrics_document` wrote; ``None`` when *raw* is no such entry.

    The entry came from this code, so its fields are trusted (no field-level re-proof).
    """
    document = as_dict(raw)
    if document is None:
        return None
    if "unavailable" in document:
        return MetricsUnavailable(str(document["unavailable"]))
    return FlowMetricValues(
        plain=dict(document.get("plain", {})),
        per_clock={
            name: dict(by_clock) for name, by_clock in document.get("per_clock", {}).items()
        },
        per_test={name: dict(by_test) for name, by_test in document.get("per_test", {}).items()},
        clocks=tuple(document.get("clocks", ())),
    )
