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
from dataclasses import dataclass
from typing import Literal, cast

from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoveragePoint,
    DurableTargetIdentity,
    apply_point_dispositions,
)
from booley.flows.sim.coverage_json import (
    FrozenJson,
    coverage_mapping_document,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_policy import (
    CoverageCriterion,
    coverage_threshold,
    score_thresholds,
)
from booley.flows.sim.coverage_waivers import WaivablePointProblem, waivable_point_problem

# Leave-one-out "needed" labelling is O(candidates x points); keep it bounded.
MAX_PROVISIONAL_CANDIDATES = 512

CandidateStatus = Literal["offered", "not_needed", "stale", "invalid"]


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
    """Check the candidate's Campaign, Target, and source binding."""
    if candidate.campaign_id != campaign.campaign_id:
        return "stale", "Candidate belongs to a different Coverage Campaign."
    if candidate.target_identity != campaign.target.identity:
        return "invalid", "Candidate Target differs from the Campaign Target."
    if _campaign_sources(campaign).get(candidate.source) != candidate.source_sha256:
        return "invalid", "Candidate source digest differs from the Campaign's."
    if current_sources is not None and current_sources.get(candidate.source) != (
        candidate.source_sha256
    ):
        return "stale", "RTL source changed since the Campaign was collected."
    return None


_POINT_SCREENS: Mapping[WaivablePointProblem, str] = {
    "missing": "Candidate does not name exactly one Coverage Point.",
    "ambiguous": "Candidate does not name exactly one Coverage Point.",
    "source": "Candidate source differs from the point's RTL source.",
    "ineligible": "Point is not an eligible scored point.",
    "hit": "Point has observed hits; it is not waivable as uncovered.",
}


def _point_problem(
    candidate: ProvisionalCandidate, points: Mapping[str, list[CoveragePoint]]
) -> tuple[CandidateStatus, str] | None:
    """Check that the candidate names one eligible, zero-hit point of its source."""
    problem = waivable_point_problem(
        points.get(candidate.point_id, []), {candidate.source}, zero_hits=True
    )
    return None if problem is None else ("invalid", _POINT_SCREENS[problem])


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
    return apply_point_dispositions(
        campaign,
        {
            point_id: {
                "kind": "waived",
                "reason": candidate.reason,
                "candidate_id": candidate.candidate_id,
                "provisional": True,
            }
            for point_id, candidate in usable.items()
        },
    )


def _provisional_evaluation(
    campaign: CoverageCampaign,
    criterion: CoverageCriterion,
    usable: Mapping[str, ProvisionalCandidate],
) -> dict[str, object]:
    """Score the strict evaluation's thresholds with the usable candidates waived."""
    strict = coverage_mapping_document(campaign.evaluation)
    waived = _apply_candidates(campaign, usable)
    score = score_thresholds(waived.rollups, criterion.thresholds)
    diagnostics = [
        {
            "code": "COV_EVAL_EMPTY_DENOMINATOR",
            "pointer": "/rollups",
            "message": f"Candidates leave no eligible points: {metric}.",
        }
        for metric in score.empty
    ]
    return {
        **strict,
        "status": "blocked" if diagnostics else score.status,
        "metrics": list(score.metrics),
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
        if rejection is not None:
            screens.append(rejection)
        else:
            usable[candidate.point_id] = candidate
    return usable, screens


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
    if len(candidates) > MAX_PROVISIONAL_CANDIDATES:
        raise ProvisionalCoverageError(
            f"{len(candidates)} Waiver Candidates exceed the limit of "
            f"{MAX_PROVISIONAL_CANDIDATES} for one Campaign"
        )
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
    return ProvisionalVerdict(strict_status, freeze_coverage_mapping(evaluation), _sorted(screens))


def criterion_from_evaluation(campaign: CoverageCampaign) -> CoverageCriterion:
    """Rebuild the Coverage Criterion a scored strict evaluation was computed with.

    Thresholds persist as exact ints or shortest-repr floats of authored decimal
    minima, so ``coverage_threshold`` restores the authored value. Criterion
    construction rejects a stored value that is out of range or malformed.
    """
    evaluation = campaign.evaluation
    thresholds = evaluation.get("thresholds")
    suite = evaluation.get("suite")
    if evaluation.get("status") not in {"pass", "fail"} or not isinstance(thresholds, Mapping):
        raise ProvisionalCoverageError("Campaign has no scored strict coverage evaluation")
    if not isinstance(suite, Mapping) or not isinstance(suite.get("required"), tuple):
        raise ProvisionalCoverageError("Campaign evaluation has no required test suite")
    stored = [
        (str(metric), minimum)
        for metric, minimum in thresholds.items()
        if isinstance(minimum, int | float) and not isinstance(minimum, bool)
    ]
    if len(stored) != len(thresholds):
        raise ProvisionalCoverageError("Campaign thresholds are malformed")
    required = tuple(str(item) for item in suite["required"])
    tests = None if sorted(required) == sorted(campaign.declared_tests) else required
    try:
        rebuilt = tuple(coverage_threshold(metric, minimum) for metric, minimum in stored)
        return CoverageCriterion(DurableTargetIdentity(campaign.target.identity), rebuilt, tests)
    except ValueError as exc:
        raise ProvisionalCoverageError(f"Campaign threshold is malformed: {exc}") from exc


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
