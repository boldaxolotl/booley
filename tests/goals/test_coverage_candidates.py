"""Specialist candidates follow the binding's record and Campaign Target identity."""

import json
import shutil

import pytest

from booley.flows.sim import waiver_candidates as store
from booley.flows.sim.coverage_analysis_input import read_coverage_campaign
from booley.goals.binding import GoalBindingError
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.paths import record_paths
from booley.mcp.call_context import binding_from_environment
from booley.specialists.coverage_candidate_recording import (
    GoalCandidateContext,
    record_goal_candidates,
)
from tests.goals.conftest import bind
from tests.mcp_tools.test_coverage_analyst import source_project
from tests.mcp_tools.test_coverage_candidate_recording import _NOW, _screened, _strict_failure


def environment(goal_mode):
    binding = bind(goal_mode)
    paths = record_paths(binding.booley_dir, binding.record_id)
    return {
        "BOOLEY_GOAL_FILE": str(paths.record_file),
        "BOOLEY_GOAL_RUN_BINDING": json.dumps(binding.to_json()),
        "BOOLEY_CONTROL_PROJECT_ROOT": str(goal_mode.main),
        "BOOLEY_RUNTIME_DIR": str(goal_mode.main / "wrong-runtime"),
    }, paths


def test_goal_context_ignores_ticket_slug_and_environment_roots(goal_mode):
    _env, paths = environment(goal_mode)
    context = GoalCandidateContext.from_recorder(GoalFlowExecution(bind(goal_mode)))
    assert context.record_dir == paths.root
    assert context.flow_reports_root == paths.runtime_dir / "flow-reports"


def test_goal_candidate_record_retains_campaign_target_under_bound_record(goal_mode, tmp_path):
    _env, paths = environment(goal_mode)
    context = GoalCandidateContext.from_recorder(GoalFlowExecution(bind(goal_mode)))
    fixture = tmp_path / "campaign-fixture"
    original = source_project(fixture)
    shutil.copytree(fixture / "reports", context.flow_reports_root, dirs_exist_ok=True)
    path = context.flow_reports_root / original.relative_to(fixture / "reports")
    loaded = read_coverage_campaign(path)
    campaign = _strict_failure(loaded.campaign)
    sha = str(campaign.source_closure["rtl"][0]["sha256"])
    result = record_goal_candidates(
        context,
        campaign,
        loaded.summary,
        path,
        _screened(campaign.points[0].id, sha),
        invocation_id="analyst",
        now=_NOW,
    )
    assert result["status"] == "recorded"
    record = store.load(context.record_dir)
    (candidate,) = record.candidates.values()
    assert candidate.binding.target_identity == campaign.target.identity
    assert candidate.binding.target_selector == campaign.target.selector
    assert (
        candidate.binding.campaign_path == path.relative_to(context.flow_reports_root).as_posix()
    )
    assert candidate.binding.manifest_sha256 == loaded.summary.manifest_sha256
    assert (paths.root / "waiver-candidates.json").exists()
    assert not (goal_mode.control / "tickets" / "waiver-candidates").exists()
    assert not (goal_mode.main / "wrong-runtime").exists()
    refusal = record_goal_candidates(
        context,
        campaign,
        loaded.summary,
        original,
        _screened(campaign.points[0].id, sha),
        invocation_id="outside",
        now=_NOW,
    )
    assert refusal["status"] == "failed"
    assert "flow-reports" in refusal["reason"]
    assert len(store.load(context.record_dir).candidates) == 1


@pytest.mark.parametrize("raw", ["{not-json", "[]", "{}"])
def test_goal_candidate_context_requires_a_valid_admission_binding(goal_mode, raw, monkeypatch):
    env, _ = environment(goal_mode)
    if raw is None:
        env.pop("BOOLEY_GOAL_RUN_BINDING")
    else:
        env["BOOLEY_GOAL_RUN_BINDING"] = raw
    with pytest.raises((ValueError, GoalBindingError), match=r"binding|JSON|Expecting"):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        if raw is None:
            monkeypatch.delenv("BOOLEY_GOAL_RUN_BINDING", raising=False)
        binding_from_environment()
