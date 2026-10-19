"""Compact Job report metrics from the authoritative, evaluated Coverage Campaign."""

from fractions import Fraction

from .coverage_campaign import CoverageCampaign


def coverage_metrics(campaign: CoverageCampaign) -> dict[str, dict[str, object]]:
    """Keep metric threshold results independent from the aggregate Criterion verdict.

    Rounded display percentages cannot decide a threshold edge. Empty denominators
    retain their absent percentage and cannot meet a configured floor.
    """
    thresholds = campaign.evaluation["thresholds"]
    metrics = {}
    for rollup in campaign.rollups:
        threshold = thresholds.get(rollup.metric)
        met = None
        if threshold is not None:
            met = bool(rollup.eligible_points) and (
                Fraction(rollup.covered_points * 100, rollup.eligible_points)
                >= Fraction(str(threshold))
            )
        metrics[rollup.metric] = {
            "percent": rollup.percent,
            "threshold": threshold,
            "met": met,
        }
    return metrics
