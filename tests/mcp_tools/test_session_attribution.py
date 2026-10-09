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
        self.calls.append((None if facts is None else facts.key, tool, outcome))

    async def resolved(self, facts):
        return facts

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


@pytest.mark.asyncio
async def test_async_goal_reply_refreshes_shared_warning_after_deferred_peer(
    tmp_path, monkeypatch
):
    import threading

    from mcp.types import TextContent
    from starlette.requests import Request

    from booley.mcp import session_observer, session_registry
    from booley.mcp.session_observer import SessionObserver
    from booley.mcp.session_registry import SessionRegistry, resolve_attribution
    from booley.runtime.pid import ProcessIdentity

    monkeypatch.setattr(session_registry, "namespace", lambda *_: "fixture")
    monkeypatch.setattr(session_observer, "namespace", lambda *_: "fixture")
    registry = SessionRegistry(tmp_path / "data")
    other = resolve_attribution(tmp_path, metadata={"threadId": "other"}, client_name="codex")
    registry.upsert(other, "lint", "completed", now=100)
    release = threading.Event()

    def peer(_request):
        assert release.wait(2)
        return ProcessIdentity(98765, "fixture", 101)

    monkeypatch.setattr(session_observer, "observed_peer", peer)
    observer = SessionObserver(now=lambda: 100, budget=0.05)
    monkeypatch.setattr(observer, "_registry", lambda: registry)

    async def dispatch(*_):
        release.set()
        await asyncio.wait_for(observer._pending, 2)
        return McpDispatchResult(
            [TextContent(type="text", text="RUNNING: sim")], False, goal_aware=True
        )

    app = McpApplication(
        [{"name": "sim", "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    request_context = context()
    request_context.session.client_params.client_info.name = "claude-code"
    request_context.request = Request(
        {
            "type": "http",
            "server": ("127.0.0.1", 8080),
            "headers": [(b"x-booley-peer-host", b"127.0.0.1"), (b"x-booley-peer-port", b"50000")],
        }
    )
    try:
        result = await server._call_application_tool(
            app,
            CallToolRequestParams(name="sim", arguments={"work_dir": str(tmp_path)}),
            request_context,
            observer,
        )
    finally:
        release.set()
    assert any(
        "another session shares this Goal worktree" in block.text for block in result.content
    )
    assert not any("unavailable" in block.text for block in result.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted", [False, True])
async def test_running_goal_poll_is_goal_aware_and_keeps_progress_payload(
    tmp_path, monkeypatch, adopted
):
    from booley.goals.binding import GoalRunBinding
    from booley.goals.model import WorktreeIdentity

    binding = GoalRunBinding(
        tmp_path,
        "fixture-20261008T100000Z",
        1,
        WorktreeIdentity("00000000-0000-4000-8000-000000000001", "main"),
        tmp_path,
        "goal/fixture",
        "run",
        (),
        (),
        "sha256:" + "a" * 64,
        "sha256:" + "a" * 64,
        True,
    )
    record = job_records.JobRecord(
        "run", "sim", "2026-10-08T10:00:00Z", 60, binding=binding.to_json(), pid=99
    )
    monkeypatch.setattr(server, "_job_record", lambda _: record)
    monkeypatch.setattr(server, "is_pid_alive", lambda _: True)
    monkeypatch.setattr(server.jobrec.time, "time", lambda: 1791453601)
    monkeypatch.setattr(server, "_format_job_running_poll", lambda _: "RUNNING: sim")
    monkeypatch.setattr(server, "_running_progress", lambda _: {"run_id": "run"})

    async def wait(*_):
        return None if adopted else False

    async def dispatch(_name, args, *_):
        return await server._dispatch_poll(args, SimpleNamespace(wait=wait))

    app = McpApplication(
        [{"name": "booley_poll", "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    result = await server._call_application_tool(
        app,
        CallToolRequestParams(name="booley_poll", arguments={"run_id": "run", "wait_seconds": 0}),
        None,
        Observer(),
    )
    assert any(
        "another session shares this Goal worktree" in block.text for block in result.content
    )
    assert result.structured_content == {"reports": [{"run_id": "run"}]}


@pytest.mark.asyncio
async def test_deferred_peer_resolution_is_used_before_job_admission(tmp_path, monkeypatch):
    import threading

    from mcp.types import TextContent

    from booley.mcp import session_observer
    from booley.mcp.session_observer import SessionObserver
    from booley.mcp.session_registry import SessionRegistry
    from booley.runtime.pid import ProcessIdentity

    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    release = threading.Event()
    peer = ProcessIdentity(98765, "fixture", 101)

    def resolve(_):
        assert release.wait(2)
        return peer

    monkeypatch.setattr(session_observer, "observed_peer", resolve)

    class Deferred(SessionObserver):
        async def attribution(self, *args, **kwargs):
            fallback = await super().attribution(*args, **kwargs)
            release.set()
            await asyncio.wait_for(self._pending, 2)
            return fallback

    observer = Deferred(budget=0.001)
    monkeypatch.setattr(observer, "_registry", lambda: SessionRegistry(tmp_path))
    admitted = job_records.JobRecord("one", "sim", "2026-10-08T10:00:00Z", 60)

    async def submit(_name, _cmd, _timeout, _jobs, *, context):
        server._JobManager._stamp_context(admitted, context)
        return [TextContent(type="text", text="RUNNING")]

    monkeypatch.setattr(server, "_dispatch_async_job", submit)
    monkeypatch.setattr(server, "_endpoint_command", lambda *_: ["fake"])
    monkeypatch.setattr(server, "_mcp_tool_timeout_seconds", lambda *_a, **_k: 60)

    async def dispatch(name, args, source, request):
        assert request.attribution.process == peer
        return await server._dispatch_booley_mcp_tool(name, args, source, {}, None, request)

    app = McpApplication(
        [{"name": "sim", "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    ctx = context()
    ctx.session.client_params.client_info.name = "claude-code"
    try:
        result = await server._call_application_tool(
            app, CallToolRequestParams(name="sim", arguments={}), ctx, observer
        )
        assert result.content[0].text == "RUNNING"
    finally:
        release.set()
    assert admitted.session_key == "pid:fixture:98765:101"


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed", [False, True])
async def test_resolved_identity_survives_registry_publication_failure(
    tmp_path, monkeypatch, delayed
):
    import threading

    from booley.mcp import session_observer
    from booley.mcp.session_observer import SessionObserver

    fallback = Attribution("worktree:fixture", "worktree", str(tmp_path), "fixture")
    peer = Attribution("pid:fixture:7:1", "process", str(tmp_path), "fixture")
    monkeypatch.setattr(session_observer, "resolve_attribution", lambda *_a, **_k: fallback)
    release = threading.Event()
    if not delayed:
        release.set()

    def resolve(_observation):
        assert release.wait(2)
        return peer

    def publish(*_):
        raise OSError("registry unavailable")

    observer = SessionObserver(budget=0.001 if delayed else 1)
    monkeypatch.setattr(observer, "_resolve", resolve)
    monkeypatch.setattr(observer, "_publish", publish)
    try:
        facts = await observer.attribution({}, {}, "claude-code")
        if delayed:
            assert facts is fallback
            assert not release.is_set()
            # Release only after resolved() has entered its normal 0.5 s wait.
            asyncio.get_running_loop().call_soon(release.set)
            facts = await observer.resolved(facts)
        assert facts is peer
    finally:
        release.set()
        await asyncio.wait_for(observer._pending, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["record", "shared", "duplicate"])
async def test_advisory_completion_preserves_success_and_deduplicates_warning(
    monkeypatch, failure
):
    from mcp.types import TextContent

    dispatched = False
    warning = "WARNING: another session shares this Goal worktree"

    class Unreliable(Observer):
        async def record(self, *_):
            if failure == "record":
                raise KeyError("presence failure")

        async def shared(self, facts):
            if failure == "shared" and dispatched:
                raise TypeError("presence failure")
            return await super().shared(facts)

    async def dispatch(*_):
        nonlocal dispatched
        dispatched = True
        text = "retained result" + ("\n" + warning if failure == "duplicate" else "")
        return McpDispatchResult(
            ([TextContent(type="text", text=text)], {"retained": True}), False, goal_aware=True
        )

    app = McpApplication(
        [{"name": "probe", "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    result = await server._call_application_tool(
        app, CallToolRequestParams(name="probe"), None, Unreliable()
    )
    assert result.structured_content == {"retained": True}
    assert not result.is_error
    assert sum(block.text.count(warning) for block in result.content) <= 1


def test_non_linux_pid_stamping_does_not_run_blocking_ps(tmp_path, monkeypatch):
    from booley.runtime import pid

    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(pid, "capture_process_identity", lambda *_: pytest.fail("blocking ps"))
    manager = SimpleNamespace(_write_root=lambda _: tmp_path)
    record = job_records.JobRecord("run", "sim", "2026-10-08T10:00:00Z", 60)
    server._JobManager._stamp_pid(manager, record, 99)
    assert record.pid == 99 and record.process_identity is None
