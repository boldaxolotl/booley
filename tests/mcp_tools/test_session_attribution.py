"""Application/SDK boundary tests: one advisory identity across dispatch and error paths."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
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
)
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


_NEEDS_PROC = pytest.mark.skipif(
    sys.platform != "linux", reason="reads the caller's environment from /proc"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_name", "thread_meta", "peer", "t3_thread", "expected", "owner"),
    [
        pytest.param(
            "claude-code",
            None,
            "child",
            "t3-1",
            ("claude", "t3-1"),
            "mcp-test|claude:t3-1",
            id="claude-t3",
            marks=_NEEDS_PROC,
        ),
        pytest.param(
            "claude-code",
            None,
            "child",
            None,
            ("claude", None),
            None,
            id="neither",
            marks=_NEEDS_PROC,
        ),
        pytest.param(
            "cursor",
            None,
            "child",
            "t3-1",
            (None, None),
            None,
            id="other-client",
            marks=_NEEDS_PROC,
        ),
        pytest.param(
            "codex-mcp-client",
            "cx-1",
            None,
            None,
            ("codex", "cx-1"),
            "mcp-test|codex:cx-1",
            id="codex",
        ),
    ],
)
async def test_job_records_the_agent_client_and_thread(
    tmp_path, monkeypatch, client_name, thread_meta, peer, t3_thread, expected, owner
):
    from booley.mcp import session_observer
    from booley.runtime.pid import capture_process_identity
    from booley.runtime.session_paths import session_jobs_dir

    proc = tmp_path / "proc"
    # A peer is a real process with, or without, the thread variable of T3 Code in its
    # environment.
    environment = {k: v for k, v in os.environ.items() if k != "T3CODE_THREAD_ID"}
    environment.update({} if t3_thread is None else {"T3CODE_THREAD_ID": t3_thread})
    child = (
        subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({hang_guard_s()})"], env=environment
        )
        if peer == "child"
        else None
    )
    try:
        identity = {
            None: None,
            "child": None if child is None else capture_process_identity(child.pid),
        }[peer]
        monkeypatch.setattr(session_observer, "observed_peer", lambda _request: identity)
        await _start_job(tmp_path, monkeypatch, proc, client_name, thread_meta)
    finally:
        if child is not None:
            child.kill()
            child.wait()
    (rec,) = job_records.list_records(session_jobs_dir())
    assert (rec.client, rec.thread) == expected
    assert rec.owner == (owner or f"mcp-test|{rec.session_key}")


@_NEEDS_PROC
@pytest.mark.asyncio
@pytest.mark.parametrize("exited", [True, False])
async def test_a_claude_caller_that_cannot_be_read_records_no_thread(exited):
    from dataclasses import replace

    from booley.mcp.agent_thread import resolve_agent_thread
    from booley.runtime.pid import capture_process_identity

    # Either the process is gone (no environment to read) or its PID now names another process.
    environment = {**os.environ, "T3CODE_THREAD_ID": "t3-1"}
    child = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({hang_guard_s()})"], env=environment
    )
    try:
        identity = capture_process_identity(child.pid)
        assert identity is not None
        if exited:
            child.kill()
            child.wait()
        else:
            identity = replace(identity, start_token=identity.start_token + 1)
        attribution = Attribution("pid:x", "process", "/w", "wk", process=identity)
        agent = await resolve_agent_thread("claude-code", {}, attribution)
    finally:
        child.kill()
        child.wait()
    assert agent is not None
    assert (agent.client, agent.thread) == ("claude", None)


async def _start_job(tmp_path, monkeypatch, proc, client_name, thread_meta):
    """Call a background Flow through the application as *client_name*, on the real observer."""
    from booley.mcp import job_ownership
    from booley.mcp.session_observer import SessionObserver
    from booley.mcp.session_registry import SessionRegistry
    from tests.mcp_tools.test_mcp_dispatch import _FakeLifetime

    for name in ("BOOLEY_RUNTIME_DIR", "BOOLEY_LOGS_DIR", "BOOLEY_SLOTS_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.chdir(tmp_path)

    async def run(*_args, **_kwargs):
        return 0, "", "", False

    # The Job's child process, its command and its timeout are not the subject; no public
    # entry point replaces them without spawning a real Flow.
    monkeypatch.setattr(server, "_run_subprocess", run)
    monkeypatch.setattr(server, "_endpoint_command", lambda *_: ["fake"])
    monkeypatch.setattr(server, "_mcp_tool_timeout_seconds", lambda *_a, **_k: 60)
    observer = SessionObserver(budget=hang_guard_s())
    # The observer reads the registry under the process's /proc; the test's own is fake.
    monkeypatch.setattr(observer, "_registry", lambda: SessionRegistry(tmp_path, proc_root=proc))
    jobs = server._JobManager(_FakeLifetime())

    async def dispatch(name, args, source, request):
        caller = job_ownership.caller_for("mcp-test", request)
        return await server._dispatch_booley_mcp_tool(
            name, args, source, {}, jobs, request, caller
        )

    app = McpApplication(
        [{"name": "sim", "schema": {"type": "object"}, "memory_admitted": True}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    ctx = context()
    ctx.session.client_params.client_info.name = client_name
    meta = None if thread_meta is None else {"threadId": thread_meta}
    try:
        await server._call_application_tool(
            app, CallToolRequestParams(name="sim", arguments={}, _meta=meta), ctx, observer
        )
    finally:
        observer.close()


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
