"""Application/SDK boundary tests: the caller's agent across dispatch and error paths."""

from __future__ import annotations

import asyncio
import functools
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolRequestParams

from booley.mcp import agent_thread, server, session_peer
from booley.mcp.application import McpApplication, McpDispatchResult, McpInputRequired
from booley.runtime import job_records
from tests.time_budget import hang_guard_s


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
    await asyncio.gather(
        *(
            server._call_application_tool(
                application,
                CallToolRequestParams(
                    name=name, arguments={"work_dir": "/fixture"}, _meta={"threadId": thread}
                ),
                context(),
            )
            for thread in ("one", "two")
        )
    )
    assert [request.agent.thread for _arguments, request in observed] == ["one", "two"]
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
    invalid = await server._call_application_tool(app, CallToolRequestParams(name="probe"))
    assert invalid.is_error
    with pytest.raises(MCPError):
        await server._call_application_tool(app, CallToolRequestParams(name="unknown"))
    form = await server._call_application_tool(
        app, CallToolRequestParams(name="probe", arguments={"work_dir": "/fixture"})
    )
    assert form.request_state == "opaque-state"
    assert form.input_requests["answer"].params.message == "human form"


@pytest.mark.parametrize("explicit", [Path("/fixture"), None], ids=["explicit", "server-cwd"])
def test_job_record_is_stamped_with_the_submitting_call(tmp_path, monkeypatch, explicit):
    from booley.mcp.call_context import CallContext

    monkeypatch.chdir(tmp_path)
    rec = job_records.JobRecord("one", "sim", "2026-10-08T10:00:00Z", 60)
    server._JobManager._stamp_context(rec, CallContext(explicit, None, None, None, {}))
    assert rec.work_dir is not None
    assert Path(rec.work_dir).resolve() == (explicit or tmp_path).resolve()


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
    from booley.runtime.pid import capture_process_identity
    from booley.runtime.session_paths import session_jobs_dir

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
        monkeypatch.setattr(session_peer, "observed_peer", lambda _request: identity)
        await _start_job(tmp_path, monkeypatch, client_name, thread_meta)
    finally:
        if child is not None:
            child.kill()
            child.wait()
    (rec,) = job_records.list_records(session_jobs_dir())
    assert (rec.client, rec.thread) == expected
    assert rec.owner == (owner or "mcp-test")
    assert rec.work_dir is not None and Path(rec.work_dir).resolve() == tmp_path.resolve()


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
        agent = await resolve_agent_thread("claude-code", {}, identity)
    finally:
        child.kill()
        child.wait()
    assert agent is not None
    assert (agent.client, agent.thread) == ("claude", None)


async def _start_job(tmp_path, monkeypatch, client_name, thread_meta):
    """Call a background Flow through the application as *client_name*."""
    from booley.mcp import job_ownership
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
    # A transport request with no peer to route by; the thread's peer is the one named above.
    ctx.request = SimpleNamespace(headers={}, scope={})
    meta = None if thread_meta is None else {"threadId": thread_meta}
    # Codex names no workspace here, so only its call passes ``work_dir``; the others omit it
    # and run in the server cwd.
    arguments = {"work_dir": str(tmp_path)} if client_name.startswith("codex") else {}
    await server._call_application_tool(
        app, CallToolRequestParams(name="sim", arguments=arguments, _meta=meta), ctx
    )


def test_non_linux_pid_stamping_does_not_run_blocking_ps(tmp_path, monkeypatch):
    from booley.runtime import pid

    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(pid, "capture_process_identity", lambda *_: pytest.fail("blocking ps"))
    manager = SimpleNamespace(_write_root=lambda _: tmp_path)
    record = job_records.JobRecord("run", "sim", "2026-10-08T10:00:00Z", 60)
    server._JobManager._stamp_pid(manager, record, 99)
    assert record.pid == 99 and record.process_identity is None


@_NEEDS_PROC
@pytest.mark.asyncio
async def test_a_boundary_lookup_without_a_peer_is_retried(monkeypatch, tmp_path):
    """A transient peer miss at the SDK boundary must not lose the Claude thread."""
    from booley.mcp.peer_checkout import CallerCheckout
    from tests.mcp_tools.test_peer_checkout import IDENTITY, fake_proc

    proc = fake_proc(tmp_path / "proc", {50001: (IDENTITY.pid, None)})
    (proc / str(IDENTITY.pid) / "environ").write_bytes(b"T3CODE_THREAD_ID=t3-1\0")
    resolve = functools.partial(agent_thread.resolve_agent_thread, proc_root=proc)
    # The server names no /proc of its own, so the thread read is pointed at the fake one.
    monkeypatch.setattr(server, "resolve_agent_thread", resolve)
    monkeypatch.setattr(session_peer, "observed_peer", lambda _request: IDENTITY)
    ctx = context()
    ctx.session.client_params.client_info.name = "claude-code"
    ctx.request = object()
    request = await server._request_context(
        ctx, CallToolRequestParams(name="ordinary"), CallerCheckout(None, tmp_path)
    )
    assert request.agent is not None and request.agent.thread == "t3-1"


@pytest.mark.asyncio
async def test_routed_checkout_survives_a_failing_peer_lookup(monkeypatch, tmp_path):
    """Routing never depends on attribution: a failed lookup keeps the caller's checkout."""
    from booley.mcp.peer_checkout import CallerCheckout

    def broken(_request):
        raise RuntimeError("lookup exploded")

    monkeypatch.setattr(session_peer, "observed_peer", broken)
    ctx = context()
    ctx.session.client_params.client_info.name = "claude-code"
    ctx.request = object()
    request = await server._request_context(
        ctx, CallToolRequestParams(name="ordinary"), CallerCheckout(None, tmp_path)
    )
    assert request.checkout == tmp_path and request.agent is None


@pytest.mark.asyncio
async def test_a_codex_call_names_its_thread_without_a_peer_lookup(monkeypatch):
    looked_up = []
    monkeypatch.setattr(session_peer, "observed_peer", looked_up.append)
    ctx = context()
    ctx.request = object()
    params = CallToolRequestParams(name="ordinary", _meta={"threadId": "cx-1"})
    request = await server._request_context(ctx, params, None)
    assert request.agent == agent_thread.AgentThread("codex", "cx-1")
    assert looked_up == []


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", ["", "t" * 1025, "a\nb", 7])
async def test_an_invalid_thread_id_is_not_recorded(thread):
    agent = await agent_thread.resolve_agent_thread("codex", {"threadId": thread}, None)
    assert agent == agent_thread.AgentThread("codex", None)
