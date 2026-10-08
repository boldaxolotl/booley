"""Application/SDK boundary tests: one advisory identity across dispatch and error paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolRequestParams

from booley.mcp import server
from booley.mcp.application import (
    McpApplication,
    McpDispatchResult,
    McpInputRequired,
    McpRequestContext,
)
from booley.mcp.session_registry import Attribution
from booley.runtime import job_records


class Observer:
    def __init__(self):
        self.calls = []

    async def attribution(self, arguments, metadata, client_name, *, request=None, tool="request"):
        return Attribution(
            "codex:" + metadata.get("threadId", "unknown"),
            "thread",
            str(arguments.get("work_dir", "/fixture")),
            "fixture",
        )

    async def record(self, facts, tool, outcome):
        self.calls.append((facts.key, tool, outcome))

    async def shared(self, _facts):
        return ("codex:one", "codex:two"), "WARNING: another session shares this Goal worktree"


def context():
    return SimpleNamespace(
        session=SimpleNamespace(
            client_capabilities=None,
            client_params=SimpleNamespace(client_info=SimpleNamespace(name="codex-mcp-client")),
        ),
        protocol_version="2026-07-28",
        request=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["ordinary", "custom", "booley_poll", "goal_status", "goal_finish", "tools/list"]
)
async def test_all_application_dispatch_paths_get_same_explicit_identity(monkeypatch, name):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    observed = []

    async def dispatch(_name, arguments, _source, request):
        observed.append((arguments, request))
        return McpDispatchResult([SimpleNamespace(text="payload")], False)

    application = McpApplication(
        [{"name": name, "schema": {"type": "object"}}],
        dispatch=lambda *_: None,
        request_dispatch=dispatch,
        canonicalize=lambda value: value,
        on_discovery_error=lambda _: None,
    )
    observer = Observer()
    await asyncio.gather(
        *(
            server._call_application_tool(
                application,
                CallToolRequestParams(
                    name=name, arguments={"work_dir": "/fixture"}, _meta={"threadId": thread}
                ),
                context(),
                observer,
            )
            for thread in ("one", "two")
        )
    )
    assert [request.attribution.key for _arguments, request in observed] == [
        "codex:one",
        "codex:two",
    ]
    assert all(arguments == {"work_dir": "/fixture"} for arguments, _request in observed)
    assert all(request.other_session_keys == ("codex:one", "codex:two") for _, request in observed)


@pytest.mark.asyncio
async def test_validation_unknown_input_required_and_stdio_outcomes(monkeypatch):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")

    async def dispatch(*_args):
        return McpInputRequired(
            "opaque-state", "answer", "human form", {"type": "object"}, goal_aware=True
        )

    app = McpApplication(
        [{"name": "goal_propose_change", "schema": {"type": "object", "required": ["work_dir"]}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda value: value,
        on_discovery_error=lambda _: None,
    )
    observer = Observer()
    invalid = await server._call_application_tool(
        app, CallToolRequestParams(name="goal_propose_change"), None, observer
    )
    assert invalid.is_error
    assert observer.calls[-1][-1] == "error"
    with pytest.raises(MCPError):
        await server._call_application_tool(
            app, CallToolRequestParams(name="unknown"), None, observer
        )
    assert observer.calls[-1][-1] == "unknown-tool"
    form = await server._call_application_tool(
        app,
        CallToolRequestParams(name="goal_propose_change", arguments={"work_dir": "/fixture"}),
        None,
        observer,
    )
    assert form.request_state == "opaque-state"
    assert "WARNING:" in form.input_requests["answer"].params.message
    assert observer.calls[-1][-1] == "input-required"


def test_job_submitter_is_immutable_when_current_caller_changes():
    from booley.mcp.call_context import CallContext

    rec = job_records.JobRecord("one", "sim", "2026-10-08T10:00:00Z", 60)
    context = CallContext(
        Path("/fixture"), None, None, None, None, {}, None, session_key="codex:submitter"
    )
    server._JobManager._stamp_context(rec, context)
    caller = McpRequestContext(
        attribution=Attribution("codex:poller", "thread", "/other", "other")
    )
    assert caller.attribution.key == "codex:poller"
    assert rec.session_key == "codex:submitter"
    assert rec.work_dir == str(context.work_dir)


def test_observed_process_audit_roundtrip_is_advisory_and_validated():
    from booley.goals.changes import Approval
    from booley.goals.proposals import Decision, ProposalError
    from booley.runtime.pid import ProcessIdentity

    peer = ProcessIdentity(99, "fixture", 101)
    decision = Decision(
        decision="approve",
        reason="human approved",
        source=Approval.ELICITED,
        quote=None,
        at="2026-10-08T10:00:00Z",
        payload_digest="sha256:" + "a" * 64,
        session_key="codex:one",
        peer_process=peer,
    )
    assert Decision.from_json(decision.to_json()) == decision
    malformed = {**decision.to_json(), "peer_process": "pid:99"}
    with pytest.raises(ProposalError, match="advisory"):
        Decision.from_json(malformed)


def test_invalid_advisory_unicode_cannot_gate_lifecycle(tmp_path):
    from booley.goals.lifecycle import _audit_attribution

    operation = SimpleNamespace(
        request=SimpleNamespace(session_key="bad\ud800"), directory=tmp_path
    )
    _audit_attribution(operation)
    assert list(tmp_path.iterdir()) == []


def test_entry_warning_excludes_its_own_observation(tmp_path):
    from booley.goals.model import WorktreeIdentity
    from booley.mcp.goal_tools import _entry_environment

    context = McpRequestContext(
        attribution=Attribution("codex:one", "thread", "/fixture", "wt"),
        other_session_keys=("codex:one", "codex:two"),
    )
    identity = WorktreeIdentity("00000000-0000-4000-8000-000000000001", "main")
    assert _entry_environment(tmp_path, context).other_sessions(identity) == ("codex:two",)
    alone = McpRequestContext(attribution=context.attribution, other_session_keys=("codex:one",))
    assert _entry_environment(tmp_path, alone).other_sessions(identity) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,text", [("sim", "Interactive result"), ("goal_status", "No active Goal Mode.")]
)
async def test_interactive_and_inactive_goal_replies_do_not_append_shared_warnings(
    monkeypatch, name, text
):
    from mcp.types import TextContent

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")

    async def dispatch(*_):
        return McpDispatchResult([TextContent(type="text", text=text)], False)

    app = McpApplication(
        [{"name": name, "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    result = await server._call_application_tool(
        app, CallToolRequestParams(name=name), None, Observer()
    )
    assert [block.text for block in result.content] == [text]


@pytest.mark.asyncio
async def test_goal_aware_shared_warning_keeps_structured_content(monkeypatch):
    from mcp.types import TextContent

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")

    async def dispatch(*_):
        return McpDispatchResult(
            ([TextContent(type="text", text="Goal result")], {"facts": "retained"}),
            False,
            goal_aware=True,
        )

    app = McpApplication(
        [{"name": "probe", "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    result = await server._call_application_tool(
        app, CallToolRequestParams(name="probe"), None, Observer()
    )
    assert result.content[0].text == "Goal result"
    assert result.content[1].text.startswith("WARNING:")
    assert result.structured_content == {"facts": "retained"}
