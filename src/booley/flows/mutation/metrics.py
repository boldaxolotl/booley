"""The Mutation Flow's metric declarations, settings and report values.

A run's valid Mutants are the ones that built; each was either detected or survived,
so the score is detected over both. A run with no valid Mutant has no score.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from booley.flows.metric_declarations import (
    Better,
    CostTier,
    FlowMetricValues,
    FlowSetting,
    MetricDeclaration,
    MetricKind,
    MetricsUnavailableError,
    counted_items,
    metric_values,
    metrics_document,
)

MUTATION_METRIC_DECLARATIONS: tuple[MetricDeclaration, ...] = (
    MetricDeclaration(
        "mutation_score",
        "%",
        Better.HIGHER,
        CostTier.HIGH,
        MetricKind.PLAIN,
        minimum=0,
        maximum=100,
    ),
    MetricDeclaration(
        "mutants_detected", "Mutants", Better.HIGHER, CostTier.HIGH, MetricKind.PLAIN, minimum=0
    ),
    MetricDeclaration(
        "mutants_survived", "Mutants", Better.LOWER, CostTier.HIGH, MetricKind.PLAIN, minimum=0
    ),
)

# The ``metrics.toml`` keys of a ``mutation`` table that are options of ``booley flow mutation``.
MUTATION_SETTINGS: tuple[FlowSetting, ...] = (
    FlowSetting("count", default=10, minimum=1, meaning="Mutants to score"),
    FlowSetting("seed", default=0, minimum=0, meaning="selects the Mutants"),
)


def mutation_metric_values(detected: int, survivors: Sequence[str]) -> FlowMetricValues:
    """The typed values of a run that scored *detected* + ``len(survivors)`` valid Mutants.

    *survivors* is one display line per survived Mutant, in the run's pick order; the first
    ones are kept with the values.
    """
    survived = len(survivors)
    valid = detected + survived
    if valid == 0:
        raise MetricsUnavailableError("no valid Mutant was scored")
    values = metric_values(
        MUTATION_METRIC_DECLARATIONS,
        {
            "mutation_score": 100 * detected / valid,
            "mutants_detected": detected,
            "mutants_survived": survived,
        },
    )
    return replace(
        values, counted_items={"mutants_survived": counted_items(survivors)} if survivors else {}
    )


def mutation_metrics_detail(
    target: str, detected: int, survivors: Sequence[str]
) -> dict[str, dict[str, object]]:
    """The report's ``detail.metrics``: *target*'s values, or why it has none."""
    return {target: metrics_document(lambda: mutation_metric_values(detected, survivors))}
