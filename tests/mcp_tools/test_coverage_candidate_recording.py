"""The Coverage Analyst records screened Waiver Candidates for its Goal Record (ADR 0066)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from booley.core.models import AgentResult
from booley.flows.sim import waiver_candidates as store
from booley.flows.sim.coverage_analysis_input import read_coverage_campaign
from booley.flows.sim.coverage_campaign import CoverageCampaign
from booley.specialists import coverage_analyst as analyst_module
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from booley.specialists.coverage_candidate_recording import (
    GoalCandidateContext,
    candidate_summary_line,
    record_goal_candidates,
)
from tests.mcp_tools.test_coverage_analyst import source_project

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _strict_failure(campaign: CoverageCampaign) -> CoverageCampaign:
    """The fixture point uncovered, plus one covered point, under a failing 100% line gate."""
    point = replace(campaign.points[0], hits_by_run=MappingProxyType({}))
    covered = replace(campaign.points[0], id=f"{campaign.points[0].id}-covered")
    evaluation = {
        "status": "fail",
        "thresholds": MappingProxyType({"line": 100}),
        "suite": MappingProxyType(
            {"status": "match", "required": campaign.declared_tests, "selected": ()}
        ),
        "metrics": (),
        "diagnostics": (),
    }
    return replace(campaign, points=(point, covered), evaluation=MappingProxyType(evaluation))


def _screened(point_id: str, source_sha256: str, screening: str = "ready_for_human_review"):
    return [
        {
            "point_id": point_id,
            "reason": "unreachable",
            "evidence": "Reset-only branch; tied off in this configuration.",
            "proof_reference": "",
            "source_fingerprint": source_sha256,
            "screening": screening,
        }
    ]


def _record_candidates(
    context, loaded, campaign, path, sha, *, invocation_id="7", screening="ready_for_human_review"
):
    return record_goal_candidates(
        context,
        campaign,
        loaded.summary,
        path,
        _screened(campaign.points[0].id, sha, screening),
        invocation_id=invocation_id,
        now=_NOW,
    )


@pytest.fixture
def record_context(tmp_path: Path):
    path = source_project(tmp_path)
    loaded = read_coverage_campaign(path)
    context = GoalCandidateContext(tmp_path / "reports", tmp_path / "record")
    campaign = _strict_failure(loaded.campaign)
    source_sha256 = str(campaign.source_closure["rtl"][0]["sha256"])
    return context, loaded, campaign, path, source_sha256


def test_ready_candidates_are_recorded_and_rescue_the_strict_verdict(record_context) -> None:
    context, loaded, campaign, path, sha = record_context

    section = _record_candidates(context, loaded, campaign, path, sha)

    assert section["status"] == "recorded", section
    assert section["recorded"] == 1
    assert section["verdicts"]["strict"] == "fail"
    assert section["verdicts"]["provisional"] == "pass"
    (candidate,) = section["verdicts"]["candidates"]
    assert candidate["status"] == "offered" and candidate["needed"] is True
    record = store.load(context.record_dir)
    (stored,) = record.candidates.values()
    assert stored.binding.campaign_path == "sim/12/targets/sim_counter/coverage.json"
    assert stored.binding.manifest_sha256 == loaded.summary.manifest_sha256
    assert stored.proposal.justification.startswith("Reset-only branch")
    assert "strict fail · provisional pass" in candidate_summary_line(section)


def test_investigate_candidates_are_not_recorded(record_context) -> None:
    context = record_context[0]

    section = _record_candidates(*record_context, screening="investigate")

    assert section["recorded"] == 0
    assert section["verdicts"]["provisional"] == "fail"
    assert store.load(context.record_dir).is_empty


def test_rejected_point_is_filtered_from_later_proposals(record_context) -> None:
    context, loaded, campaign, path, sha = record_context
    point_id = campaign.points[0].id
    record = store.rejected_record(
        store.CandidateRecord(),
        [store.Rejection(campaign.target.identity, point_id, sha, "2026-09-30T11:00:00Z", "A.R.")],
    )
    context.record_dir.mkdir(parents=True)
    store.waiver_candidates_path(context.record_dir).write_bytes(record.to_bytes())

    section = _record_candidates(context, loaded, campaign, path, sha)

    assert (section["recorded"], section["filtered_by_rejection"]) == (0, 1)


def test_campaign_outside_the_record_flow_reports_is_refused(
    record_context, tmp_path: Path
) -> None:
    context, loaded, campaign, path, sha = record_context
    elsewhere = replace(context, flow_reports_root=tmp_path / "other")

    section = _record_candidates(elsewhere, loaded, campaign, path, sha)

    assert section["status"] == "failed"
    assert "flow-reports" in section["reason"]
    assert "not recorded" in candidate_summary_line(section)


def test_legacy_campaign_without_point_store_is_refused(record_context) -> None:
    context, _loaded, campaign, path, sha = record_context

    section = record_goal_candidates(
        context, campaign, None, path, _screened(campaign.points[0].id, sha), invocation_id="7"
    )

    assert section["status"] == "failed"
    assert "only point-store Campaigns" in section["reason"]


def test_corrupt_candidate_record_is_reported_not_raised(record_context) -> None:
    context, loaded, campaign, path, sha = record_context
    record_path = store.waiver_candidates_path(context.record_dir)
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{not json", encoding="utf-8")

    section = _record_candidates(context, loaded, campaign, path, sha)

    assert section["status"] == "failed"
    assert record_path.read_text(encoding="utf-8") == "{not json"


def test_interactive_mode_has_no_goal_context() -> None:
    from booley.flows.execution_persistence import NoAcceptanceRecorder

    assert GoalCandidateContext.from_recorder(NoAcceptanceRecorder()) is None


def _unreachable_model(monkeypatch):
    from booley.mcp import coverage_evidence

    def model(params):
        for key, value in params.nested_mcp_env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
        result = coverage_evidence.query_active_coverage_evidence({"view": "points", "limit": 1})
        return AgentResult(
            structured={
                "hypotheses": [],
                "recommendations": [],
                "waiver_candidates": [
                    {
                        "point_ref": result["points"][0]["point_ref"],
                        "reason": "unreachable",
                        "evidence": "Tied off in this configuration",
                        "proof_reference": "",
                    }
                ],
            }
        )

    return model


def test_run_records_candidates_in_goal_mode(tmp_path: Path, monkeypatch) -> None:
    path = source_project(tmp_path)
    context = GoalCandidateContext(tmp_path / "reports", tmp_path / "record")
    monkeypatch.setattr(analyst_module.GoalCandidateContext, "from_recorder", lambda *_: context)
    analyst = CoverageAnalystSpecialist(model=_unreachable_model(monkeypatch))
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])

    result = analyst._run()

    section = result.detail["waiver_candidate_record"]
    # The fixture point was hit, so D4 still routes "unreachable" to investigate.
    assert result.detail["waiver_candidates"][0]["screening"] == "investigate"
    assert section["status"] == "recorded" and section["recorded"] == 0
    assert "Waiver Candidates recorded 0" in result.report_text
    json.dumps(result.detail)  # the MCP detail stays JSON-serializable


def test_run_outside_goal_mode_records_nothing(tmp_path: Path, monkeypatch) -> None:
    path = source_project(tmp_path)
    monkeypatch.delenv("BOOLEY_CONTROL_PROJECT_ROOT", raising=False)
    analyst = CoverageAnalystSpecialist(model=_unreachable_model(monkeypatch))
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])

    result = analyst._run()

    assert "waiver_candidate_record" not in result.detail
    assert not (tmp_path / "records").exists()


def test_goal_record_directory_parameter_keeps_candidates_out_of_ticket_store(
    record_context, tmp_path
):
    context, loaded, campaign, path, sha = record_context
    goal_dir = tmp_path / "goals" / "evidence-20261007T100000Z"
    context = replace(context, record_dir=goal_dir)
    section = _record_candidates(context, loaded, campaign, path, sha, invocation_id="1")
    assert section["status"] == "recorded"
    assert (goal_dir / "waiver-candidates.json").is_file()
    assert not (context.record_dir / "waiver-candidates").exists()
    assert not store.load(goal_dir).is_empty


def test_image_alias_reports_root_records_canonical_campaign(
    record_context, tmp_path, monkeypatch
) -> None:
    from tests.conftest import require_symlinks, symlink_or_skip
    from tests.runtime.test_sandbox_layout import configure_project_alias

    context, loaded, campaign, path, sha = record_context
    require_symlinks(tmp_path)
    alias = tmp_path / "booley-project"
    symlink_or_skip(alias, tmp_path / "reports", target_is_directory=True)
    configure_project_alias(monkeypatch, alias, tmp_path / "reports")
    context = replace(context, flow_reports_root=alias)
    section = _record_candidates(context, loaded, campaign, path, sha)
    assert section["status"] == "recorded", section
    record = store.load(context.record_dir)
    assert (
        next(iter(record.candidates.values())).binding.campaign_path
        == "sim/12/targets/sim_counter/coverage.json"
    )
