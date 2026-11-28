"""Application/SDK boundary tests: one advisory identity across dispatch and error paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolRequestParams

from booley.mcp import server
from booley.mcp.application import McpApplication, McpDispatchResult, McpInputRequired
from booley.mcp.session_registry import Attribution
from booley.runtime import job_records
from tests.time_budget import hang_guard_s, scaled


class Observer:
    def __init__(self):
        self.calls = []

    async def attribution(
        self, arguments, metadata, client_name, *, request=None, tool="request", caller=None
    ):
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
@pytest.mark.parametrize("name", ["ordinary", "custom", "booley_poll", "tools/list"])
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


@pytest.mark.asyncio
async def test_validation_unknown_input_required_and_stdio_outcomes(monkeypatch):

    async def dispatch(*_args):
        return McpInputRequired("opaque-state", "answer", "human form", {"type": "object"})

    app = McpApplication(
        [{"name": "probe", "schema": {"type": "object", "required": ["work_dir"]}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda value: value,
        on_discovery_error=lambda _: None,
    )
    observer = Observer()
    invalid = await server._call_application_tool(
        app, CallToolRequestParams(name="probe"), None, observer
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
        CallToolRequestParams(name="probe", arguments={"work_dir": "/fixture"}),
        None,
        observer,
    )
    assert form.request_state == "opaque-state"
    assert form.input_requests["answer"].params.message == "human form"
    assert observer.calls[-1][-1] == "input-required"


def test_job_record_is_stamped_with_the_submitting_call():
    from booley.mcp.call_context import CallContext

    rec = job_records.JobRecord("one", "sim", "2026-10-08T10:00:00Z", 60)
    context = CallContext(Path("/fixture"), None, None, None, {}, session_key="codex:submitter")
    server._JobManager._stamp_context(rec, context)
    assert rec.session_key == "codex:submitter"
    assert rec.work_dir == str(context.work_dir)


@pytest.mark.asyncio
async def test_deferred_peer_resolution_is_used_before_job_admission(tmp_path, monkeypatch):
    import threading

    from mcp.types import TextContent

    from booley.mcp import session_observer
    from booley.mcp.session_observer import SessionObserver
    from booley.mcp.session_registry import SessionRegistry
    from booley.runtime.pid import ProcessIdentity

    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    release = threading.Event()
    peer = ProcessIdentity(98765, "fixture", 101)

    def resolve(_):
        assert release.wait(hang_guard_s())
        return peer

    monkeypatch.setattr(session_observer, "observed_peer", resolve)

    class Deferred(SessionObserver):
        async def attribution(self, *args, **kwargs):
            fallback = await super().attribution(*args, **kwargs)
            release.set()
            await asyncio.wait_for(self._pending, hang_guard_s())
            return fallback

    observer = Deferred(budget=scaled(0.001))
    monkeypatch.setattr(observer, "_registry", lambda: SessionRegistry(tmp_path))
    admitted = job_records.JobRecord("one", "sim", "2026-10-08T10:00:00Z", 60)

    async def submit(_name, _cmd, _timeout, _jobs, *, context, caller=None):
        server._JobManager._stamp_context(admitted, context)
        return [TextContent(type="text", text="RUNNING")]

    monkeypatch.setattr(server, "_dispatch_async_job", submit)
    monkeypatch.setattr(server, "_endpoint_command", lambda *_: ["fake"])
    monkeypatch.setattr(server, "_mcp_tool_timeout_seconds", lambda *_a, **_k: 60)

    async def dispatch(name, args, source, request):
        assert request.attribution.process == peer
        return await server._dispatch_booley_mcp_tool(name, args, source, {}, None, request)

    app = McpApplication(
        [{"name": "sim", "schema": {"type": "object"}, "memory_admitted": True}],
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
        assert release.wait(hang_guard_s())
        return peer

    def publish(*_):
        raise OSError("registry unavailable")

    observer = SessionObserver(budget=scaled(0.001) if delayed else hang_guard_s())
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
        await asyncio.wait_for(observer._pending, hang_guard_s())


def test_non_linux_pid_stamping_does_not_run_blocking_ps(tmp_path, monkeypatch):
    from booley.runtime import pid

    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(pid, "capture_process_identity", lambda *_: pytest.fail("blocking ps"))
    manager = SimpleNamespace(_write_root=lambda _: tmp_path)
    record = job_records.JobRecord("run", "sim", "2026-10-08T10:00:00Z", 60)
    server._JobManager._stamp_pid(manager, record, 99)
    assert record.pid == 99 and record.process_identity is None


@pytest.mark.asyncio
async def test_cancelled_call_records_cancelled_outcome():
    async def dispatch(*_args):
        raise asyncio.CancelledError

    application = McpApplication(
        [{"name": "ordinary", "schema": {"type": "object"}}],
        dispatch=lambda *_: None,
        request_dispatch=dispatch,
        canonicalize=lambda value: value,
        on_discovery_error=lambda _: None,
    )
    observer = Observer()
    with pytest.raises(asyncio.CancelledError):
        await server._call_application_tool(
            application,
            CallToolRequestParams(name="ordinary", arguments={}, _meta={"threadId": "one"}),
            context(),
            observer,
        )
    assert observer.calls == [("codex:one", "ordinary", "cancelled")]


@pytest.mark.asyncio
async def test_cancel_after_the_call_returned_keeps_its_real_outcome():
    class CancelledWhileRecording(Observer):
        async def record(self, facts, tool, outcome):
            await super().record(facts, tool, outcome)
            raise asyncio.CancelledError

    async def dispatch(*_args):
        return McpDispatchResult([SimpleNamespace(text="done")], False)

    application = McpApplication(
        [{"name": "ordinary", "schema": {"type": "object"}}],
        dispatch=lambda *_: None,
        request_dispatch=dispatch,
        canonicalize=lambda value: value,
        on_discovery_error=lambda _: None,
    )
    observer = CancelledWhileRecording()
    with pytest.raises(asyncio.CancelledError):
        await server._call_application_tool(
            application,
            CallToolRequestParams(name="ordinary", arguments={}, _meta={"threadId": "one"}),
            context(),
            observer,
        )
    assert observer.calls == [("codex:one", "ordinary", "completed")]


@pytest.mark.asyncio
async def test_routed_checkout_survives_a_failing_session_observer(tmp_path):
    """Routing never depends on presentation: observer failure keeps the caller's checkout."""
    from booley.mcp.peer_checkout import CallerCheckout

    class Failing(Observer):
        async def attribution(self, *_args, **_kwargs):
            raise RuntimeError("registry exploded")

    params = CallToolRequestParams(name="ordinary", arguments={})
    request = await server._observed_request_context(
        context(), params, Failing(), CallerCheckout(None, tmp_path)
    )
    assert request.checkout == tmp_path and request.attribution is None
