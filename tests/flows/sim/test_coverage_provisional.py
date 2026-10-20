"""Provisional Coverage Verdicts layer screened Waiver Candidates on strict evidence."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from types import MappingProxyType

import pytest

from booley.criteria.state import CriterionEntry
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoveragePoint,
    CoveragePointIdentity,
    CoverageRollup,
)
from booley.flows.sim.coverage_policy import (
    ApprovedWaiverSet,
    CoverageCriterion,
    CoverageThreshold,
    evaluate_coverage_campaign,
)
from booley.flows.sim.coverage_projection import project_coverage_criterion
from booley.flows.sim.coverage_provisional import (
    MAX_PROVISIONAL_CANDIDATES,
    ProvisionalCandidate,
    ProvisionalCoverageError,
    criterion_from_evaluation,
    evaluate_provisional_coverage,
)
from tests.flows.sim.test_coverage_waivers import _TARGET, _campaign

_SOURCE = "rtl/counter.sv"
_SHA = "sha256:" + "a" * 64
_OTHER_SHA = "sha256:" + "b" * 64
_CAMPAIGN_ID = "campaign:sim_counter:12"
_NO_WAIVERS = ApprovedWaiverSet(MappingProxyType({"enabled": False}), "sha256:none", ())


def _point(point_id: str, *, hits: int, source: str = _SOURCE) -> CoveragePoint:
    return CoveragePoint(
        id=point_id,
        identity=CoveragePointIdentity(
            metric="line",
            location=MappingProxyType({"source": source}),
            hierarchy="TOP.counter",
            subject=MappingProxyType({"id": point_id}),
            collector=MappingProxyType({"record_type": "v_line"}),
        ),
        hits_by_run=MappingProxyType({"run:smoke": hits} if hits else {}),
        disposition=MappingProxyType({"kind": "eligible"}),
    )


def _evaluated(minimum: int) -> tuple[CoverageCampaign, CoverageCriterion]:
    """Three covered and two uncovered line points, strictly evaluated."""
    points = (
        *(_point(f"c{index}", hits=1) for index in range(3)),
        _point("u1", hits=0),
        _point("u2", hits=0),
    )
    campaign = replace(
        _campaign(),
        points=points,
        source_closure=MappingProxyType(
            {"rtl": (MappingProxyType({"path": _SOURCE, "sha256": _SHA}),)}
        ),
        rollups=(CoverageRollup("line", "line semantics", 5, 5, 3, 0, 60.0),),
    )
    criterion = CoverageCriterion(_TARGET, (CoverageThreshold("line", Fraction(minimum)),), None)
    return evaluate_coverage_campaign(campaign, criterion, _NO_WAIVERS), criterion


def _candidate(point: str, **changes: str) -> ProvisionalCandidate:
    candidate = ProvisionalCandidate(
        candidate_id=f"wc-{point}",
        campaign_id=_CAMPAIGN_ID,
        target_identity=str(_TARGET),
        point_id=point,
        source=_SOURCE,
        source_sha256=_SHA,
        reason="unreachable",
    )
    return replace(candidate, **changes)


def _screens(verdict) -> dict[str, tuple[str, bool]]:
    return {item.candidate_id: (item.status, item.needed) for item in verdict.screens}


def test_candidates_rescue_a_strict_failure_and_each_is_needed() -> None:
    campaign, criterion = _evaluated(100)
    assert campaign.evaluation["status"] == "fail"

    verdict = evaluate_provisional_coverage(
        campaign, criterion, [_candidate("u1"), _candidate("u2")]
    )

    assert verdict.strict_status == "fail"
    assert verdict.status == "pass"
    assert verdict.evaluation["provisional"] is True
    assert verdict.evaluation["candidate_ids"] == ("wc-u1", "wc-u2")
    assert verdict.evaluation["metrics"][0]["eligible_points"] == 3
    assert _screens(verdict) == {"wc-u1": ("offered", True), "wc-u2": ("offered", True)}
    # The strict evidence itself is untouched.
    assert campaign.evaluation["status"] == "fail"


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"campaign_id": "campaign:other"}, "stale"),
        ({"target_identity": "acme:demo:other:1.0#sim"}, "invalid"),
        ({"point_id": "missing"}, "invalid"),
        ({"point_id": "c0"}, "invalid"),
        ({"source": "rtl/other.sv"}, "invalid"),
        ({"source_sha256": _OTHER_SHA}, "invalid"),
    ],
)
def test_candidates_are_rederived_from_the_campaign(changes: dict[str, str], status: str) -> None:
    """D1: a record is checked, not trusted — forged or stale bindings are not offered."""
    campaign, criterion = _evaluated(100)
    candidate = _candidate("u1", **changes)

    verdict = evaluate_provisional_coverage(campaign, criterion, [candidate])

    assert verdict.status == "fail"
    assert verdict.offered_ids == ()
    assert _screens(verdict)[candidate.candidate_id][0] == status


def test_changed_source_at_ticket_head_makes_a_candidate_stale() -> None:
    campaign, criterion = _evaluated(100)

    verdict = evaluate_provisional_coverage(
        campaign,
        criterion,
        [_candidate("u1"), _candidate("u2")],
        current_sources={_SOURCE: _OTHER_SHA},
    )

    assert verdict.status == "fail"
    assert {status for status, _ in _screens(verdict).values()} == {"stale"}


def test_waiving_every_point_blocks_on_an_empty_denominator() -> None:
    campaign, criterion = _evaluated(100)
    uncovered_only = replace(campaign, points=campaign.points[3:])

    verdict = evaluate_provisional_coverage(
        uncovered_only, criterion, [_candidate("u1"), _candidate("u2")]
    )

    assert verdict.status == "blocked"
    assert verdict.evaluation["diagnostics"][0]["code"] == "COV_EVAL_EMPTY_DENOMINATOR"


def test_provisional_evaluation_projects_through_the_existing_criterion_projection() -> None:
    campaign, criterion = _evaluated(100)
    verdict = evaluate_provisional_coverage(
        campaign, criterion, [_candidate("u1"), _candidate("u2")]
    )
    atomic = CriterionEntry(params={"metrics": {"line": {"min_pct": 100}}})

    assert project_coverage_criterion(atomic, verdict.evaluation, {}, atomic=True)[0] is True
    assert project_coverage_criterion(atomic, campaign.evaluation, {}, atomic=True)[0] is False


@pytest.mark.parametrize("minimum", [Fraction(100), Fraction(3333, 100), Fraction(875, 10)])
def test_criterion_round_trips_through_the_persisted_strict_evaluation(minimum: Fraction) -> None:
    campaign, _ = _evaluated(100)
    criterion = CoverageCriterion(_TARGET, (CoverageThreshold("line", minimum),), None)
    evaluated = evaluate_coverage_campaign(campaign, criterion, _NO_WAIVERS)

    assert criterion_from_evaluation(evaluated) == criterion


def test_criterion_cannot_be_rebuilt_from_an_unscored_evaluation() -> None:
    campaign, _ = _evaluated(100)
    unscored = replace(campaign, evaluation=MappingProxyType({"status": "blocked"}))

    with pytest.raises(ProvisionalCoverageError, match="no scored"):
        criterion_from_evaluation(unscored)


def test_candidate_count_cap() -> None:
    campaign, criterion = _evaluated(100)
    candidates = [_candidate(f"p{index}") for index in range(MAX_PROVISIONAL_CANDIDATES + 1)]
    with pytest.raises(ProvisionalCoverageError, match="limit"):
        evaluate_provisional_coverage(campaign, criterion, candidates)


@pytest.mark.parametrize(
    "minimum,points,strict,status,screen",
    [
        (75, ("u1", "u2"), "fail", "pass", "offered"),
        (100, ("u1",), "fail", "fail", "offered"),
        (50, ("u1",), "pass", "pass", "not_needed"),
        (100, ("u1",), "blocked", "blocked", "invalid"),
    ],
)
def test_verdict_without_needed_candidates(minimum, points, strict, status, screen):
    campaign, criterion = _evaluated(minimum)
    if strict == "blocked":
        campaign = replace(campaign, evaluation=MappingProxyType({"status": strict}))
    assert campaign.evaluation["status"] == strict
    verdict = evaluate_provisional_coverage(campaign, criterion, [_candidate(p) for p in points])
    assert verdict.status == status
    assert _screens(verdict) == {f"wc-{p}": (screen, False) for p in points}
    if strict != "fail":
        assert verdict.evaluation == campaign.evaluation
