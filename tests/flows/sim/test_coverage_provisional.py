"""Provisional Coverage Verdicts layer screened Waiver Candidates on strict evidence."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from types import MappingProxyType

import pytest

from booley.criteria.state import CriterionEntry
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoverageCapability,
    CoveragePoint,
    CoveragePointIdentity,
    derive_coverage_rollups,
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


def _point(
    point_id: str, *, hits: int, source: str = _SOURCE, metric: str = "line"
) -> CoveragePoint:
    return CoveragePoint(
        id=point_id,
        identity=CoveragePointIdentity(
            metric=metric,
            location=MappingProxyType({"source": source}),
            hierarchy="TOP.counter",
            subject=MappingProxyType({"id": point_id}),
            collector=MappingProxyType({"record_type": f"v_{metric}"}),
        ),
        hits_by_run=MappingProxyType({"run:smoke": hits} if hits else {}),
        disposition=MappingProxyType({"kind": "eligible"}),
    )


def _evaluated(
    minimum: int | Fraction, *, covered: int = 3, uncovered: int = 2
) -> tuple[CoverageCampaign, CoverageCriterion]:
    """Covered and uncovered line points (default 3 and 2), strictly evaluated."""
    points = (
        *(_point(f"c{index}", hits=1) for index in range(covered)),
        *(_point(f"u{index}", hits=0) for index in range(1, uncovered + 1)),
    )
    campaign = replace(
        _campaign(),
        points=points,
        source_closure=MappingProxyType(
            {"rtl": (MappingProxyType({"path": _SOURCE, "sha256": _SHA}),)}
        ),
        rollups=derive_coverage_rollups(points),
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


def test_a_candidate_rescuing_one_metric_is_needed_while_another_fails() -> None:
    """ "Needed" is judged per metric: an unrelated failing metric does not hide it."""
    points = (
        *(_point(f"c{index}", hits=1) for index in range(3)),
        _point("u1", hits=0),
        _point("bc0", hits=1, metric="branch"),
        _point("bu1", hits=0, metric="branch"),
    )
    scored = MappingProxyType({"collection": "supported", "scoring": "scored_v1"})
    base = _campaign()
    campaign = replace(
        base,
        points=points,
        collector=replace(
            base.collector,
            capabilities=tuple(
                CoverageCapability(record_class=metric, status="reported", attributes=scored)
                for metric in ("line", "branch")
            ),
        ),
        source_closure=MappingProxyType(
            {"rtl": (MappingProxyType({"path": _SOURCE, "sha256": _SHA}),)}
        ),
        rollups=derive_coverage_rollups(points),
    )
    criterion = CoverageCriterion(
        _TARGET,
        (CoverageThreshold("line", Fraction(100)), CoverageThreshold("branch", Fraction(100))),
        None,
    )
    strict = evaluate_coverage_campaign(campaign, criterion, _NO_WAIVERS)
    assert strict.evaluation["status"] == "fail"

    verdict = evaluate_provisional_coverage(strict, criterion, [_candidate("u1")])

    assert verdict.status == "fail"
    assert {row["metric"]: row["verdict"] for row in verdict.evaluation["metrics"]} == {
        "line": "pass",
        "branch": "fail",
    }
    assert _screens(verdict) == {"wc-u1": ("offered", True)}


def _duplicate_u1(points: tuple[CoveragePoint, ...]) -> tuple[CoveragePoint, ...]:
    return (*points, *(point for point in points if point.id == "u1"))


def _unscored_u1(points: tuple[CoveragePoint, ...]) -> tuple[CoveragePoint, ...]:
    return tuple(
        replace(point, disposition=MappingProxyType({"kind": "unscored"}))
        if point.id == "u1"
        else point
        for point in points
    )


def _other_source_u1(points: tuple[CoveragePoint, ...]) -> tuple[CoveragePoint, ...]:
    return tuple(
        _point("u1", hits=0, source="rtl/other.sv") if point.id == "u1" else point
        for point in points
    )


@pytest.mark.parametrize(
    ("changes", "edit_points", "status", "reason"),
    [
        (
            {"campaign_id": "campaign:other"},
            None,
            "stale",
            "Candidate belongs to a different Coverage Campaign.",
        ),
        (
            {"target_identity": "acme:demo:other:1.0#sim"},
            None,
            "invalid",
            "Candidate Target differs from the Campaign Target.",
        ),
        (
            {"point_id": "missing"},
            None,
            "invalid",
            "Candidate does not name exactly one Coverage Point.",
        ),
        (
            {"point_id": "c0"},
            None,
            "invalid",
            "Point has observed hits; it is not waivable as uncovered.",
        ),
        (
            {"source": "rtl/other.sv"},
            None,
            "invalid",
            "Candidate source digest differs from the Campaign's.",
        ),
        (
            {"source_sha256": _OTHER_SHA},
            None,
            "invalid",
            "Candidate source digest differs from the Campaign's.",
        ),
        ({}, _duplicate_u1, "invalid", "Candidate does not name exactly one Coverage Point."),
        ({}, _unscored_u1, "invalid", "Point is not an eligible scored point."),
        (
            {},
            _other_source_u1,
            "invalid",
            "Candidate source differs from the point's RTL source.",
        ),
    ],
)
def test_candidates_are_rederived_from_the_campaign(
    changes: dict[str, str], edit_points, status: str, reason: str
) -> None:
    """D1: a record is checked, not trusted — forged or stale bindings are not offered."""
    campaign, criterion = _evaluated(100)
    if edit_points is not None:
        campaign = replace(campaign, points=edit_points(campaign.points))
    candidate = _candidate("u1", **changes)

    verdict = evaluate_provisional_coverage(campaign, criterion, [candidate])

    assert verdict.status == "fail"
    assert verdict.offered_ids == ()
    (screen,) = verdict.screens
    assert (screen.status, screen.reason) == (status, reason)


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
    assert verdict.evaluation["diagnostics"][0]["message"] == (
        "Candidates leave no eligible points: line."
    )


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


def test_threshold_edges_agree_across_criterion_provisional_and_report() -> None:
    """The Criterion, the Provisional Verdict and the Job report share one scorer."""
    from booley.flows.sim.coverage_policy import coverage_metrics

    edges = [
        (1, 1, Fraction(50), "pass"),  # exact hit
        (1, 2, Fraction("33.33"), "pass"),  # decimal edge, passes
        (2, 1, Fraction("66.67"), "fail"),  # decimal edge, fails
    ]
    for covered, uncovered, minimum, verdict in edges:
        strict, _ = _evaluated(minimum, covered=covered, uncovered=uncovered)
        assert strict.evaluation["metrics"][0]["verdict"] == verdict
        assert coverage_metrics(strict)["line"]["met"] is (verdict == "pass")

        # One extra uncovered point fails the strict verdict; waiving it restores the edge.
        extra, criterion = _evaluated(minimum, covered=covered, uncovered=uncovered + 1)
        assert extra.evaluation["status"] == "fail"
        provisional = evaluate_provisional_coverage(
            extra, criterion, [_candidate(f"u{uncovered + 1}")]
        )
        assert provisional.evaluation["metrics"][0]["verdict"] == verdict
        assert provisional.status == verdict


def test_rebuild_reports_a_rejected_stored_threshold_as_provisional_error() -> None:
    campaign, _ = _evaluated(100)
    stored = {**campaign.evaluation, "thresholds": {"line": 150}}
    corrupt = replace(campaign, evaluation=MappingProxyType(stored))

    with pytest.raises(ProvisionalCoverageError, match=r"threshold is malformed.*finite numbers"):
        criterion_from_evaluation(corrupt)


@pytest.mark.parametrize("stored_minimum", ["50", True, float("nan")], ids=["str", "bool", "nan"])
def test_rebuild_reports_a_malformed_stored_threshold_as_provisional_error(
    stored_minimum: object,
) -> None:
    campaign, _ = _evaluated(100)
    stored = {**campaign.evaluation, "thresholds": {"line": stored_minimum}}
    corrupt = replace(campaign, evaluation=MappingProxyType(stored))

    with pytest.raises(ProvisionalCoverageError, match="malformed"):
        criterion_from_evaluation(corrupt)


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
