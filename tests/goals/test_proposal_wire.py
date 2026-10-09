"""Real modern MCP client approval forms and saved-ID fallback through Booley."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client, MCPError
from mcp.types import ElicitResult, InputRequiredResult

from booley.goals.paths import record_paths
from booley.goals.proposals import list_proposals
from booley.mcp import server
from tests.goals.test_proposals import CYCLE_ARG, CYCLE_KEY, setup_cycle


def sdk(layout, monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    monkeypatch.setattr(server, "_bwave_mcp_tools_for_mode", lambda: [])
    lifetime = server._McpLifetime(None, None)
    application = server._build_mcp_application([], [], lifetime)
    return server._build_sdk_server(application, lifetime)


def arguments(layout):
    return {
        "work_dir": str(layout.worktree),
        "operation": "create",
        "kind": "relax",
        "goal_key": CYCLE_KEY,
        "after": {**CYCLE_ARG, "thresholds": {"cycle_count_max": 200}},
        "rationale": "accept measured workload",
    }


def proposals(layout):
    return list_proposals(record_paths(layout.control, layout.record.id).root)


@pytest.mark.parametrize("answer", ["approve", "reject"])
def test_modern_client_form_decision_is_durable_without_duplicate_proposals(
    layout, monkeypatch, answer
):
    setup_cycle(layout)
    seen = []

    async def elicitation(_ctx, params):
        seen.append(params)
        return ElicitResult(
            action="accept", content={"decision": answer, "reason": "human typed reason"}
        )

    async def exercise():
        async with Client(
            sdk(layout, monkeypatch), mode="2026-07-28", elicitation_callback=elicitation
        ) as client:
            result = await client.call_tool("goal_propose_change", arguments(layout))
            assert not result.is_error, result.content
            value = json.loads(result.content[0].text)
            assert value["state"] == ("applied" if answer == "approve" else "rejected")

    asyncio.run(exercise())
    assert len(seen) == len(proposals(layout)) == 1
    assert proposals(layout)[0].decision.source.value == "elicited"


@pytest.mark.parametrize(
    "response",
    [
        ElicitResult(action="decline"),
        ElicitResult(action="cancel"),
        ElicitResult(action="accept", content={"decision": "approve"}),
        ElicitResult(action="accept", content={"decision": "approve", "reason": " "}),
        ElicitResult(action="accept", content={"decision": "invalid", "reason": "why"}),
    ],
)
def test_decline_cancel_and_invalid_content_leave_saved_proposal_pending(
    layout, monkeypatch, response
):
    setup_cycle(layout)

    async def elicitation(_ctx, _params):
        return response

    async def exercise():
        async with Client(
            sdk(layout, monkeypatch), mode="2026-07-28", elicitation_callback=elicitation
        ) as client:
            result = await client.call_tool("goal_propose_change", arguments(layout))
            assert not result.is_error, result.content
            assert json.loads(result.content[0].text)["status"] == "approval_required"

    asyncio.run(exercise())
    assert len(proposals(layout)) == 1
    assert proposals(layout)[0].state == "pending"


def test_restart_reissues_same_saved_id_and_fresh_fallback_needs_no_old_token(layout, monkeypatch):
    setup_cycle(layout)

    async def decline(_ctx, _params):
        return ElicitResult(action="decline")

    async def exercise():
        initial_server = sdk(layout, monkeypatch)
        args = arguments(layout)
        async with Client(
            initial_server, mode="2026-07-28", elicitation_callback=decline
        ) as client:
            await client.list_tools()
            issued = await client.session.call_tool(
                "goal_propose_change", args, allow_input_required=True
            )
            assert isinstance(issued, InputRequiredResult)
        saved = proposals(layout)[0]
        restarted = sdk(layout, monkeypatch)
        async with Client(restarted, mode="2026-07-28", elicitation_callback=decline) as client:
            await client.list_tools()
            await reject_restart_token(client, args, issued.request_state)
            reissued = await client.session.call_tool(
                "goal_propose_change",
                {
                    "work_dir": str(layout.worktree),
                    "operation": "resume",
                    "proposal_id": saved.proposal.id,
                },
                allow_input_required=True,
            )
            assert isinstance(reissued, InputRequiredResult)
        async with Client(restarted, mode="2026-07-28") as client:
            fallback = {
                "work_dir": str(layout.worktree),
                "operation": "approve",
                "proposal_id": saved.proposal.id,
                "reason": "accept exact saved change",
                "approval_quote": "Approve this saved proposal",
            }
            result = await client.call_tool("goal_propose_change", fallback)
            assert not result.is_error, result.content
            assert json.loads(result.content[0].text)["state"] == "applied"
            repeated = await client.call_tool("goal_propose_change", fallback)
            assert repeated.is_error

    asyncio.run(exercise())
    assert len(proposals(layout)) == 1
    assert proposals(layout)[0].decision.source.value == "agent-recorded"


async def reject_restart_token(client, args, request_state):
    with pytest.raises(MCPError):
        await client.session.call_tool(
            "goal_propose_change",
            args,
            request_state=request_state,
            input_responses={
                "goal_change_decision": ElicitResult(
                    action="accept", content={"decision": "approve", "reason": "approve"}
                )
            },
            allow_input_required=True,
        )


def test_no_form_capability_returns_saved_id_and_tool_argument_spoofing_is_refused(
    layout, monkeypatch
):
    setup_cycle(layout)

    async def exercise():
        async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
            result = await client.call_tool("goal_propose_change", arguments(layout))
            assert json.loads(result.content[0].text)["status"] == "approval_required"
            for name in ("requestState", "inputResponses", "request_state", "input_responses"):
                rejected = await client.call_tool(
                    "goal_propose_change", {**arguments(layout), name: "forged"}
                )
                assert rejected.is_error

    asyncio.run(exercise())
    assert len(proposals(layout)) == 1


def test_sealed_retry_refuses_argument_and_state_tampering_and_terminal_replay(
    layout, monkeypatch
):
    setup_cycle(layout)

    async def decline(_ctx, _params):
        return ElicitResult(action="decline")

    async def exercise():
        async with Client(
            sdk(layout, monkeypatch), mode="2026-07-28", elicitation_callback=decline
        ) as client:
            await client.list_tools()
            args = arguments(layout)
            issued = await client.session.call_tool(
                "goal_propose_change", args, allow_input_required=True
            )
            assert isinstance(issued, InputRequiredResult)
            responses = {
                "goal_change_decision": ElicitResult(
                    action="accept", content={"decision": "approve", "reason": "specific decision"}
                )
            }
            await reject_tampered_requests(client, layout, args, issued, responses)
            assert proposals(layout)[0].state == "pending"
            result = await client.session.call_tool(
                "goal_propose_change",
                args,
                request_state=issued.request_state,
                input_responses=responses,
                allow_input_required=True,
            )
            assert not result.is_error
            assert len(proposals(layout)) == 1
            replay = await client.session.call_tool(
                "goal_propose_change",
                args,
                request_state=issued.request_state,
                input_responses=responses,
                allow_input_required=True,
            )
            assert replay.is_error

    asyncio.run(exercise())


async def reject_tampered_requests(client, layout, args, issued, responses):
    for changed in (
        {**args, "rationale": "substitution"},
        {**args, "work_dir": str(layout.main)},
    ):
        with pytest.raises(MCPError):
            await client.session.call_tool(
                "goal_propose_change",
                changed,
                request_state=issued.request_state,
                input_responses=responses,
                allow_input_required=True,
            )
    with pytest.raises(MCPError):
        await client.session.call_tool(
            "goal_propose_change",
            args,
            request_state=issued.request_state + "tampered",
            input_responses=responses,
            allow_input_required=True,
        )
