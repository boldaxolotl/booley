"""Bounded model-facing views over one fully validated Coverage Campaign."""

import json
from dataclasses import replace

import pytest

from booley.core.boundary import BoundaryError
from booley.flows.sim.coverage_analysis_input import CoverageSourceClosure
from booley.flows.sim.coverage_campaign import (
    DurableTargetIdentity,
    decode_coverage_campaign,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_evidence import (
    MAX_RESPONSE_BYTES,
    CoverageEvidenceSession,
    decode_coverage_evidence_audit,
)
from tests.flows.sim.test_coverage_campaign import _valid_document


def _campaign_with_waiver():
    document = _valid_document()
    document["points"][0]["hits_by_run"] = {}
    document["points"][0]["disposition"] = {
        "kind": "waived",
        "reason": "unreachable",
        "waiver_id": "waiver:counter-line",
        "waiver_file": "coverage/waivers.toml",
        "waiver_fingerprint": "sha256:" + "8" * 64,
        "provenance": {
            "justification": "Reset prevents this state",
            "approved_by": "reviewer",
            "approved_at": "2026-09-10T08:00:00Z",
            "approval_ref": "review:42",
            "proof": {"kind": "formal", "reference": "proof:counter"},
        },
    }
    document["rollups"][0].update(
        eligible_points=0, covered_points=0, waived_points=1, percent=None
    )
    return decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )


def test_points_view_keeps_complete_approved_waiver_disposition():
    campaign = _campaign_with_waiver()
    session = CoverageEvidenceSession(campaign, None)

    result = session.query({"view": "points", "disposition": "waived"})

    assert result["matched_points"] == 1
    disposition = result["points"][0]["disposition"]
    assert disposition["kind"] == "waived"
    assert disposition["reason"] == "unreachable"
    assert disposition["waiver_id"] == "waiver:counter-line"
    assert disposition["provenance"]["proof"]["reference"] == "proof:counter"


def test_overview_counts_waivers_without_expanding_every_point():
    session = CoverageEvidenceSession(_campaign_with_waiver(), None)

    result = session.query({"view": "overview"})

    assert result["point_counts"] == {"waived": 1}
    assert result["uncovered_eligible_by_metric"] == {}
    assert result["tests"]["runs"][0]["simulation_verdict"] == "pass"


def test_verbose_overview_falls_back_to_bounded_rollups():
    document = _valid_document()
    document["collection"]["diagnostics"] = ["x" * 100_000]
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )

    result = CoverageEvidenceSession(campaign, None).query({"view": "overview"})

    assert result["overview_truncated"] is True
    assert result["rollups"][0]["total_points"] == 1
    assert len(json.dumps(result).encode()) <= MAX_RESPONSE_BYTES


def test_points_filter_and_cursor_are_deterministic():
    campaign = _campaign_with_waiver()
    session = CoverageEvidenceSession(campaign, None)

    first = session.query({"view": "points", "disposition": "waived", "limit": 1})
    repeated = CoverageEvidenceSession(campaign, None).query(
        {"view": "points", "disposition": "waived", "limit": 1}
    )

    assert first == repeated
    assert first["next_cursor"] is None


def test_model_facing_points_use_stable_short_references_and_audit_exact_ids():
    campaign = _campaign_with_waiver()
    point = campaign.points[0]
    session = CoverageEvidenceSession(campaign, None)

    first = session.query({"view": "points", "disposition": "waived"})
    repeated = session.query({"view": "points", "disposition": "waived"})

    assert first["points"][0]["point_ref"] == "point:1"
    assert "id" not in first["points"][0]
    assert repeated["points"][0]["point_ref"] == "point:1"
    assert session.analysis_scope()["point_references"] == {"point:1": point.id}


def test_complete_evidence_audit_is_validated_into_public_and_private_parts():
    campaign = _campaign_with_waiver()
    session = CoverageEvidenceSession(campaign, None)
    session.query({"view": "points", "disposition": "waived"})

    audit = decode_coverage_evidence_audit(session.analysis_scope())

    assert audit.analysis_scope["point_ids"] == (campaign.points[0].id,)
    assert audit.point_references == {"point:1": campaign.points[0].id}
    assert "point_references" not in audit.analysis_scope


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("total_points", True),
        ("budget_exhausted", "false"),
        ("queries", [{}]),
        ("point_ids", [7]),
    ],
)
def test_evidence_audit_rejects_invalid_persisted_fields(field, invalid):
    session = CoverageEvidenceSession(_campaign_with_waiver(), None)
    session.query({"view": "points", "disposition": "waived"})
    audit = session.analysis_scope()
    audit[field] = invalid

    with pytest.raises(BoundaryError):
        decode_coverage_evidence_audit(audit)


def test_rejected_delivery_does_not_authorize_point_reference():
    campaign = _campaign_with_waiver()
    session = CoverageEvidenceSession(campaign, None, total_budget_bytes=1)

    result = session.query({"view": "points", "disposition": "waived"})

    assert result["error"] == "evidence_budget_exhausted"
    assert session.analysis_scope()["point_references"] == {}
    with pytest.raises(ValueError, match="Unknown Coverage Point reference"):
        session.query({"view": "points", "point_refs": ["point:1"]})


def test_oversized_response_does_not_authorize_point_reference():
    campaign = _campaign_with_waiver()
    point = campaign.points[0]
    identity = replace(
        point.identity,
        collector=freeze_coverage_mapping(
            {"record_type": "line", "native_key": "x" * (MAX_RESPONSE_BYTES + 1)}
        ),
    )
    session = CoverageEvidenceSession(
        replace(campaign, points=(replace(point, identity=identity),)), None
    )

    with pytest.raises(ValueError, match="exceeds the response limit"):
        session.query({"view": "points", "limit": 1})

    assert session.analysis_scope()["point_references"] == {}


def test_points_cursor_advances_within_the_same_filtered_query():
    campaign = _campaign_with_waiver()
    point = campaign.points[0]
    campaign = replace(campaign, points=(point, replace(point, id=f"{point.id}-second")))
    session = CoverageEvidenceSession(campaign, None)

    first = session.query({"view": "points", "disposition": "waived", "limit": 1})
    second = session.query(
        {
            "view": "points",
            "disposition": "waived",
            "limit": 1,
            "cursor": first["next_cursor"],
        }
    )

    assert first["points"][0]["point_ref"] != second["points"][0]["point_ref"]
    assert "id" not in first["points"][0]
    assert "id" not in second["points"][0]
    assert second["next_cursor"] is None


def test_points_cursor_cannot_be_reused_with_different_filters():
    campaign = _campaign_with_waiver()
    point = campaign.points[0]
    campaign = replace(campaign, points=(point, replace(point, id=f"{point.id}-second")))
    session = CoverageEvidenceSession(campaign, None)
    first = session.query({"view": "points", "disposition": "waived", "limit": 1})

    with pytest.raises(ValueError, match="does not match this query"):
        session.query(
            {
                "view": "points",
                "disposition": "eligible",
                "limit": 1,
                "cursor": first["next_cursor"],
            }
        )


def test_points_encode_only_returned_pages_and_reuse_filtered_selection(monkeypatch):
    from booley.flows.sim import coverage_evidence

    campaign = _campaign_with_waiver()
    point = campaign.points[0]
    campaign = replace(campaign, points=(point, replace(point, id=f"{point.id}-second")))
    match_calls = 0
    encode_calls = 0
    match = coverage_evidence._matches
    encode = coverage_evidence.encode_coverage_point

    def counted_match(point, request):
        nonlocal match_calls
        match_calls += 1
        return match(point, request)

    def counted_encode(point):
        nonlocal encode_calls
        encode_calls += 1
        return encode(point)

    monkeypatch.setattr(coverage_evidence, "_matches", counted_match)
    monkeypatch.setattr(coverage_evidence, "encode_coverage_point", counted_encode)
    session = CoverageEvidenceSession(campaign, None)

    first = session.query({"view": "points", "disposition": "waived", "limit": 1})
    after_first = match_calls
    second = session.query(
        {
            "view": "points",
            "disposition": "waived",
            "limit": 1,
            "cursor": first["next_cursor"],
        }
    )

    assert after_first == len(campaign.points)
    assert match_calls == after_first
    assert encode_calls == len(first["points"]) + len(second["points"])


def test_points_covered_false_filter_is_not_treated_as_unfiltered():
    campaign = _campaign_with_waiver()
    point = campaign.points[0]
    covered = replace(point, id=f"{point.id}-covered", hits_by_run={"run:reset": 1})
    uncovered = replace(point, id=f"{point.id}-uncovered", hits_by_run={})
    campaign = replace(campaign, points=(covered, uncovered))
    session = CoverageEvidenceSession(campaign, None)

    result = session.query({"view": "points", "covered": False})

    assert result["matched_points"] == 1
    assert session.analysis_scope()["point_ids"] == [uncovered.id]


def test_source_view_is_limited_to_exact_campaign_point_ids():
    document = _valid_document()
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    source = "\n".join(f"line {line}" for line in range(1, 21)) + "\n"
    point = campaign.points[0]
    point = replace(
        point,
        identity=replace(
            point.identity,
            location=freeze_coverage_mapping(
                {
                    "source": "rtl/counter.sv",
                    "start": {"line": 10, "column": 3},
                    "end": {"line": 10, "column": 28},
                }
            ),
        ),
    )
    campaign = replace(campaign, points=(point,))
    sources = CoverageSourceClosure(
        campaign.target.identity,
        freeze_coverage_mapping(
            {
                "rtl/counter.sv": {
                    "category": "rtl",
                    "sha256": "sha256:" + "6" * 64,
                    "text": source,
                }
            }
        ),
    )
    session = CoverageEvidenceSession(campaign, sources)

    result = session.query({"view": "source", "point_ids": [point.id], "context_lines": 2})

    assert result["excerpts"][0]["start_line"] == 8
    assert result["excerpts"][0]["end_line"] == 12
    assert "line 10" in result["excerpts"][0]["text"]


def test_source_view_resolves_only_previously_delivered_point_references():
    document = _valid_document()
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    source = "\n".join(f"line {line}" for line in range(1, 21)) + "\n"
    sources = CoverageSourceClosure(
        campaign.target.identity,
        freeze_coverage_mapping(
            {
                "rtl/counter.sv": {
                    "category": "rtl",
                    "sha256": "sha256:" + "6" * 64,
                    "text": source,
                }
            }
        ),
    )
    session = CoverageEvidenceSession(campaign, sources)

    with pytest.raises(ValueError, match="Unknown Coverage Point reference"):
        session.query({"view": "source", "point_refs": ["point:1"]})
    points = session.query({"view": "points", "limit": 1})
    point_ref = points["points"][0]["point_ref"]
    result = session.query({"view": "source", "point_refs": [point_ref]})

    assert result["excerpts"][0]["point_ref"] == point_ref
    assert "point_id" not in result["excerpts"][0]


def test_source_view_preserves_requested_order_and_duplicates():
    document = _valid_document()
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    first = campaign.points[0]
    second = replace(first, id=f"{first.id}-second")
    campaign = replace(campaign, points=(first, second))
    sources = CoverageSourceClosure(
        campaign.target.identity,
        freeze_coverage_mapping(
            {
                "rtl/counter.sv": {
                    "category": "rtl",
                    "sha256": "sha256:" + "6" * 64,
                    "text": "module counter; endmodule\n",
                }
            }
        ),
    )
    session = CoverageEvidenceSession(campaign, sources)

    session.query({"view": "source", "point_ids": [second.id, first.id, second.id]})

    assert session.analysis_scope()["queries"][-1]["point_ids"] == [
        second.id,
        first.id,
        second.id,
    ]


def test_source_view_unknown_id_depends_on_verified_source_access():
    campaign = _campaign_with_waiver()

    assert CoverageEvidenceSession(campaign, None).query(
        {"view": "source", "point_ids": ["unknown"]}
    ) == {"source_access": "report_only", "excerpts": []}

    sources = CoverageSourceClosure(
        campaign.target.identity,
        freeze_coverage_mapping(
            {
                "rtl/counter.sv": {
                    "category": "rtl",
                    "sha256": "sha256:" + "6" * 64,
                    "text": "module counter; endmodule\n",
                }
            }
        ),
    )
    with pytest.raises(ValueError, match="Unknown Coverage Point"):
        CoverageEvidenceSession(campaign, sources).query(
            {"view": "source", "point_ids": ["unknown"]}
        )


def test_session_enforces_the_persisted_point_ceiling(monkeypatch):
    from booley.flows.sim import coverage_evidence

    campaign = _campaign_with_waiver()
    point = campaign.points[0]
    campaign = replace(campaign, points=(point, replace(point, id=f"{point.id}-second")))
    monkeypatch.setattr(coverage_evidence, "MAX_POINTS", 1)

    with pytest.raises(ValueError, match="session limit"):
        CoverageEvidenceSession(campaign, None)


def test_cumulative_budget_stops_unbounded_model_retrieval():
    campaign = _campaign_with_waiver()
    session = CoverageEvidenceSession(campaign, None, total_budget_bytes=1_500)

    first = session.query({"view": "points", "disposition": "waived"})
    second = session.query({"view": "points", "disposition": "waived"})

    assert first["points"]
    assert second == {
        "error": "evidence_budget_exhausted",
        "delivered_bytes": session.delivered_bytes,
        "total_budget_bytes": 1_500,
    }


def test_bound_tool_deep_loads_campaign_and_publishes_audit(tmp_path, monkeypatch):
    from booley.mcp import coverage_evidence
    from tests.mcp_tools.test_coverage_analyst import source_project

    campaign_path = source_project(tmp_path)
    audit_path = tmp_path / "audit.json"
    monkeypatch.setenv("BOOLEY_COVERAGE_CAMPAIGN", str(campaign_path))
    monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", str(tmp_path))
    monkeypatch.setenv("BOOLEY_COVERAGE_AUDIT", str(audit_path))
    monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
    monkeypatch.setattr(coverage_evidence, "_TERMINAL_ERROR", None)

    result = coverage_evidence.query_active_coverage_evidence({"view": "overview"})

    assert result["campaign_id"].startswith("campaign:")
    audit = json.loads(audit_path.read_text())
    assert audit["queries"] == [{"point_ids": [], "returned": 0, "view": "overview"}]
    assert audit["point_references"] == {}
    assert audit["evidence_bytes_delivered"] > 0


def test_bound_tool_uses_audit_path_as_session_identity(tmp_path, monkeypatch):
    from booley.mcp import coverage_evidence
    from tests.mcp_tools.test_coverage_analyst import source_project

    campaign_path = source_project(tmp_path)
    monkeypatch.setenv("BOOLEY_COVERAGE_CAMPAIGN", str(campaign_path))
    monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", str(tmp_path))
    monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
    monkeypatch.setattr(coverage_evidence, "_TERMINAL_ERROR", None)
    monkeypatch.setenv("BOOLEY_COVERAGE_AUDIT", str(tmp_path / "first-audit.json"))
    result = coverage_evidence.query_active_coverage_evidence({"view": "points", "limit": 1})
    point_ref = result["points"][0]["point_ref"]

    monkeypatch.setenv("BOOLEY_COVERAGE_AUDIT", str(tmp_path / "second-audit.json"))
    with pytest.raises(ValueError, match="Unknown Coverage Point reference"):
        coverage_evidence.query_active_coverage_evidence(
            {"view": "source", "point_refs": [point_ref]}
        )


def test_bound_tool_exposes_nothing_when_final_point_storage_is_corrupt(tmp_path, monkeypatch):
    from booley.mcp import coverage_evidence
    from tests.mcp_tools.test_coverage_analyst import source_project

    campaign_path = source_project(tmp_path)
    point_path = campaign_path.with_name("coverage-points.jsonl.gz")
    stored = bytearray(point_path.read_bytes())
    stored[-1] ^= 1
    point_path.write_bytes(stored)
    monkeypatch.setenv("BOOLEY_COVERAGE_CAMPAIGN", str(campaign_path))
    monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", str(tmp_path))
    monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
    monkeypatch.setattr(coverage_evidence, "_TERMINAL_ERROR", None)

    with pytest.raises(ValueError, match=r"COV_POINT_(FORMAT|INTEGRITY)"):
        coverage_evidence.query_active_coverage_evidence({"view": "overview"})


def test_bound_tool_audits_terminal_query_rejection(tmp_path, monkeypatch):
    from booley.mcp import coverage_evidence
    from tests.mcp_tools.test_coverage_analyst import source_project

    campaign_path = source_project(tmp_path)
    audit_path = tmp_path / "audit.json"
    monkeypatch.setenv("BOOLEY_COVERAGE_CAMPAIGN", str(campaign_path))
    monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", str(tmp_path))
    monkeypatch.setenv("BOOLEY_COVERAGE_AUDIT", str(audit_path))
    monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
    monkeypatch.setattr(coverage_evidence, "_TERMINAL_ERROR", None)

    with pytest.raises(ValueError, match="Unknown evidence query fields"):
        coverage_evidence.query_active_coverage_evidence({"view": "points", "unsupported": True})

    audit = json.loads(audit_path.read_text())
    assert "Unknown evidence query fields" in audit["terminal_error"]
    with pytest.raises(ValueError, match="Evidence session already failed"):
        coverage_evidence.query_active_coverage_evidence({"view": "overview"})
    assert "Unknown evidence query fields" in json.loads(audit_path.read_text())["terminal_error"]


def _campaign_with_source_gaps(count=157):
    from booley.flows.sim.coverage_campaign import CoverageArtifact, CoverageFinding
    from booley.flows.sim.coverage_source_gaps import GAP_CODE

    campaign = _campaign_with_waiver()
    paths = [f"rtl/file_{index:04d}.sv" for index in range(count)]
    closure = freeze_coverage_mapping(
        {"rtl": [{"path": p, "sha256": "sha256:" + "a" * 64} for p in paths], "testbench": []}
    )
    artifact = CoverageArtifact(
        "artifact:inventory",
        "declaration_inventory",
        "declarations/inventory.json",
        "sha256:" + "a" * 64,
        1,
        "fresh_queryable",
        None,
        freeze_coverage_mapping({"discovery_status": "complete"}),
    )
    unrelated = tuple(
        CoverageFinding("warning", "OTHER", "/collection", "unrelated") for _ in range(130)
    )
    gaps = tuple(
        CoverageFinding(
            "warning", GAP_CODE, f"/source_closure/rtl/{i}/path", "not used for attribution"
        )
        for i in range(count)
    )
    return replace(
        campaign,
        source_closure=closure,
        artifacts=(*campaign.artifacts, artifact),
        findings=(*unrelated, *gaps),
    ), paths


def test_zero_point_sources_are_fully_pageable_without_source_access_or_point_references():
    campaign, paths = _campaign_with_source_gaps()
    session = CoverageEvidenceSession(campaign, None)
    overview = session.query({"view": "overview"})["rtl_sources_without_points"]
    assert overview["total_sources"] == len(paths)
    assert overview["omitted"] == len(paths) - 50
    assert overview["discovery_status"] == "complete"
    retrieved, cursor = [], None
    for _ in range(4):
        request = {"view": "zero_point_sources", "limit": 50}
        if cursor:
            request["cursor"] = cursor
        page = session.query(request)
        retrieved.extend(page["paths"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert retrieved == paths
    audit = session.analysis_scope()
    decoded = decode_coverage_evidence_audit(audit)
    assert decoded.analysis_scope["points_retrieved"] == 0
    assert sum(
        q["returned"]
        for q in decoded.analysis_scope["queries"]
        if q["view"] == "zero_point_sources"
    ) == len(paths)
    assert all(not q["point_ids"] for q in audit["queries"])


def test_zero_point_sources_cursor_and_budget_preserve_audit_contract():
    campaign, _paths = _campaign_with_source_gaps()
    session = CoverageEvidenceSession(campaign, None, total_budget_bytes=1)
    assert session.query({"view": "zero_point_sources"})["error"] == "evidence_budget_exhausted"
    decode_coverage_evidence_audit(session.analysis_scope())
    session = CoverageEvidenceSession(campaign, None)
    first = session.query({"view": "zero_point_sources", "limit": 10})
    with pytest.raises(ValueError, match="cursor"):
        session.query(
            {
                "view": "zero_point_sources",
                "limit": 20,
                "cursor": "wrong:" + first["next_cursor"].split(":")[1],
            }
        )


def test_minimal_overview_retains_discovery_and_count():
    from booley.flows.sim.coverage_evidence import _minimal_overview

    campaign, paths = _campaign_with_source_gaps()
    overview = CoverageEvidenceSession(campaign, None).query({"view": "overview"})
    minimal = _minimal_overview(overview)["rtl_sources_without_points"]
    assert minimal["discovery_status"] == "complete"
    assert minimal["total_sources"] == len(paths)
    assert minimal["omitted"] == len(paths)
    assert minimal["query"]["view"] == "zero_point_sources"


def test_malformed_source_gap_pointers_do_not_invalidate_scored_evidence():
    from booley.flows.sim.coverage_campaign import CoverageFinding
    from booley.flows.sim.coverage_source_gaps import GAP_CODE, source_gap_summary

    campaign, paths = _campaign_with_source_gaps(1)
    campaign = replace(
        campaign,
        findings=(
            *campaign.findings,
            CoverageFinding("warning", GAP_CODE, "/source_closure/rtl/999/path", "bad"),
            campaign.findings[-1],
        ),
    )
    summary = source_gap_summary(campaign)
    assert summary.paths == tuple(paths)
    assert summary.unusable_findings == 2
