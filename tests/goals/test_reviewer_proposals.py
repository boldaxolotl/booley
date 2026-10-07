"""Review relaxation consumes actual Specialist receipts without another run."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from booley.goals.status import build_status
from booley.goals.store import GoalStore
from tests.goals.conftest import enter_goals
from tests.goals.test_mcp_routing import _goal_reviewer_endpoint
from tests.goals.test_proposal_wire import sdk
from tests.goals.test_proposals import observations


async def approve_review_relaxation(layout, monkeypatch):
    async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
        response = await client.call_tool(
            "goal_propose_change",
            {
                "work_dir": str(layout.worktree),
                "operation": "create",
                "kind": "relax",
                "goal_key": "review_rtl_bugs_clean",
                "after": {"family": "review", "review": "rtl_bugs", "verdict": "done"},
                "rationale": "accept the completed review",
            },
        )
        assert not response.is_error, response.content
        saved = json.loads(response.content[0].text)
        response = await client.call_tool(
            "goal_propose_change",
            {
                "work_dir": str(layout.worktree),
                "operation": "approve",
                "proposal_id": saved["proposal_id"],
                "reason": "the original review is sufficient",
                "approval_quote": "Approve changing this fixture review to done",
            },
        )
        assert not response.is_error, response.content
        assert json.loads(response.content[0].text)["state"] == "applied"


def review_issues(has_finding):
    return (
        [
            {
                "severity": "CRITICAL",
                "confidence": "HIGH",
                "category": "bugs",
                "kind": "code_defect",
                "disposition": "current",
                "ticket_clause": "fixture behavior",
                "file": "rtl.v",
                "line": 1,
                "summary": "fixture finding",
            }
        ]
        if has_finding
        else []
    )


@pytest.mark.parametrize("has_finding", [False, True])
def test_modern_review_relaxation_preserves_actual_specialist_receipt(
    layout, monkeypatch, has_finding
):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    layout.record = enter_goals(
        layout, [{"family": "review", "review": "rtl_bugs", "verdict": "clean"}]
    )
    endpoint, provider = _goal_reviewer_endpoint(layout, monkeypatch)
    issues = review_issues(has_finding)
    provider.return_value.output = json.dumps({"issues": issues})
    assert endpoint._run().exit_code == int(has_finding)
    original = observations(layout)[0]
    assert original["met"] is (not has_finding)
    asyncio.run(approve_review_relaxation(layout, monkeypatch))
    store = GoalStore(layout.control)
    assert build_status(store, store.load(layout.record.id)).goals[0].status == "met"
    rows = observations(layout)
    assert len(rows) == 2 and rows[0] == original
    derived = rows[-1]
    assert derived["criterion"] == "review_rtl_bugs_done" and derived["met"] is True
    assert derived["recorded_at"] == original["recorded_at"]
    assert derived["detail"]["receipt_id"] == original["detail"]["receipt_id"]
    assert derived["detail"]["issue_list"] == original["detail"]["pending"]
    assert derived["detail"]["_source_fingerprint"] == original["detail"]["_source_fingerprint"]
    source = derived["detail"]["goal_derivation"]["source_evidence"][0]
    assert source["sequence"] == original["sequence"]
    assert source["criterion"] == original["criterion"]
    assert provider.call_count == 1
    replay, replay_provider = _goal_reviewer_endpoint(layout, monkeypatch)
    assert replay._run().exit_code == 0
    assert replay_provider.call_count == 0
    assert observations(layout) == rows
    (layout.worktree / "rtl.v").write_text("module top; wire changed; endmodule\n")
    changed, changed_provider = _goal_reviewer_endpoint(layout, monkeypatch)
    assert changed._run().exit_code == 0
    assert changed_provider.call_count == 1
