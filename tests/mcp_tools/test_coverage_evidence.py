"""Bounded model-facing views over one fully validated Coverage Campaign."""

import json
from dataclasses import replace

import pytest

from booley.flows.sim.coverage_analysis_input import CoverageSourceClosure
from booley.flows.sim.coverage_campaign import (
    DurableTargetIdentity,
    decode_coverage_campaign,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_evidence import MAX_RESPONSE_BYTES, CoverageEvidenceSession
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
    assert audit["evidence_bytes_delivered"] > 0


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
