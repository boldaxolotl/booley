"""Actual Reviewer production receipts, exact derivations and modern completion transport."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import Client

from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleRequest
from booley.review.goal_package import GoalCompletionPackage
from tests.goals.conftest import enter_goals
from tests.goals.test_finish import environment
from tests.goals.test_mcp_routing import _goal_reviewer_endpoint
from tests.goals.test_proposal_wire import sdk
from tests.goals.test_proposals import observations
from tests.goals.test_reviewer_proposals import approve_review_relaxation, review_issues

# These multi-step Git/Reviewer cases retain the established Goal lifecycle budget.
# The measured Windows Reviewer baseline is 27.768s (windows-test-timings.json);
# its 3x/30s-rounded minimum is 90s, above CI's unannotated 60s default.
pytestmark = pytest.mark.timeout(120)


@pytest.mark.parametrize("derived", [False, True])
def test_open_done_findings_finish_without_ticket_assessment_or_second_approval(
    layout, monkeypatch, capsys, derived
):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    layout.record = enter_goals(
        layout,
        [{"family": "review", "review": "rtl_bugs", "verdict": "clean" if derived else "done"}],
    )
    endpoint, provider = _goal_reviewer_endpoint(layout, monkeypatch)
    provider.return_value.output = json.dumps({"issues": review_issues(True)})
    assert endpoint._run().exit_code == int(derived)
    producer = observations(layout)[0]
    if derived:
        asyncio.run(approve_review_relaxation(layout, monkeypatch))
    selected = observations(layout)[-1]
    replay, replay_provider = _goal_reviewer_endpoint(layout, monkeypatch)
    replay_result = replay._run()
    assert replay_result.exit_code == 0 and replay_provider.call_count == 0
    display = capsys.readouterr().out + str(replay_result)
    assert "explicit human approval before acceptance" not in display
    call = LifecycleRequest(
        layout.worktree,
        layout.record.id,
        str(uuid4()),
        summary="Completed review; the finding remains visible.",
        explain_html=True,
    )
    result = finish_goal(call, environment(layout))
    assert result["status"] == "finished"
    facts = json.loads(Path(result["package"]).read_bytes())
    assert facts["goals"][0]["selected_observation"] == selected
    assert "fixture finding" in result["message"]
    assert "fixture finding" in Path(result["html"]).read_text()
    assert "SemanticAssessment" not in Path(result["package"]).read_text()
    assert "approval before acceptance" not in result["message"]
    assert GoalCompletionPackage.from_json(facts).to_json() == facts
    if derived:
        assert facts["goals"][0]["original_observations"] == [producer]
        assert facts["proposal_decisions"][0]["decision"]["quote"]
        assert facts["proposal_decisions"][0]["transaction_digest"]


def test_modern_mcp_finish_requires_stable_binding_and_preserves_response_retry(
    layout, monkeypatch
):
    from tests.goals.test_status import publish

    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    operation = str(uuid4())
    arguments = {
        "work_dir": str(layout.worktree),
        "record_id": layout.record.id,
        "operation_id": operation,
        "summary": "Modern transport completed the Goal.",
    }

    async def run():
        async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
            first = await client.call_tool("goal_finish", arguments)
            assert not first.is_error, first.content
            retry = await client.call_tool("goal_finish", arguments)
            assert not retry.is_error
            assert retry.content == first.content
            changed = await client.call_tool("goal_finish", {**arguments, "summary": "Changed"})
            assert changed.is_error and "immutable payload" in changed.content[0].text

    asyncio.run(run())


def test_derived_sibling_and_rejected_agent_decision_are_frozen_separately(layout):
    from tests.goals.test_proposals import CYCLE_KEY, approve, proposal, publish, setup_cycle

    env = setup_cycle(layout)
    publish(layout)
    approved = proposal(layout, env)
    approve(layout, env, approved.proposal.id)
    rejected = proposal(layout, env, maximum=300)
    approve(layout, env, rejected.proposal.id, answer="reject")
    selected = observations(layout)
    result = finish_goal(
        LifecycleRequest(
            layout.worktree,
            layout.record.id,
            str(uuid4()),
            summary="Measured cycle bound accepted; rejected further relaxation.",
            explain_html=True,
        ),
        environment(layout),
    )
    facts = json.loads(Path(result["package"]).read_bytes())
    cycle = next(row for row in facts["goals"] if row["key"] == CYCLE_KEY)
    assert cycle["selected_observation"] == next(
        row for row in reversed(selected) if row["criterion"] == CYCLE_KEY
    )
    assert cycle["original_observations"][0] == selected[0]
    assert (
        len(facts["change_log"])
        == len(facts["applied_proposal_decisions"])
        == len(facts["rejected_proposal_decisions"])
        == 1
    )
    assert len(facts["agent_recorded_decisions"]) == 2
    assert rejected.proposal.payload_digest in result["message"]
    assert rejected.proposal.payload_digest in Path(result["html"]).read_text()


def test_modern_abandon_quote_binding_and_saved_retry_after_replacement(layout, monkeypatch):
    from booley.goals.paths import record_paths

    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    arguments = {
        "work_dir": str(layout.worktree),
        "record_id": layout.record.id,
        "operation_id": str(uuid4()),
        "abandon": True,
        "instruction_quote": "Stop with work retained",
    }

    async def run():
        async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
            first = await client.call_tool("goal_finish", arguments)
            assert not first.is_error
            changed = await client.call_tool(
                "goal_finish", {**arguments, "instruction_quote": "Different"}
            )
            assert changed.is_error
            from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
            from booley.goals.model import parse_goal_args

            record = enter_goal_mode(
                EntryRequest(
                    layout.worktree,
                    "replacement",
                    parse_goal_args([{"family": "lint", "target": "top"}]),
                ),
                EntryEnvironment(layout.control),
            ).record
            path = record_paths(layout.control, record.id).record_file
            before = path.read_bytes()
            retry = await client.call_tool("goal_finish", arguments)
            assert retry.content == first.content and not retry.is_error
            assert path.read_bytes() == before

    asyncio.run(run())


# Native Windows passed call: 40.944s; 3x rounded to 30s requires 150s.
@pytest.mark.timeout(150)
def test_attempt_frozen_survives_real_status_and_reviewer_activity_without_new_evidence(
    layout, monkeypatch
):
    from booley.goals.paths import record_paths
    from tests.goals.test_finish import Crash

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    layout.record = enter_goals(
        layout, [{"family": "review", "review": "rtl_bugs", "verdict": "done"}]
    )
    endpoint, provider = _goal_reviewer_endpoint(layout, monkeypatch)
    provider.return_value.output = json.dumps({"issues": review_issues(True)})
    assert endpoint._run().exit_code == 0
    call = LifecycleRequest(
        layout.worktree, layout.record.id, str(uuid4()), summary="Finish the recorded review."
    )

    def stop(name):
        if name == "attempt-frozen":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(layout, stop))
    path = record_paths(layout.control, layout.record.id).state_file
    before = json.loads(path.read_bytes())
    exact = observations(layout)
    monkeypatch.setattr("booley.goals.state_store.utc_now_rfc3339", lambda: "2030-01-02T03:04:05Z")

    async def status():
        async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
            result = await client.call_tool("goal_status", {"work_dir": str(layout.worktree)})
            assert not result.is_error

    asyncio.run(status())
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    replay, replay_provider = _goal_reviewer_endpoint(layout, monkeypatch)
    activity = replay.execute_prepared()
    assert activity.exit_code == 0, activity.outcome.report_text
    assert replay_provider.call_count == 0
    after = json.loads(path.read_bytes())
    assert before["criteria"] == after["criteria"] and observations(layout) == exact
    assert (
        before["timeline"] != after["timeline"] or before["last_updated"] != after["last_updated"]
    )
    result = finish_goal(call, environment(layout))
    assert result["status"] == "finished"
    assert (
        json.loads(Path(result["package"]).read_bytes())["goals"][0]["selected_observation"]
        == exact[0]
    )
