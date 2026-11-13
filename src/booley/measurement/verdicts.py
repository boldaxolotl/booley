"""Pure evaluation of one rules table against its child Flow's outcome (ADR 0092, #3507).

No I/O and no fallback to the worst clock: a per-clock metric is read from the one clock
it names, or from the only clock the Flow reported. Every other case fails closed as an
``error`` verdict, because an ``error`` means the number could not be produced.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from booley.core.exit_codes import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.flows.metric_declarations import FlowMetricValues, MetricKind, MetricValue
from booley.measurement.flow_metrics import metric_declarations
from booley.measurement.metrics_file import Bound, TableRules, TrackedMetric
from booley.measurement.run_document import MetricResult, Verdict


@dataclass(frozen=True)
class FlowValues:
    """The Flow returned typed values for the Target, whatever its exit status."""

    values: FlowMetricValues
    # Every clock in the Flow's timing report, with or without a value. Never derived from
    # ``values.per_clock``: ``metric_values`` drops a clock with no value, which would turn
    # a two-clock design into a single-clock one.
    clocks: tuple[str, ...]


@dataclass(frozen=True)
class FlowFailed:
    """The Flow returned no values for the Target."""

    excerpt: str  # why: the crash, the missing tool, the values that could not be produced


@dataclass(frozen=True)
class FlowNotStarted:
    """The Flow never started (the run stopped or was cancelled first)."""

    reason: str  # why it was not evaluated


FlowOutcome = FlowValues | FlowFailed | FlowNotStarted


def evaluate_table(rules: TableRules, outcome: FlowOutcome) -> tuple[MetricResult, ...]:
    """One result per rules entry, bounds first then tracked (the order a run serializes in)."""
    kinds = {declaration.name: declaration.kind for declaration in metric_declarations(rules.flow)}
    entries: list[Bound | TrackedMetric] = [*rules.bounds, *rules.tracked]
    if isinstance(outcome, FlowFailed):
        return tuple(
            MetricResult(entry.key, _bound_of(entry), None, Verdict.ERROR, outcome.excerpt)
            for entry in entries
        )
    if isinstance(outcome, FlowNotStarted):
        return tuple(
            MetricResult(entry.key, _bound_of(entry), None, Verdict.NOT_EVALUATED, outcome.reason)
            for entry in entries
        )
    return tuple(
        _judge(entry, *_read_value(entry, kinds[entry.metric], rules.flow, outcome))
        for entry in entries
    )


def _bound_of(entry: Bound | TrackedMetric) -> MetricValue | None:
    """The bound recorded for an enforced entry; a tracked entry has none."""
    return entry.value if isinstance(entry, Bound) else None


def _judge(
    entry: Bound | TrackedMetric, value: MetricValue | None, why: str | None
) -> MetricResult:
    """The verdict of one entry whose value was read (or could not be, with *why*)."""
    bound = _bound_of(entry)
    if value is None:
        return MetricResult(entry.key, bound, None, Verdict.ERROR, why)
    if not isinstance(entry, Bound):
        return MetricResult(entry.key, None, value, Verdict.TRACKED, None)
    if entry.limit == "max":
        met = value <= entry.value
        detail = f"{value} is above the bound {bound} (at most)"
    else:
        met = value >= entry.value
        detail = f"{value} is below the bound {bound} (at least)"
    return MetricResult(
        entry.key, bound, value, Verdict.MET if met else Verdict.UNMET, None if met else detail
    )


def _read_value(
    entry: Bound | TrackedMetric, kind: MetricKind, flow: str, outcome: FlowValues
) -> tuple[MetricValue | None, str | None]:
    """Return ``(value, None)``, or ``(None, why)`` when the value cannot be read."""
    metric = entry.metric
    values = outcome.values
    if kind is MetricKind.PLAIN:
        plain = values.plain.get(metric)
        return (plain, None) if plain is not None else (None, f"{flow} returned no {metric}")
    if kind is MetricKind.PER_TEST:
        # The loader resolves every per-test entry to a Test Run (metrics_file).
        assert entry.test_run is not None
        by_test = values.per_test.get(metric, {}).get(entry.test_run)
        if by_test is not None:
            return by_test, None
        return None, f"no {metric} for Test Run {entry.test_run}"
    return _read_clock_value(entry, outcome)


def _read_clock_value(
    entry: Bound | TrackedMetric, outcome: FlowValues
) -> tuple[MetricValue | None, str | None]:
    """Read a per-clock metric from the clock *entry* names, or from the only clock reported."""
    metric = entry.metric
    clocks = sorted(outcome.clocks)
    listed = ", ".join(clocks)
    clock = entry.prefix
    if clock is None:
        if not clocks:
            return None, "the Flow reported no clock"
        if len(clocks) > 1:
            return None, (
                f"{metric} is per clock and the Flow reported clocks {listed}; "
                f'prefix the key with one ("<clock>.{entry.key}")'
            )
        clock = clocks[0]
    elif clock not in clocks:
        reported = f"reported clocks: {listed}" if clocks else "the Flow reported no clock"
        return None, f"clock {clock} was not reported; {reported}"
    by_clock = outcome.values.per_clock.get(metric, {}).get(clock)
    if by_clock is None:
        return None, f"clock {clock} has no {metric} value"
    return by_clock, None


def measure_exit_code(verdicts: Iterable[Verdict]) -> int:
    """Exit code of a set of verdicts: 2 if any ``error``, else 1 if any ``unmet``, else 0."""
    seen = set(verdicts)
    if Verdict.ERROR in seen:
        return EXIT_ERROR
    if Verdict.UNMET in seen:
        return EXIT_FAILURE
    return EXIT_SUCCESS
