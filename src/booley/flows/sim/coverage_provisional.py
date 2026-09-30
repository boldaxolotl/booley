"""Pure Provisional Coverage Verdicts: strict evidence plus screened Waiver Candidates.

ADR 0066. The strict verdict (Approved Waiver Set only) is already persisted in
the evaluated Campaign. This module layers a Ticket's Waiver Candidates on top
of that evaluated Campaign without re-matching approved waivers, and returns an
evaluation in the strict evaluation's shape, so the existing Criterion
projection can read it unchanged. A provisional verdict never satisfies a
Criterion; it only decides whether a Ticket may go to review.

Every candidate is re-derived from the Campaign here (D1: candidate records are
checked, not trusted). A candidate is offered only when it names an eligible,
zero-hit RTL point of this exact Campaign and its source still matches.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Literal, cast

from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoveragePoint,
    DurableTargetIdentity,
    FrozenJson,
    _freeze_mapping,
)
from booley.flows.sim.coverage_policy import (
    _METRIC_ORDER,
    CoverageCriterion,
    CoverageThreshold,
    _derive_rollup,
    _json_value,
    _metric_result,
)

# Leave-one-out "needed" labelling is O(candidates x points); keep it bounded.
MAX_PROVISIONAL_CANDIDATES = 512

CandidateStatus = Literal["offered", "not_needed", "stale", "invalid"]
_WAIVABLE_REASONS = frozenset({"excluded", "unreachable"})


class ProvisionalCoverageError(ValueError):
    """Provisional evaluation inputs cannot be evaluated deterministically."""


@dataclass(frozen=True)
class ProvisionalCandidate:
    """The binding of one recorded Waiver Candidate, as the Campaign must confirm it."""

    candidate_id: str
    campaign_id: str
    target_identity: str
    point_id: str
    source: str
    source_sha256: str
    reason: str


@dataclass(frozen=True)
class CandidateScreen:
    """Why one candidate is or is not offered for approval."""

    candidate_id: str
    status: CandidateStatus
    reason: str
    needed: bool = False


@dataclass(frozen=True)
class ProvisionalVerdict:
    """Strict status, provisional evaluation, and one screen per candidate."""

    strict_status: str
    evaluation: Mapping[str, FrozenJson]
    screens: tuple[CandidateScreen, ...]

    @property
    def status(self) -> str:
        return str(self.evaluation["status"])

    @property
    def offered_ids(self) -> tuple[str, ...]:
        return tuple(item.candidate_id for item in self.screens if item.status == "offered")


def _points_by_id(campaign: CoverageCampaign) -> dict[str, list[CoveragePoint]]:
    points: dict[str, list[CoveragePoint]] = {}
    for point in campaign.points:
        points.setdefault(point.id, []).append(point)
    return points


def _campaign_sources(campaign: CoverageCampaign) -> dict[str, str]:
    records = cast(tuple[Mapping[str, str], ...], campaign.source_closure.get("rtl", ()))
    return {record["path"]: record["sha256"] for record in records}


def _binding_problem(
    candidate: ProvisionalCandidate,
    campaign: CoverageCampaign,
    current_sources: Mapping[str, str] | None,
) -> tuple[CandidateStatus, str] | None:
    """Check the candidate's Campaign, Target, reason, and source binding."""
    if candidate.campaign_id != campaign.campaign_id:
        return "stale", "Candidate belongs to a different Coverage Campaign."
    if candidate.target_identity != campaign.target.identity:
        return "invalid", "Candidate Target differs from the Campaign Target."
    if candidate.reason not in _WAIVABLE_REASONS:
        return "invalid", f"Reason {candidate.reason!r} is not waivable."
    if _campaign_sources(campaign).get(candidate.source) != candidate.source_sha256:
        return "invalid", "Candidate source digest differs from the Campaign's."
    if current_sources is not None and current_sources.get(candidate.source) != (
        candidate.source_sha256
    ):
        return "stale", "RTL source changed since the Campaign was collected."
    return None


def _point_problem(
    candidate: ProvisionalCandidate, points: Mapping[str, list[CoveragePoint]]
) -> tuple[CandidateStatus, str] | None:
    """Check that the candidate names one eligible, zero-hit point of its source."""
    matches = points.get(candidate.point_id, [])
    if len(matches) != 1:
        return "invalid", "Candidate does not name exactly one Coverage Point."
    point = matches[0]
    if point.identity.location.get("source") != candidate.source:
        return "invalid", "Candidate source differs from the point's RTL source."
    if point.disposition.get("kind") != "eligible":
        return "invalid", "Point is not an eligible scored point."
    if sum(point.hits_by_run.values()) > 0:
        return "invalid", "Point has observed hits; it is not waivable as uncovered."
    return None


def _screen_one(
    candidate: ProvisionalCandidate,
    campaign: CoverageCampaign,
    points: Mapping[str, list[CoveragePoint]],
    current_sources: Mapping[str, str] | None,
) -> CandidateScreen | None:
    """Return a rejecting screen, or None when the candidate is usable."""
    problem = _binding_problem(candidate, campaign, current_sources) or _point_problem(
        candidate, points
    )
    if problem is None:
        return None
    return CandidateScreen(candidate.candidate_id, problem[0], problem[1])


def _apply_candidates(
    campaign: CoverageCampaign, usable: Mapping[str, ProvisionalCandidate]
) -> CoverageCampaign:
    points = tuple(
        replace(
            point,
            disposition=_freeze_mapping(
                {
                    "kind": "waived",
                    "reason": usable[point.id].reason,
                    "candidate_id": usable[point.id].candidate_id,
                    "provisional": True,
                }
            ),
        )
        if point.id in usable
        else point
        for point in campaign.points
    )
    rollups = tuple(_derive_rollup(rollup, points) for rollup in campaign.rollups)
    return replace(campaign, points=points, rollups=rollups)


def _provisional_evaluation(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    usable: Mapping[str, ProvisionalCandidate],
) -> dict[str, object]:
    """Score the strict evaluation's thresholds with the usable candidates waived."""
    strict = cast(dict[str, object], _json_value(campaign.evaluation))
    waived = _apply_candidates(campaign, usable)
    by_metric = {rollup.metric: rollup for rollup in waived.rollups}
    diagnostics: list[dict[str, str]] = []
    metrics: list[dict[str, object]] = []
    for threshold in sorted(
        criterion.thresholds, key=lambda item: _METRIC_ORDER.index(item.metric)
    ):
        rollup = by_metric.get(threshold.metric)
        if rollup is None or rollup.eligible_points == 0:
            diagnostics.append(
                {
                    "code": "COV_EVAL_EMPTY_DENOMINATOR",
                    "pointer": "/rollups",
                    "message": f"Candidates leave no eligible points: {threshold.metric}.",
                }
            )
            continue
        metrics.append(_metric_result(rollup, threshold))
    if diagnostics:
        status = "blocked"
    else:
        status = "pass" if all(item["verdict"] == "pass" for item in metrics) else "fail"
    return {
        **strict,
        "status": status,
        "metrics": metrics,
        "diagnostics": diagnostics,
        "provisional": True,
        "candidate_ids": sorted(item.candidate_id for item in usable.values()),
    }


def _metric_verdicts(evaluation: Mapping[str, object]) -> dict[str, object]:
    metrics = cast(list[dict[str, object]], evaluation["metrics"])
    return {str(row["metric"]): row["verdict"] for row in metrics}


def _needed(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    usable: Mapping[str, ProvisionalCandidate],
    evaluation: Mapping[str, object],
) -> frozenset[str]:
    """Candidates whose removal alone would fail a metric that passes provisionally.

    Judged per metric, because atomic Criteria project one metric each: an
    unrelated failing metric must not hide that a candidate is needed.
    """
    passing = {
        metric for metric, verdict in _metric_verdicts(evaluation).items() if verdict == "pass"
    }
    needed = set()
    for point_id, candidate in usable.items():
        remaining = {key: item for key, item in usable.items() if key != point_id}
        without = _provisional_evaluation(campaign, criterion, remaining)
        verdicts = _metric_verdicts(without)
        if any(verdicts.get(metric) != "pass" for metric in passing):
            needed.add(candidate.candidate_id)
    return frozenset(needed)


def _usable_candidates(
    campaign: CoverageCampaign,
    candidates: Sequence[ProvisionalCandidate],
    current_sources: Mapping[str, str] | None,
) -> tuple[dict[str, ProvisionalCandidate], list[CandidateScreen]]:
    points = _points_by_id(campaign)
    usable: dict[str, ProvisionalCandidate] = {}
    screens: list[CandidateScreen] = []
    for candidate in candidates:
        rejection = _screen_one(candidate, campaign, points, current_sources)
        if rejection is None and candidate.point_id in usable:
            rejection = CandidateScreen(
                candidate.candidate_id, "invalid", "Duplicate candidate for one point."
            )
        if rejection is not None:
            screens.append(rejection)
        else:
            usable[candidate.point_id] = candidate
    return usable, screens


def _require_evaluable(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    candidates: Sequence[ProvisionalCandidate],
) -> None:
    if str(criterion.target) != campaign.target.identity:
        raise ProvisionalCoverageError("Coverage Criterion belongs to a different Target")
    if len(candidates) > MAX_PROVISIONAL_CANDIDATES:
        raise ProvisionalCoverageError(
            f"{len(candidates)} Waiver Candidates exceed the limit of "
            f"{MAX_PROVISIONAL_CANDIDATES} for one Campaign"
        )
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ProvisionalCoverageError("Waiver Candidate ids must be unique")


def evaluate_provisional_coverage(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    candidates: Sequence[ProvisionalCandidate],
    *,
    current_sources: Mapping[str, str] | None = None,
) -> ProvisionalVerdict:
    """Return the Provisional Coverage Verdict of one strictly evaluated Campaign.

    ``current_sources`` maps RTL paths to their SHA-256 at the Ticket head being
    judged; pass None only when the Campaign's own source closure is current.
    """
    _require_evaluable(campaign, criterion, candidates)
    strict_status = str(campaign.evaluation.get("status"))
    usable, screens = _usable_candidates(campaign, candidates, current_sources)
    strict = cast(Mapping[str, FrozenJson], campaign.evaluation)
    if strict_status != "fail":
        # Only a scored strict failure can be rescued: a pass needs no candidates,
        # and a blocked evaluation has diagnostics that no waiver resolves.
        status: CandidateStatus = "not_needed" if strict_status == "pass" else "invalid"
        reason = f"Strict coverage evaluation is {strict_status}."
        screens.extend(
            CandidateScreen(item.candidate_id, status, reason) for item in usable.values()
        )
        return ProvisionalVerdict(strict_status, strict, _sorted(screens))
    evaluation = _provisional_evaluation(campaign, criterion, usable)
    needed = _needed(campaign, criterion, usable, evaluation)
    screens.extend(
        CandidateScreen(
            item.candidate_id,
            "offered",
            "Eligible zero-hit point of this Campaign.",
            needed=item.candidate_id in needed,
        )
        for item in usable.values()
    )
    return ProvisionalVerdict(strict_status, _freeze_mapping(evaluation), _sorted(screens))


def criterion_from_evaluation(campaign: CoverageCampaign) -> CoverageCriterion:
    """Rebuild the Coverage Criterion a scored strict evaluation was computed with.

    Thresholds persist as exact ints or shortest-repr floats of authored decimal
    minima, so ``Fraction(str(value))`` restores the authored value.
    """
    evaluation = campaign.evaluation
    thresholds = evaluation.get("thresholds")
    suite = evaluation.get("suite")
    if evaluation.get("status") not in {"pass", "fail"} or not isinstance(thresholds, Mapping):
        raise ProvisionalCoverageError("Campaign has no scored strict coverage evaluation")
    if not isinstance(suite, Mapping) or not isinstance(suite.get("required"), tuple):
        raise ProvisionalCoverageError("Campaign evaluation has no required test suite")
    try:
        rebuilt = tuple(
            CoverageThreshold(str(metric), Fraction(str(minimum)))
            for metric, minimum in thresholds.items()
            if isinstance(minimum, int | float) and not isinstance(minimum, bool)
        )
    except (ValueError, ZeroDivisionError) as exc:
        raise ProvisionalCoverageError(f"Campaign threshold is malformed: {exc}") from exc
    if len(rebuilt) != len(thresholds) or not rebuilt:
        raise ProvisionalCoverageError("Campaign thresholds are malformed")
    required = tuple(str(item) for item in suite["required"])
    tests = None if sorted(required) == sorted(campaign.declared_tests) else required
    return CoverageCriterion(DurableTargetIdentity(campaign.target.identity), rebuilt, tests)


def _sorted(screens: list[CandidateScreen]) -> tuple[CandidateScreen, ...]:
    return tuple(sorted(screens, key=lambda item: item.candidate_id))


__all__ = [
    "MAX_PROVISIONAL_CANDIDATES",
    "CandidateScreen",
    "ProvisionalCandidate",
    "ProvisionalCoverageError",
    "ProvisionalVerdict",
    "criterion_from_evaluation",
    "evaluate_provisional_coverage",
]
