"""Pure deterministic policy evaluation for Coverage Campaigns."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import TypeAlias

from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoveragePoint,
    CoverageRollup,
    DurableTargetIdentity,
    FrozenJson,
    _freeze_mapping,
)

_CRITERION_SCHEMA = "booley.coverage-criterion/v1"
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


@dataclass(frozen=True)
class ApprovedWaiver:
    """One already-validated exact Target-and-point exclusion."""

    target: DurableTargetIdentity
    point_id: str
    reason: str
    waiver_id: str
    waiver_file: str
    waiver_fingerprint: str
    provenance: Mapping[str, FrozenJson]


@dataclass(frozen=True)
class ApprovedWaiverSet:
    """Immutable project-wide waiver input produced by the Phase 2B loader."""

    configuration: Mapping[str, FrozenJson]
    digest: str
    waivers: tuple[ApprovedWaiver, ...]


EvaluatedCoverageCampaign: TypeAlias = CoverageCampaign


def _json_value(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _number(value: Fraction) -> int | float:
    return value.numerator if value.denominator == 1 else float(value)


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
        "coverage_campaign_schema": campaign.schema,
        "target": str(criterion.target),
        "metrics": thresholds,
        "tests": sorted(required_tests),
        "waiver_configuration": _json_value(waivers.configuration),
        "approved_waiver_set_digest": waivers.digest,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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
        "minimum_percent": _number(threshold.minimum_percent),
        "verdict": "pass" if actual >= threshold.minimum_percent else "fail",
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


def _apply_waivers(campaign: CoverageCampaign, approved: ApprovedWaiverSet) -> CoverageCampaign:
    by_point = {
        waiver.point_id: waiver
        for waiver in approved.waivers
        if str(waiver.target) == campaign.target.identity
    }
    points = tuple(
        replace(
            point,
            disposition=_freeze_mapping(
                {
                    "kind": "waived",
                    "reason": by_point[point.id].reason,
                    "waiver_id": by_point[point.id].waiver_id,
                    "waiver_file": by_point[point.id].waiver_file,
                    "waiver_fingerprint": by_point[point.id].waiver_fingerprint,
                    "provenance": _json_value(by_point[point.id].provenance),
                }
            ),
        )
        if point.id in by_point and point.disposition.get("kind") == "eligible"
        else point
        for point in campaign.points
    )
    rollups = tuple(_derive_rollup(rollup, points) for rollup in campaign.rollups)
    return replace(campaign, points=points, rollups=rollups)


def _derive_rollup(rollup: CoverageRollup, points: tuple[CoveragePoint, ...]) -> CoverageRollup:
    matching = [point for point in points if point.identity.metric == rollup.metric]
    eligible = [point for point in matching if point.disposition.get("kind") == "eligible"]
    waived = [point for point in matching if point.disposition.get("kind") == "waived"]
    covered = [point for point in eligible if sum(point.hits_by_run.values()) > 0]
    percent = round(len(covered) * 100 / len(eligible), 2) if eligible else None
    return replace(
        rollup,
        total_points=len(matching),
        eligible_points=len(eligible),
        covered_points=len(covered),
        waived_points=len(waived),
        percent=percent,
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
        "thresholds": {item.metric: _number(item.minimum_percent) for item in ordered},
        "metrics": [],
        "diagnostics": diagnostics,
    }
    return replace(campaign, evaluation=_freeze_mapping(evaluation))


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


def _empty_denominator_diagnostics(
    campaign: CoverageCampaign, criterion: CoverageCriterion
) -> list[dict[str, str]]:
    by_metric = {rollup.metric: rollup for rollup in campaign.rollups}
    empty = [
        item.metric
        for item in criterion.thresholds
        if item.metric not in by_metric or by_metric[item.metric].eligible_points == 0
    ]
    return [
        _diagnostic(
            "COV_EVAL_EMPTY_DENOMINATOR",
            "/rollups",
            f"Configured metric has no eligible points: {metric}.",
        )
        for metric in sorted(empty, key=_METRIC_ORDER.index)
    ]


def _ungated_campaign(
    campaign: CoverageCampaign, approved_waivers: ApprovedWaiverSet
) -> EvaluatedCoverageCampaign:
    if approved_waivers.waivers:
        raise ValueError("ungated coverage evaluation requires an empty approved waiver set")
    evaluation = {
        "status": "not_requested",
        "criterion_fingerprint": None,
        "suite": {"status": "not_evaluated"},
        "thresholds": {},
        "metrics": [],
        "diagnostics": [],
    }
    return replace(campaign, evaluation=_freeze_mapping(evaluation))


def _scored_campaign(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    approved_waivers: ApprovedWaiverSet,
    required_tests: tuple[str, ...],
) -> EvaluatedCoverageCampaign:
    by_metric = {rollup.metric: rollup for rollup in campaign.rollups}
    ordered = sorted(criterion.thresholds, key=lambda item: _METRIC_ORDER.index(item.metric))
    results = [_metric_result(by_metric[item.metric], item) for item in ordered]
    status = "pass" if all(item["verdict"] == "pass" for item in results) else "fail"
    evaluation = {
        "status": status,
        "criterion_fingerprint": _criterion_fingerprint(
            campaign, criterion, required_tests, approved_waivers
        ),
        "approved_waiver_set_digest": approved_waivers.digest,
        "suite": {
            "status": "match",
            "required": sorted(required_tests),
            "selected": sorted(campaign.selected_tests),
        },
        "thresholds": {item.metric: _number(item.minimum_percent) for item in ordered},
        "metrics": results,
        "diagnostics": [],
    }
    return replace(campaign, evaluation=_freeze_mapping(evaluation))


def evaluate_coverage_campaign(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion | None,
    approved_waivers: ApprovedWaiverSet,
) -> EvaluatedCoverageCampaign:
    """Return a new immutable Campaign with deterministic policy evidence."""
    if criterion is None:
        return _ungated_campaign(campaign, approved_waivers)
    required_tests = campaign.declared_tests if criterion.tests is None else criterion.tests
    campaign = _apply_waivers(campaign, approved_waivers)
    diagnostics = _evidence_diagnostics(campaign, criterion, required_tests)
    diagnostics.extend(_empty_denominator_diagnostics(campaign, criterion))
    if diagnostics:
        return _blocked_campaign(
            campaign,
            criterion,
            approved_waivers,
            required_tests,
            diagnostics,
        )
    return _scored_campaign(campaign, criterion, approved_waivers, required_tests)
