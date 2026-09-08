"""Report-driven Analyst behavior at its agreed deep-module seam."""

from booley.flows.sim.coverage_campaign import DurableTargetIdentity, decode_coverage_campaign
from booley.specialists.coverage_analysis import CoverageAnalyzer
from tests.flows.sim.test_coverage_campaign import _valid_document


def test_exact_eligible_campaign_produces_versioned_advisory_report():
    campaign = decode_coverage_campaign(
        _valid_document(), DurableTargetIdentity("acme:demo:counter:1.0#sim_counter")
    )
    calls = []

    def model(prompt):
        calls.append(prompt)
        return {"hypotheses": [], "recommendations": [], "waiver_candidates": []}

    report = CoverageAnalyzer(model).analyze_coverage_campaign(campaign, None, "Explain gaps")
    document = report.to_dict()
    assert document["$schema"] == "booley.coverage-analysis/v1"
    assert document["eligibility"] == "eligible"
    assert document["closure_recommendation"] == "ungated_no_recommendation"
    assert document["observed_evidence"]["evaluation"]["status"] == "not_requested"
    assert document["source_access"] == "report_only"
    assert len(calls) == 1
    assert "Explain gaps" in calls[0]


import json

import pytest

from booley.specialists.coverage_analysis import CoverageAnalysisError


def test_incomplete_usable_campaign_keeps_independent_simulation_truth():
    document = _valid_document()
    document["collection"]["status"] = "incomplete"
    document["normalization"]["status"] = "partial"
    document["tests"]["runs"][0]["simulation_verdict"] = "fail"
    document["collector"]["capabilities"][0].update(collection="supported", scoring="scored_v1")
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    report = (
        CoverageAnalyzer(
            lambda prompt: {"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        )
        .analyze_coverage_campaign(campaign, None, "")
        .to_dict()
    )
    assert report["eligibility"] == "eligible_with_limits"
    assert any("incomplete" in item for item in report["limitations"])
    assert report["observed_evidence"]["tests"]["runs"][0]["simulation_verdict"] == "fail"
    assert report["closure_recommendation"] == "ungated_no_recommendation"


@pytest.mark.parametrize("change", ["nonterminal", "point_free", "incompatible", "invalid"])
def test_unusable_campaign_rejects_before_model_invocation(change):
    document = _valid_document()
    if change == "nonterminal":
        document["collection"]["status"] = "running"
    elif change == "invalid":
        document["$schema"] = "wrong"
    else:
        document["points"] = []
        document["rollups"] = []
        if change == "incompatible":
            document["collector"]["native_format"]["compatibility"] = "incompatible"
            document["normalization"]["status"] = "incompatible"
            document["collection"]["status"] = "incompatible"
            document["evaluation"]["status"] = "blocked"
    calls = []
    with pytest.raises(ValueError):
        campaign = decode_coverage_campaign(
            document, DurableTargetIdentity(document["target"]["identity"])
        )
        CoverageAnalyzer(calls.append).analyze_coverage_campaign(campaign, None, "")
    assert calls == []


@pytest.mark.parametrize(
    "response",
    [
        {
            "observed_evidence": {},
            "hypotheses": [],
            "recommendations": [],
            "waiver_candidates": [],
        },
        {
            "hypotheses": [{"point_ids": ["invented"], "explanation": "guess"}],
            "recommendations": [],
            "waiver_candidates": [],
        },
        {"hypotheses": [], "recommendations": "write tests", "waiver_candidates": []},
    ],
)
def test_model_cannot_overwrite_observations_or_invent_references(response):
    document = _valid_document()
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    with pytest.raises(CoverageAnalysisError):
        CoverageAnalyzer(lambda prompt: response).analyze_coverage_campaign(campaign, None, "")


@pytest.mark.parametrize(
    "point,reason,evidence,proof,expected",
    [
        ("known", "excluded", "Unused hardware", "", "investigate"),
        ("unknown", "excluded", "Unused hardware", "", "forbidden"),
        ("known", "constant", "", "", "forbidden"),
        ("known", "unreachable", "Hypothesis", "", "investigate"),
    ],
)
def test_candidates_are_screened_and_never_approved(point, reason, evidence, proof, expected):
    document = _valid_document()
    point_id = document["points"][0]["id"] if point == "known" else "unknown"
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    candidate = {
        "point_id": point_id,
        "reason": reason,
        "evidence": evidence,
        "proof_reference": proof,
    }
    response = {"hypotheses": [], "recommendations": [], "waiver_candidates": [candidate]}
    report = (
        CoverageAnalyzer(lambda prompt: response)
        .analyze_coverage_campaign(campaign, None, "")
        .to_dict()
    )
    screened = report["waiver_candidates"][0]
    assert screened["screening"] == expected
    assert screened["approval"] == "not_approved"
    assert report["observed_evidence"]["points"][0]["disposition"] == {"kind": "eligible"}


def test_forged_source_snapshot_cannot_be_supplied_to_model():
    from booley.specialists.coverage_analysis import CoverageSourceClosure

    document = _valid_document()
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    snapshot = CoverageSourceClosure(
        campaign.target.identity,
        {
            "rtl/counter.sv": {
                "text": "SECRET",
                "category": "rtl",
                "sha256": document["source_closure"]["rtl"][0]["sha256"],
            }
        },
    )
    prompts = []

    def model(prompt):
        prompts.append(json.loads(prompt))
        return {"hypotheses": [], "recommendations": [], "waiver_candidates": []}

    report = CoverageAnalyzer(model).analyze_coverage_campaign(campaign, snapshot, "").to_dict()
    assert report["source_access"] == "report_only"
    assert prompts[0]["sources"] is None


@pytest.mark.parametrize(
    "status,recommendation",
    [
        ("pass", "coverage_ready"),
        ("fail", "coverage_not_ready"),
        ("blocked", "coverage_evidence_blocked"),
        ("not_requested", "ungated_no_recommendation"),
    ],
)
def test_closure_recommendations_use_persisted_policy_not_simulation(status, recommendation):
    from dataclasses import replace
    from fractions import Fraction

    from booley.flows.sim.coverage_campaign import derive_coverage_rollups, freeze_coverage_mapping
    from booley.flows.sim.coverage_policy import (
        ApprovedWaiverSet,
        CoverageCriterion,
        CoverageThreshold,
        evaluate_coverage_campaign,
    )

    document = _valid_document()
    document["tests"]["runs"][0]["simulation_verdict"] = "fail"
    document["collector"]["capabilities"][0].update(collection="supported", scoring="scored_v1")
    target = DurableTargetIdentity(document["target"]["identity"])
    campaign = decode_coverage_campaign(document, target)
    if status == "fail":
        points = tuple(
            replace(point, hits_by_run=freeze_coverage_mapping({})) for point in campaign.points
        )
        campaign = replace(campaign, points=points, rollups=derive_coverage_rollups(points))
    criterion = (
        None
        if status == "not_requested"
        else CoverageCriterion(
            target,
            (CoverageThreshold("branch" if status == "blocked" else "line", Fraction(100)),),
            None,
        )
    )
    campaign = evaluate_coverage_campaign(
        campaign, criterion, ApprovedWaiverSet({}, "sha256:" + "a" * 64, ())
    )
    report = (
        CoverageAnalyzer(
            lambda prompt: {"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        )
        .analyze_coverage_campaign(campaign, None, "")
        .to_dict()
    )
    assert report["closure_recommendation"] == recommendation
    assert report["observed_evidence"]["tests"]["runs"][0]["simulation_verdict"] == "fail"


@pytest.mark.parametrize("kind", ["testbench", "fsm"])
def test_non_rtl_and_unscored_native_points_cannot_be_waiver_candidates(kind):
    from dataclasses import replace

    from booley.flows.sim.coverage_campaign import derive_coverage_rollups
    from tests.flows.sim.test_coverage_campaign import _FSM_POINT_ID, _TESTBENCH_POINT_ID

    document = _valid_document()
    point = document["points"][0]
    if kind == "testbench":
        point["id"] = _TESTBENCH_POINT_ID
        point["identity"]["location"]["source"] = "tb/counter_tb.sv"
    else:
        point["id"] = _FSM_POINT_ID
        point["identity"]["metric"] = "fsm"
        point["identity"]["subject"] = {"machine": "counter_state", "transition": "RUN_to_WRAP"}
        point["identity"]["collector"] = {"record_type": "v_fsm", "native_key": "10:fsm-run-wrap"}
        document["collector"]["capabilities"] = [{"record_class": "fsm", "status": "reported"}]
    point["disposition"] = {"kind": "unscored", "reason": "outside_v1_scoring"}
    # Build the new rollup using the public codec/model seam before analysis.
    from booley.flows.sim.coverage_campaign import (
        CoveragePoint,
        CoveragePointIdentity,
        freeze_coverage_mapping,
    )

    identity = point["identity"]
    observation = CoveragePoint(
        point["id"],
        CoveragePointIdentity(
            identity["metric"],
            freeze_coverage_mapping(identity["location"]),
            identity["hierarchy"],
            freeze_coverage_mapping(identity["subject"]),
            freeze_coverage_mapping(identity["collector"]),
        ),
        point["hits_by_run"],
        freeze_coverage_mapping(point["disposition"]),
    )
    from booley.flows.sim.coverage_campaign import encode_coverage_campaign

    base = decode_coverage_campaign(
        _valid_document(), DurableTargetIdentity(document["target"]["identity"])
    )
    document["rollups"] = encode_coverage_campaign(
        replace(base, points=(observation,), rollups=derive_coverage_rollups((observation,)))
    )["rollups"]
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    response = {
        "hypotheses": [],
        "recommendations": [],
        "waiver_candidates": [
            {
                "point_id": point["id"],
                "reason": "excluded",
                "evidence": "Claim",
                "proof_reference": "",
            }
        ],
    }
    report = (
        CoverageAnalyzer(lambda prompt: response)
        .analyze_coverage_campaign(campaign, None, "")
        .to_dict()
    )
    assert report["waiver_candidates"][0]["screening"] == "forbidden"
