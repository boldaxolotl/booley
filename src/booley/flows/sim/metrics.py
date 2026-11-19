"""Typed metrics of the simulation and coverage Flows (issue #3497).

A Simulation Record outcome carries one observation per Test Run, and a coverage
run also a Coverage Record. :func:`simulation_flow_metrics` turns an outcome into
the typed values the ``sim`` and ``coverage`` declarations below name. Flow-neutral
types and the emission funnel live in :mod:`booley.flows.metric_declarations`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from booley.core.boundary import as_dict, as_int
from booley.flows.eda_failures import first_tool_diagnostic
from booley.flows.metric_declarations import (
    COUNTED_ITEM_CHARS,
    COUNTED_ITEMS_CAP,
    Better,
    CostTier,
    FlowMetricValues,
    MetricDeclaration,
    MetricKind,
    MetricsUnavailable,
    MetricsUnavailableError,
    MetricValue,
    metric_values,
    metrics_from_document,
)
from booley.flows.sim.backends.cocotb_results import IMPORT_FAILURE_PREFIX
from booley.flows.sim.coverage_reference import (
    CoverageRecordReferenceError,
    resolve_coverage_record_reference,
)
from booley.flows.sim.record.coordinator import SimulationRecordOutcome
from booley.flows.sim.record.test_run_report import observation_grade

# Coverage Record metric key (``coverage_policy``) -> declared metric name.
COVERAGE_METRIC_NAMES = {
    "line": "line_coverage",
    "branch": "branch_coverage",
    "expression": "expression_coverage",
    "toggle": "toggle_coverage",
    "cover_property": "cover_property_coverage",
}


def _declarations(tier: CostTier, *, coverage: bool) -> tuple[MetricDeclaration, ...]:
    """The simulation metrics at *tier*; a coverage run adds the five coverage percentages.

    The shared names are written once so a coverage run cannot drift from a plain
    one. A coverage run observes no Cycle Count (every coverage observation carries
    ``cycle_count=None``), so ``cycle_count`` is declared for the plain run only.
    """
    shared = (
        MetricDeclaration("tests_failed", "tests", Better.LOWER, tier, MetricKind.PLAIN),
        MetricDeclaration("tests_total", "tests", Better.HIGHER, tier, MetricKind.PLAIN),
    )
    if not coverage:
        return (
            *shared,
            MetricDeclaration("cycle_count", "cycles", Better.LOWER, tier, MetricKind.PER_TEST),
        )
    return (
        *shared,
        *(
            MetricDeclaration(name, "%", Better.HIGHER, tier, MetricKind.PLAIN)
            for name in COVERAGE_METRIC_NAMES.values()
        ),
    )


# A plain simulation costs less than an instrumented build plus the whole suite.
SIM_METRIC_DECLARATIONS = _declarations(CostTier.MEDIUM, coverage=False)
COVERAGE_METRIC_DECLARATIONS = _declarations(CostTier.HIGH, coverage=True)


def coverage_record_path(outcome: SimulationRecordOutcome) -> Path:
    """Return the public ``coverage.json`` beside the Target's Simulation Record directory."""
    return outcome.manifest_path.parents[1] / "coverage.json"


def _coverage_percents(outcome: SimulationRecordOutcome) -> dict[str, MetricValue]:
    """Coverage percentages by declared name; empty unless collection completed.

    Raises :class:`MetricsUnavailableError` when the collector itself failed (an unreadable
    or incompatible native database, a failed or disagreeing merge): the run measured no
    coverage. It also raises when the coverage evaluation is blocked, naming its first
    diagnostic: the run exits 2 and its rollups cannot be trusted. A design-caused abort
    with an unblocked evaluation returns ``{}``, so the test counts stay values.

    Computed from the point counts, not the stored two-decimal ``percent``, which would
    round 20,000 of 20,001 points up to ``100.0`` where the coverage floor compares the
    exact fraction.
    """
    try:
        coverage_record = resolve_coverage_record_reference(
            coverage_record_path(outcome)
        ).loaded.coverage_record
    except CoverageRecordReferenceError as error:
        raise MetricsUnavailableError(f"the Coverage Record could not be read: {error}") from error
    collection = coverage_record.collection
    if collection["status"] != "complete":
        for diagnostic in cast("Iterable[Mapping[str, Any]]", collection["diagnostics"]):
            if diagnostic["code"] != "COV_SIMULATION_ABORTED":
                raise MetricsUnavailableError(
                    f"the coverage collection did not complete: {diagnostic['message']}"
                )
    evaluation = coverage_record.evaluation
    if evaluation["status"] == "blocked":
        first = cast("Sequence[Mapping[str, Any]]", evaluation["diagnostics"])[0]
        raise MetricsUnavailableError(
            f"the coverage evaluation was blocked: {first['code']}: {first['message']}"
        )
    if collection["status"] != "complete":
        return {}
    return {
        COVERAGE_METRIC_NAMES[rollup.metric]: rollup.covered_points * 100 / rollup.eligible_points
        for rollup in coverage_record.rollups
        if rollup.metric in COVERAGE_METRIC_NAMES and rollup.eligible_points > 0
    }


def _observation_reason(row: Mapping[str, object], limit: int | None = 500) -> str:
    """The reason text of observation *row*'s ``detail``, cut to *limit* characters."""
    reason = str((as_dict(row.get("detail")) or {}).get("reason", ""))
    return reason if limit is None else reason[:limit]


def _failing_tests(grades: Sequence[tuple[Mapping[str, object], str]]) -> list[str]:
    """The non-passing Test Runs by name, each with one reason line (the Flow caps them).

    The line is the reason's first tool error line, else its first line: a build's
    output tail starts wherever the tail was cut.
    """
    failing = [(str(row["test_run"]), row) for row, grade in grades if grade != "pass"]
    failing.sort(key=lambda item: item[0])
    lines: list[str] = []
    for name, row in failing[:COUNTED_ITEMS_CAP]:
        text = _observation_reason(row, limit=None)
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        reason = first_tool_diagnostic(text, default=first_line)[:COUNTED_ITEM_CHARS]
        lines.append(f"{name}: {reason}" if reason else name)
    return lines


def simulation_report_metric_values(
    detail: dict[str, Any],
) -> dict[str, FlowMetricValues | MetricsUnavailable]:
    """Each Target's values from a ``sim`` report's ``detail.records.<selector>.metrics``.

    A Target whose record carries no metrics entry is skipped.
    """
    values: dict[str, FlowMetricValues | MetricsUnavailable] = {}
    for selector, record in (as_dict(detail.get("records")) or {}).items():
        entry = metrics_from_document((as_dict(record) or {}).get("metrics"))
        if entry is not None:
            values[str(selector)] = entry
    return values


def simulation_flow_metrics(outcome: SimulationRecordOutcome) -> FlowMetricValues:
    """Return the typed metrics of one Target's simulation or coverage run.

    ``tests_failed`` counts every Test Run that did not pass (failed, timed out,
    crashed, not run or inconclusive).
    ``cycle_count`` is keyed by Test Run name and only for a Test Run that passed with
    a count. A coverage run adds the five coverage percentages, omitted when the
    coverage collection did not complete (a failing test is still a value).
    A collector failure or a blocked coverage evaluation raises instead. ``counted_items``
    names the first failing Test Runs, each with one line of its reason.

    Raises :class:`MetricsUnavailableError` when the run produced no verdict for every
    Test Run: the Simulation Record did not finish, a Test Run ended in infrastructure error, or
    a cocotb test module could not be imported (the run measured nothing of that test).
    """
    if not outcome.complete:
        raise MetricsUnavailableError("the Simulation Record did not finish every Test Run")
    grades = [(row, observation_grade(row)) for row in outcome.observations]
    errored = [str(row["test_run"]) for row, grade in grades if grade == "error"]
    if errored:
        first = next(row for row, grade in grades if grade == "error")
        raise MetricsUnavailableError(
            "Test Runs ended in infrastructure error, with no verdict: "
            f"{', '.join(sorted(errored))} ({_observation_reason(first)})"
        )
    for row, grade in grades:
        reason = _observation_reason(row, limit=None)
        if grade == "inconclusive" and IMPORT_FAILURE_PREFIX in reason:
            raise MetricsUnavailableError(f"Test Run {row['test_run']}: {reason}")
    plain: dict[str, MetricValue | None] = {
        "tests_failed": sum(grade != "pass" for _, grade in grades),
        "tests_total": len(grades),
    }
    is_coverage_run = outcome.coverage_reference is not None
    if is_coverage_run:
        plain |= _coverage_percents(outcome)
    cycles: dict[str, MetricValue | None] = {}
    for row, grade in grades:
        count = as_int(row["cycle_count"])  # rejects bool, which is an int subclass
        if grade == "pass" and count is not None:
            cycles[str(row["test_run"])] = count
    declarations = COVERAGE_METRIC_DECLARATIONS if is_coverage_run else SIM_METRIC_DECLARATIONS
    values = metric_values(
        declarations, plain, per_test={} if is_coverage_run else {"cycle_count": cycles}
    )
    failing = _failing_tests(grades)
    return dataclasses.replace(values, counted_items={"tests_failed": failing} if failing else {})
