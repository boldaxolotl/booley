"""Typed metrics of the simulation and coverage Flows (issue #3497).

A Simulation Campaign outcome carries one observation per Test Run, and a coverage
run also a Coverage Campaign. :func:`simulation_flow_metrics` turns an outcome into
the typed values the ``sim`` and ``coverage`` declarations below name. Flow-neutral
types and the emission funnel live in :mod:`booley.flows.metric_declarations`.
"""

from __future__ import annotations

from pathlib import Path

from booley.core.boundary import as_int
from booley.flows.metric_declarations import (
    Better,
    CostTier,
    FlowMetricValues,
    MetricDeclaration,
    MetricKind,
    MetricsUnavailableError,
    MetricValue,
    metric_values,
)
from booley.flows.sim.campaign.coordinator import CampaignOutcome
from booley.flows.sim.campaign.test_run_report import observation_grade
from booley.flows.sim.coverage_reference import (
    CoverageCampaignReferenceError,
    resolve_coverage_campaign_reference,
)

# Coverage Campaign metric key (``coverage_policy``) -> declared metric name.
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


def coverage_campaign_path(outcome: CampaignOutcome) -> Path:
    """Return the public ``coverage.json`` beside the Target's Campaign directory."""
    return outcome.manifest_path.parents[1] / "coverage.json"


def _coverage_percents(outcome: CampaignOutcome) -> dict[str, MetricValue]:
    """Coverage percentages by declared name; empty unless collection completed.

    Computed from the point counts, not the stored two-decimal ``percent``, which would
    round 20,000 of 20,001 points up to ``100.0`` where the coverage floor compares the
    exact fraction.
    """
    try:
        campaign = resolve_coverage_campaign_reference(
            coverage_campaign_path(outcome)
        ).loaded.campaign
    except CoverageCampaignReferenceError as error:
        raise MetricsUnavailableError(
            f"the Coverage Campaign could not be read: {error}"
        ) from error
    if campaign.collection["status"] != "complete":
        return {}
    return {
        COVERAGE_METRIC_NAMES[rollup.metric]: rollup.covered_points * 100 / rollup.eligible_points
        for rollup in campaign.rollups
        if rollup.metric in COVERAGE_METRIC_NAMES and rollup.eligible_points > 0
    }


def simulation_flow_metrics(outcome: CampaignOutcome) -> FlowMetricValues:
    """Return the typed metrics of one Target's simulation or coverage run.

    ``tests_failed`` counts every Test Run that did not pass (failed, timed out,
    crashed, not run or inconclusive), as Goal Mode's failed-test count does.
    ``cycle_count`` is keyed by Test Run name and only for a Test Run that passed with
    a count. A coverage run adds the five coverage percentages, omitted when the
    coverage collection did not complete (a failing test is still a value).

    Raises :class:`MetricsUnavailableError` when the run produced no verdict for every
    Test Run: the Campaign did not finish, or a Test Run ended in infrastructure error.
    """
    if not outcome.complete:
        raise MetricsUnavailableError("the simulation campaign did not finish every Test Run")
    grades = [(row, observation_grade(row)) for row in outcome.observations]
    errored = [str(row["test_run"]) for row, grade in grades if grade == "error"]
    if errored:
        raise MetricsUnavailableError(
            f"Test Runs ended in infrastructure error, with no verdict: {', '.join(sorted(errored))}"
        )
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
    return metric_values(
        declarations, plain, per_test={} if is_coverage_run else {"cycle_count": cycles}
    )
