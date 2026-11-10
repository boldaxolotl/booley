"""Pure deterministic policy evaluation for Coverage Campaigns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any, TypeAlias

from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoverageRollup,
    DurableTargetIdentity,
    apply_point_dispositions,
)
from booley.flows.sim.coverage_json import (
    CRITERION_FINGERPRINT_JSON,
    content_digest,
    coverage_mapping_document,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_waivers import ApprovedWaiver, ApprovedWaiverSet

__all__ = [
    "ApprovedWaiver",
    "ApprovedWaiverSet",
    "CoverageCriterion",
    "CoverageThreshold",
    "EvaluatedCoverageCampaign",
    "ThresholdScore",
    "coverage_metrics",
    "coverage_threshold",
    "evaluate_coverage_campaign",
    "evaluation_json",
    "score_thresholds",
    "strict_reevaluation",
]

_CRITERION_SCHEMA = "booley.coverage-criterion/v1"
_COVERAGE_SEMANTICS = "booley.coverage-semantics/v1"
_METRIC_ORDER = ("line", "branch", "expression", "toggle", "cover_property")


@dataclass(frozen=True)
class CoverageThreshold:
    """One exact inclusive minimum for a scored V1 metric."""

    metric: str
    minimum_percent: Fraction


@dataclass(frozen=True)
class CoverageCriterion:
    """One expanded Target-bound coverage policy."""

    target: DurableTargetIdentity
    thresholds: tuple[CoverageThreshold, ...]
    tests: tuple[str, ...] | None

    def __post_init__(self) -> None:
        """Reject a Criterion no evaluation could score or persist exactly."""
        metrics = [item.metric for item in self.thresholds]
        if not metrics or len(set(metrics)) != len(metrics) or set(metrics) - set(_METRIC_ORDER):
            raise ValueError("Coverage Criterion must name unique V1 metrics")
        for item in self.thresholds:
            value = item.minimum_percent
            if isinstance(value, bool) or not 0 < value <= 100:
                raise ValueError("Coverage thresholds must be finite numbers in (0, 100]")
            if coverage_threshold(item.metric, threshold_number(value)).minimum_percent != value:
                raise ValueError(
                    f"Coverage threshold for {item.metric} does not round-trip "
                    f"through its persisted number: {value}"
                )


EvaluatedCoverageCampaign: TypeAlias = CoverageCampaign


def threshold_number(value: Fraction) -> int | float:
    return value.numerator if value.denominator == 1 else float(value)


def coverage_threshold(metric: str, stored: int | float) -> CoverageThreshold:
    """Recover a threshold from its persisted number: the one recovery rule."""
    return CoverageThreshold(metric, Fraction(str(stored)))


def _criterion_fingerprint(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    required_tests: tuple[str, ...],
    waivers: ApprovedWaiverSet,
) -> str:
    thresholds = {
        item.metric: {
            "numerator": item.minimum_percent.numerator,
            "denominator": item.minimum_percent.denominator,
        }
        for item in sorted(criterion.thresholds, key=lambda item: _METRIC_ORDER.index(item.metric))
    }
    payload = {
        "$schema": _CRITERION_SCHEMA,
        "coverage_semantics": _COVERAGE_SEMANTICS,
        "target": str(criterion.target),
        "metrics": thresholds,
        "tests": sorted(required_tests),
        "waiver_configuration": waivers.configuration,
        "approved_waiver_set_digest": waivers.digest,
    }
    return content_digest(CRITERION_FINGERPRINT_JSON.encode(payload))


def _metric_result(rollup: CoverageRollup, threshold: CoverageThreshold) -> dict[str, object]:
    actual = Fraction(rollup.covered_points * 100, rollup.eligible_points)
    return {
        "metric": threshold.metric,
        "total_points": rollup.total_points,
        "eligible_points": rollup.eligible_points,
        "covered_points": rollup.covered_points,
        "waived_points": rollup.waived_points,
        "actual_numerator": rollup.covered_points * 100,
        "actual_denominator": rollup.eligible_points,
        "actual_percent": rollup.percent,
        "minimum_percent": threshold_number(threshold.minimum_percent),
        "verdict": "pass" if actual >= threshold.minimum_percent else "fail",
    }


@dataclass(frozen=True)
class ThresholdScore:
    """Per-metric rows of one scoring, the metrics it could not score, and the verdict."""

    metrics: tuple[dict[str, object], ...]
    empty: tuple[str, ...]
    status: str


def score_thresholds(
    rollups: tuple[CoverageRollup, ...], thresholds: tuple[CoverageThreshold, ...]
) -> ThresholdScore:
    """Score *thresholds* against *rollups*: the one exact scorer.

    Each threshold whose rollup has eligible points gets a row in metric order;
    the others are listed in ``empty``. ``status`` is ``pass`` only with at least
    one row, no ``empty`` metric and every row passing. It is meaningful only for
    a non-empty threshold set (Criterion construction guarantees one) with no
    ``empty`` metrics, which callers block on. The rounded percent never decides.
    """
    by_metric = {rollup.metric: rollup for rollup in rollups}
    rows: list[dict[str, object]] = []
    empty: list[str] = []
    for threshold in sorted(thresholds, key=lambda item: _METRIC_ORDER.index(item.metric)):
        rollup = by_metric.get(threshold.metric)
        if rollup is None or rollup.eligible_points == 0:
            empty.append(threshold.metric)
        else:
            rows.append(_metric_result(rollup, threshold))
    passed = bool(rows) and not empty and all(row["verdict"] == "pass" for row in rows)
    return ThresholdScore(tuple(rows), tuple(empty), "pass" if passed else "fail")


def coverage_metrics(campaign: CoverageCampaign) -> dict[str, dict[str, object]]:
    """Report per-metric threshold results for the Job report.

    Independent of the aggregate Criterion verdict, so blocked evaluations keep
    ``met``. Reads only the score's rows: a thresholded metric with no row has an
    empty denominator and cannot meet its floor.
    """
    stored = campaign.evaluation["thresholds"]
    thresholds = tuple(
        coverage_threshold(rollup.metric, stored[rollup.metric])
        for rollup in campaign.rollups
        if stored.get(rollup.metric) is not None
    )
    rows = score_thresholds(campaign.rollups, thresholds).metrics
    passed = {row["metric"] for row in rows if row["verdict"] == "pass"}
    return {
        rollup.metric: {
            "percent": rollup.percent,
            "threshold": stored.get(rollup.metric),
            "met": None if stored.get(rollup.metric) is None else rollup.metric in passed,
        }
        for rollup in campaign.rollups
    }


def _diagnostic(code: str, pointer: str, message: str) -> dict[str, str]:
    return {"code": code, "pointer": pointer, "message": message}


def _metric_available(campaign: CoverageCampaign, metric: str) -> bool:
    return any(
        capability.record_class == metric
        and capability.status == "reported"
        and capability.attributes.get("collection") == "supported"
        and capability.attributes.get("scoring") == "scored_v1"
        for capability in campaign.collector.capabilities
    )


def _blocked_campaign(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    waivers: ApprovedWaiverSet,
    required_tests: tuple[str, ...],
    diagnostics: list[dict[str, str]],
) -> EvaluatedCoverageCampaign:
    ordered = sorted(criterion.thresholds, key=lambda item: _METRIC_ORDER.index(item.metric))
    evaluation = {
        "status": "blocked",
        "criterion_fingerprint": _criterion_fingerprint(
            campaign, criterion, required_tests, waivers
        ),
        "approved_waiver_set_digest": waivers.digest,
        "suite": {
            "status": "mismatch"
            if sorted(required_tests) != sorted(campaign.selected_tests)
            else "match",
            "required": sorted(required_tests),
            "selected": sorted(campaign.selected_tests),
        },
        "thresholds": {item.metric: threshold_number(item.minimum_percent) for item in ordered},
        "metrics": [],
        "diagnostics": diagnostics,
    }
    return replace(campaign, evaluation=freeze_coverage_mapping(evaluation))


def _target_suite_diagnostics(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    required_tests: tuple[str, ...],
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if str(criterion.target) != campaign.target.identity:
        diagnostics.append(
            _diagnostic(
                "COV_EVAL_TARGET_MISMATCH",
                "/target/identity",
                "Campaign and Coverage Criterion belong to different Targets.",
            )
        )
    if sorted(required_tests) != sorted(campaign.selected_tests):
        diagnostics.append(
            _diagnostic(
                "COV_EVAL_SUITE_MISMATCH",
                "/tests/selected",
                "Campaign selection does not match the Coverage Criterion suite.",
            )
        )
    return diagnostics


def _evidence_diagnostics(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    required_tests: tuple[str, ...],
) -> list[dict[str, str]]:
    diagnostics = _target_suite_diagnostics(campaign, criterion, required_tests)
    if campaign.collection.get("status") != "complete":
        diagnostics.append(
            _diagnostic(
                "COV_EVAL_COLLECTION_INCOMPLETE",
                "/collection/status",
                "Coverage collection is not complete.",
            )
        )
    if campaign.normalization.get("status") == "incompatible":
        diagnostics.append(
            _diagnostic(
                "COV_EVAL_NORMALIZATION_INCOMPATIBLE",
                "/normalization/status",
                "Coverage normalization is incompatible.",
            )
        )
    unavailable = [
        item.metric
        for item in criterion.thresholds
        if not _metric_available(campaign, item.metric)
    ]
    diagnostics.extend(
        _diagnostic(
            "COV_EVAL_METRIC_UNAVAILABLE",
            "/collector/capabilities",
            f"Configured metric is unavailable for V1 scoring: {metric}.",
        )
        for metric in sorted(unavailable, key=_METRIC_ORDER.index)
    )
    return diagnostics


def _ungated_campaign(
    campaign: CoverageCampaign, diagnostics: list[dict[str, str]]
) -> EvaluatedCoverageCampaign:
    """Report a Campaign with no Criterion.

    Approved waivers have already been applied by the caller: they need no
    Criterion, only thresholds do. Waiver match findings block evaluation.
    """
    evaluation = {
        "status": "blocked" if diagnostics else "not_requested",
        "criterion_fingerprint": None,
        "suite": {"status": "not_evaluated"},
        "thresholds": {},
        "metrics": [],
        "diagnostics": diagnostics,
    }
    return replace(campaign, evaluation=freeze_coverage_mapping(evaluation))


def _scored_campaign(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    approved_waivers: ApprovedWaiverSet,
    required_tests: tuple[str, ...],
    score: ThresholdScore,
) -> EvaluatedCoverageCampaign:
    ordered = sorted(criterion.thresholds, key=lambda item: _METRIC_ORDER.index(item.metric))
    evaluation = {
        "status": score.status,
        "criterion_fingerprint": _criterion_fingerprint(
            campaign, criterion, required_tests, approved_waivers
        ),
        "approved_waiver_set_digest": approved_waivers.digest,
        "suite": {
            "status": "match",
            "required": sorted(required_tests),
            "selected": sorted(campaign.selected_tests),
        },
        "thresholds": {item.metric: threshold_number(item.minimum_percent) for item in ordered},
        "metrics": list(score.metrics),
        "diagnostics": [],
    }
    return replace(campaign, evaluation=freeze_coverage_mapping(evaluation))


def evaluate_coverage_campaign(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion | None,
    approved_waivers: ApprovedWaiverSet,
) -> EvaluatedCoverageCampaign:
    """Return a new immutable Campaign with deterministic policy evidence."""
    waiver_match = approved_waivers.match(campaign)
    campaign = apply_point_dispositions(
        campaign,
        {
            waiver.point_id: {
                "kind": "waived",
                "reason": waiver.reason,
                "waiver_id": waiver.waiver_id,
                "waiver_file": waiver.waiver_file,
                "waiver_fingerprint": waiver.waiver_fingerprint,
                "provenance": waiver.provenance,
            }
            for waiver in waiver_match.waivers
        },
    )
    diagnostics = [
        _diagnostic(item.code, item.pointer, item.message) for item in waiver_match.findings
    ]
    if criterion is None:
        return _ungated_campaign(campaign, diagnostics)
    required_tests = campaign.declared_tests if criterion.tests is None else criterion.tests
    diagnostics.extend(_evidence_diagnostics(campaign, criterion, required_tests))
    score = score_thresholds(campaign.rollups, criterion.thresholds)
    if (
        campaign.collection.get("status") == "complete"
        and campaign.normalization.get("status") != "incompatible"
    ):
        diagnostics.extend(
            _diagnostic(
                "COV_EVAL_EMPTY_DENOMINATOR",
                "/rollups",
                f"Configured metric has no eligible points: {metric}.",
            )
            for metric in score.empty
        )
    if diagnostics:
        return _blocked_campaign(
            campaign,
            criterion,
            approved_waivers,
            required_tests,
            diagnostics,
        )
    return _scored_campaign(campaign, criterion, approved_waivers, required_tests, score)


def _without_approved_waivers(campaign: CoverageCampaign) -> CoverageCampaign:
    """The Campaign before any Approved Waiver was applied to its points."""
    return apply_point_dispositions(
        campaign,
        {
            point.id: {"kind": "eligible"}
            for point in campaign.points
            if point.disposition.get("kind") == "waived" and "waiver_id" in point.disposition
        },
    )


def strict_reevaluation(
    campaign: CoverageCampaign, waivers: ApprovedWaiverSet, *, criterion: CoverageCriterion
) -> EvaluatedCoverageCampaign:
    """Evaluate a persisted Campaign strictly against a new Approved Waiver Set."""
    return evaluate_coverage_campaign(_without_approved_waivers(campaign), criterion, waivers)


def evaluation_json(campaign: EvaluatedCoverageCampaign) -> Mapping[str, Any]:
    """Plain JSON of an evaluation, as Criterion details store it."""
    return coverage_mapping_document(campaign.evaluation)
