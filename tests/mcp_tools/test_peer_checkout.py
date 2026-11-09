"""A call without ``work_dir`` runs in the checkout its calling agent works in (#2606)."""

from __future__ import annotations

import contextlib
import functools
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent

from booley.harness.dashboard import model
from booley.harness.dashboard.app import DashboardApp
from booley.mcp import server, session_observer, session_peer, session_registry
from booley.mcp.application import McpApplication, McpDispatchResult, McpRequestContext
from booley.mcp.session_observer import SessionObserver
from booley.mcp.session_registry import SessionRegistry
from booley.runtime import chat_sessions
from booley.runtime.pid import ProcessIdentity
from tests.conftest import symlink_or_skip
from tests.goals.conftest import git
from tests.mcp_tools.test_session_registry import SCOPE, _fake_process
from tests.runtime.test_chat_sessions import process

THREAD_ID = "019a0000-0000-7000-8000-000000000000"
SERVER_PORT = 4321  # Matches the 10E1 endpoint written by ``fake_proc``.


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Two repositories, each with a linked worktree; the server runs in ``main``."""
    paths: dict[str, Path] = {}
    for name in ("main", "other"):
        root = tmp_path / name
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        git(root, "commit", "-q", "--allow-empty", "-m", "base")
        linked = tmp_path / f"{name}-wt"
        git(root, "worktree", "add", "-q", "-b", f"{name}-work", str(linked))
        paths[name], paths[f"{name}_wt"] = root.resolve(), linked.resolve()
    (paths["main_wt"] / "sub").mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(paths["main"])
    return SimpleNamespace(
        **paths, sub=paths["main_wt"] / "sub", elsewhere=(tmp_path / "elsewhere").resolve()
    )


def link_cwd(proc: Path, pid: int, cwd: Path | str | None) -> ProcessIdentity:
    """A fake ``/proc/<pid>`` whose ``cwd`` link names *cwd* (no link when ``None``)."""
    (proc / str(pid)).mkdir(parents=True, exist_ok=True)
    if cwd is not None:
        symlink_or_skip(proc / str(pid) / "cwd", Path(cwd))
    return ProcessIdentity(pid, SCOPE, 7)


def fake_proc(root: Path, peers: dict[int, tuple[int, Path | None]]) -> Path:
    """A ``/proc`` where each client port is a loopback socket held by one process.

    *peers* maps a client port to ``(pid, cwd)``.
    """
    (root / "net").mkdir(parents=True)
    lines = ["header"]
    for number, (port, (pid, cwd)) in enumerate(peers.items()):
        inode = str(1000 + number)
        lines.append(
            f"{number}: 0100007F:{port:04X} 0100007F:{SERVER_PORT:04X} 01 0 0 0 0 0 {inode}"
        )
        _fake_process(root, pid, inode)
        link_cwd(root, pid, cwd)
    (root / "net/tcp").write_text("\n".join(lines) + "\n")
    return root


# --- Which checkout: the routing rule -------------------------------------------------


def test_non_routable_caller_cwd_keeps_the_server_default(
    tmp_path: Path, repos: SimpleNamespace
) -> None:
    from booley.mcp import peer_checkout

    cwds = {
        "linked-worktree": repos.main_wt,
        "main-checkout": repos.main,
        "worktree-subdirectory": repos.sub,
        "unrelated-dir": repos.elsewhere,
        "other-repo-worktree": repos.other_wt,
        "unreadable-cwd": None,
        "deleted-cwd": f"{repos.main_wt} (deleted)",
    }
    proc = tmp_path / "proc"
    routed = {
        case: peer_checkout.process_checkout(
            link_cwd(proc, 100 + number, cwd), server_cwd=repos.main, proc_root=proc
        )
        for number, (case, cwd) in enumerate(cwds.items())
    }
    # Only a linked worktree's own root routes; every other case keeps the server cwd.
    assert routed == dict.fromkeys(cwds) | {"linked-worktree": repos.main_wt}


def test_server_inside_a_linked_worktree_routes_only_to_other_worktrees(
    tmp_path: Path, repos: SimpleNamespace
) -> None:
    from booley.mcp import peer_checkout

    sibling = tmp_path / "sibling"
    git(repos.main, "worktree", "add", "-q", "-b", "sibling-work", str(sibling))
    proc = tmp_path / "proc"
    same, other = link_cwd(proc, 100, repos.main_wt), link_cwd(proc, 101, sibling.resolve())
    kwargs = {"server_cwd": repos.main_wt, "proc_root": proc}
    assert peer_checkout.process_checkout(same, **kwargs) is None
    assert peer_checkout.process_checkout(other, **kwargs) == sibling.resolve()


def test_server_outside_git_never_routes(tmp_path: Path, repos: SimpleNamespace) -> None:
    from booley.mcp import peer_checkout

    proc = tmp_path / "proc"
    caller = link_cwd(proc, 100, repos.main_wt)
    assert (
        peer_checkout.process_checkout(caller, server_cwd=repos.elsewhere, proc_root=proc) is None
    )


def test_claude_code_process_routes_by_its_own_cwd(tmp_path: Path, repos: SimpleNamespace) -> None:
    from booley.mcp import peer_checkout

    proc = fake_proc(tmp_path / "proc", {50001: (200, repos.main_wt)})
    request = http_request(50001)
    routed = peer_checkout.caller_checkout(
        request, client_name="claude-code", metadata={}, proc_root=proc
    )
    assert routed.checkout == repos.main_wt
    assert routed.peer is not None and routed.peer.pid == 200
    peer = routed.peer
    kwargs = {"metadata": {}, "proc_root": proc}
    assert peer_checkout.session_process(peer, client_name="claude-code", **kwargs) == peer
    # No printable client name: the peer is not known to be a session process.
    assert peer_checkout.session_process(peer, client_name="", **kwargs) is None


def http_request(port: int) -> Any:
    """A transport-stamped request as ``PeerBoundary`` produces it."""
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "server": ("127.0.0.1", SERVER_PORT),
            "headers": [
                (b"x-booley-peer-host", b"127.0.0.1"),
                (b"x-booley-peer-port", str(port).encode()),
            ],
        }
    )


def test_booley_chat_codex_launch_routes_by_owner_cwd_and_unmarked_codex_never_routes(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.mcp import peer_checkout

    booley = tmp_path / "booley"
    booley.mkdir()
    monkeypatch.setattr(peer_checkout, "resolve_common_booley_dir", lambda: booley)
    proc = tmp_path / "proc"
    owner = process(proc, 100, 1, executable="node")
    peer = process(proc, 101, 100)
    link_cwd(proc, 100, repos.main_wt)  # The tab's launch directory.
    link_cwd(proc, 101, repos.elsewhere)  # An app-server helper with its own cwd.
    codex = {"client_name": "codex-mcp-client", "metadata": {"threadId": THREAD_ID}}

    # Ordinary Codex: no booley chat mark, so neither the helper nor the ancestor routes.
    assert peer_checkout.session_process(peer, proc_root=proc, **codex) is None
    chat_sessions.reserve_codex_chat(booley, owner)
    marked = peer_checkout.session_process(peer, proc_root=proc, **codex)
    assert marked == owner
    assert marked is not None
    routed = peer_checkout.process_checkout(marked, server_cwd=repos.main, proc_root=proc)
    assert routed == repos.main_wt
    # A Codex call without a valid thread id has no tab identity either.
    no_thread = {"client_name": "codex-mcp-client", "metadata": {"threadId": ""}}
    assert peer_checkout.session_process(peer, proc_root=proc, **no_thread) is None


def test_caller_checkout_falls_back_to_the_server_cwd_on_unreadable_state(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.mcp import peer_checkout

    proc = fake_proc(tmp_path / "proc", {50001: (200, repos.main_wt)})
    request = http_request(50001)
    none = peer_checkout.CallerCheckout(None, None)
    assert peer_checkout.caller_checkout(None, client_name="x", metadata={}) == none
    # A malformed Git marker is unreadable metadata, not a reason to fail the call.
    marker = repos.main_wt / ".git"
    marker.unlink()  # Git may mark the file hidden on Windows, which forbids truncating it.
    marker.write_text("not a pointer\n")
    result = peer_checkout.caller_checkout(
        request, client_name="claude-code", metadata={}, proc_root=proc
    )
    assert result.checkout is None and result.peer is not None
    # Codex with no handle map at all keeps the server cwd.
    monkeypatch.setattr(peer_checkout, "resolve_common_booley_dir", lambda: tmp_path / "missing")
    codex = peer_checkout.caller_checkout(
        request, client_name="codex", metadata={"threadId": THREAD_ID}, proc_root=proc
    )
    assert codex.checkout is None


# --- Routing the call: work_dir injection ---------------------------------------------


def capturing_application(*schemas: dict[str, Any]) -> tuple[McpApplication, list[dict[str, Any]]]:
    """An application whose tools record the arguments they are dispatched with."""
    seen: list[dict[str, Any]] = []

    async def dispatch(_name: str, arguments: dict[str, Any], *_rest: Any) -> McpDispatchResult:
        seen.append(arguments)
        return McpDispatchResult([TextContent(type="text", text="ran")], False)

    application = McpApplication(
        [{"name": f"tool{number}", "schema": schema} for number, schema in enumerate(schemas)],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=lambda _: None,
    )
    return application, seen


@pytest.mark.asyncio
async def test_explicit_work_dir_and_tools_without_work_dir_are_never_rewritten(
    repos: SimpleNamespace,
) -> None:
    with_work_dir = {
        "type": "object",
        "properties": {"work_dir": {"type": "string"}},
        "additionalProperties": False,
    }
    without = {"type": "object", "properties": {}, "additionalProperties": False}
    required = {**with_work_dir, "required": ["work_dir"]}
    application, seen = capturing_application(with_work_dir, without, required)
    context = McpRequestContext(checkout=repos.main_wt)

    await application.call_tool("tool0", {"work_dir": "/explicit"}, request_context=context)
    await application.call_tool("tool0", {}, request_context=context)
    await application.call_tool("tool0", {"work_dir": ""}, request_context=context)
    await application.call_tool("tool0", {}, request_context=McpRequestContext())
    await application.call_tool("tool1", {}, request_context=context)
    assert seen == [
        {"work_dir": "/explicit"},
        {"work_dir": str(repos.main_wt)},
        {"work_dir": str(repos.main_wt)},
        {},
        {},
    ]
    # Validation runs first, so a required work_dir stays required.
    refused = await application.call_tool("tool2", {}, request_context=context)
    assert refused.is_error and len(seen) == 5


@pytest.mark.asyncio
async def test_routed_caller_is_refused_by_an_endpoint_without_work_dir(
    repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AsyncMock(return_value=(0, "done", "", False))
    monkeypatch.setattr(server, "_run_inline_endpoint", runner)
    custom = {
        "name": "utility_custom",
        "description": "Project-defined",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "module": "utility_custom",
        "is_custom": True,
        "custom_path": "utility_custom.py",
        "default_timeout": 60,
    }
    application = server._build_mcp_application([custom], [], server._McpLifetime(None, None))
    routed = McpRequestContext(checkout=repos.main_wt)
    refused = await application.call_tool("utility_custom", {}, request_context=routed)
    assert refused.is_error
    assert "must declare `work_dir`" in refused.content[0].text
    runner.assert_not_awaited()
    # A caller that is not routed runs it on the server cwd as before.
    ran = await application.call_tool("utility_custom", {}, request_context=McpRequestContext())
    assert not ran.is_error
    runner.assert_awaited_once()


# --- The SDK boundary, end to end -----------------------------------------------------


@contextlib.asynccontextmanager
async def sdk_calls(
    application: McpApplication,
    sessions: SessionObserver | None,
    proc: Path,
    monkeypatch: pytest.MonkeyPatch,
    client_name: str = "claude-code",
) -> AsyncIterator[Callable[..., Awaitable[httpx.Response]]]:
    """Real stateful SDK HTTP transport; ``call(port, tool, arguments)`` posts as that peer."""
    from booley.mcp import peer_checkout

    lookup = session_peer.peer_identity
    monkeypatch.setattr(
        session_peer, "peer_identity", lambda *args, **_k: lookup(*args, proc_root=proc)
    )
    monkeypatch.setattr(
        peer_checkout,
        "caller_checkout",
        functools.partial(peer_checkout.caller_checkout, proc_root=proc),
    )
    monkeypatch.setattr(session_registry, "namespace", lambda *_: SCOPE)
    monkeypatch.setattr(session_observer, "namespace", lambda *_: SCOPE)
    lifetime = SimpleNamespace(
        sessions=sessions,
        mark_activity=lambda: None,
        mark_mcp_endpoint_start=lambda: None,
        mark_mcp_endpoint_end=lambda: None,
    )
    sdk = server._build_sdk_server(application, lifetime)  # type: ignore[arg-type]
    http = sdk.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    current = {"port": 0}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        scope = {**scope, "client": ("127.0.0.1", current["port"]), "server": ("127.0.0.1", 4321)}
        await session_peer.PeerBoundary(http)(scope, receive, send)

    headers = {"accept": "application/json, text/event-stream"}
    async with (
        sdk.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers=headers
        ) as client,
    ):
        current["port"] = min(
            int(line.split()[1].split(":")[1], 16)
            for line in (proc / "net/tcp").read_text().splitlines()[1:]
        )
        initialize = {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1"},
        }
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": initialize},
        )
        client.headers["mcp-session-id"] = response.headers["mcp-session-id"]
        client.headers["mcp-protocol-version"] = "2025-11-25"
        await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        numbers = iter(range(2, 1000))

        async def call(port: int, tool: str, arguments: dict[str, Any]) -> httpx.Response:
            current["port"] = port
            body = {"name": tool, "arguments": arguments}
            return await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": next(numbers),
                    "method": "tools/call",
                    "params": body,
                },
            )

        yield call


LINT_DEFINITION = {
    "name": "lint",
    "description": "Run lint",
    "schema": {
        "type": "object",
        "properties": {"work_dir": {"type": "string"}},
        "additionalProperties": False,
    },
    "module": "flow",
    "module_path": "booley.flows.lint.flow",
    "is_flow": True,
    "is_custom": False,
    "is_specialist": False,
    "default_timeout": 60,
}


@pytest.mark.asyncio
async def test_flow_call_without_work_dir_runs_on_the_callers_worktree(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AsyncMock(return_value=(0, "done", "", False))
    monkeypatch.setattr(server, "_run_inline_endpoint", runner)
    application = server._build_mcp_application(
        [LINT_DEFINITION], [], server._McpLifetime(None, None)
    )
    proc = fake_proc(tmp_path / "proc", {50001: (200, repos.main_wt), 50002: (201, repos.main)})
    # No Session observer: routing never depends on presentation succeeding.
    async with sdk_calls(application, None, proc, monkeypatch) as call:
        routed = await call(50001, "lint", {})
        main_checkout = await call(50002, "lint", {})
        explicit = await call(50001, "lint", {"work_dir": str(repos.other_wt)})
    assert all(response.status_code == 200 for response in (routed, main_checkout, explicit))
    commands = [item.args[1] for item in runner.await_args_list]
    assert commands[0][-2:] == ["--work-dir", str(repos.main_wt)]
    # A main-checkout caller is the server cwd: no checkout is named.
    assert "--work-dir" not in commands[1]
    assert commands[2][-2:] == ["--work-dir", str(repos.other_wt)]


@pytest.mark.asyncio
async def test_routed_and_explicit_calls_stamp_the_worktree_last_used(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = repos.main / ".booley"
    interactive = common / "worktrees" / "wt"
    git(repos.main, "worktree", "add", "-q", "-b", "wt-branch", str(interactive))
    monkeypatch.setenv("BOOLEY_DIR", str(common))
    stamp = common / "worktrees" / ".last-used" / "wt"
    monkeypatch.setattr(server, "_run_inline_endpoint", AsyncMock(return_value=(0, "", "", False)))
    application = server._build_mcp_application(
        [LINT_DEFINITION], [], server._McpLifetime(None, None)
    )
    proc = fake_proc(
        tmp_path / "proc",
        {50001: (200, interactive.resolve()), 50002: (201, repos.main), 50003: (202, repos.other)},
    )

    def age_stamp() -> None:
        """Set the stamp's mtime far in the past, creating it if absent."""
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (1, 1))

    # Observations are read inside the transport block and asserted outside it, so a
    # failed assertion is not wrapped in the SDK's TaskGroup exception.
    async with sdk_calls(application, None, proc, monkeypatch) as call:
        # A caller outside every interactive worktree stamps nothing.
        await call(50002, "lint", {})
        stamped_by_main_caller = stamp.exists()
        # Routed call without work_dir.
        await call(50001, "lint", {})
        stamped_by_routed_call = stamp.is_file()
        # A routed poll (schema without work_dir) stamps through the caller's checkout.
        age_stamp()
        await call(50001, "booley_poll", {"wait_seconds": 0})
        routed_poll_mtime = stamp.stat().st_mtime
        # An explicit work_dir stamps the worktree it names, whoever the caller is.
        age_stamp()
        await call(50003, "lint", {"work_dir": str(interactive)})
        explicit_mtime = stamp.stat().st_mtime
    assert not stamped_by_main_caller
    assert stamped_by_routed_call
    assert routed_poll_mtime > 1
    assert explicit_mtime > 1


@pytest.mark.asyncio
async def test_dashboard_lists_the_session_under_its_worktree(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, seen = capturing_application(LINT_DEFINITION["schema"])
    booley = tmp_path / "booley"
    booley.mkdir()
    observer = SessionObserver(budget=1)
    proc = fake_proc(tmp_path / "proc", {50001: (200, repos.main_wt)})
    monkeypatch.setattr(observer, "_registry", lambda: SessionRegistry(booley, proc_root=proc))
    async with sdk_calls(application, observer, proc, monkeypatch) as call:
        response = await call(50001, "tool0", {})
    assert response.status_code == 200, response.text
    assert seen == [{"work_dir": str(repos.main_wt)}]
    (row,) = SessionRegistry(booley).snapshot().rows
    assert row.attribution.work_dir == str(repos.main_wt)
    assert row.attribution.default_work_dir == ""
    assert row.attribution.process is not None and row.attribution.process.pid == 200
    app = DashboardApp(lambda: None)
    app.snapshot = model.DashboardSnapshot(sessions=(row,), observed_at=1791453900)
    facts = app._session_facts(row)
    assert facts.work_dir == str(repos.main_wt)
    assert "(default)" not in facts.work_dir


@pytest.mark.asyncio
async def test_two_callers_in_one_worktree_see_each_other_as_sharing(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[McpRequestContext] = []

    async def dispatch(*args: Any) -> McpDispatchResult:
        contexts.append(args[-1])
        return McpDispatchResult([TextContent(type="text", text="ran")], False)

    application = McpApplication(
        [{"name": "probe", "schema": LINT_DEFINITION["schema"]}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=lambda _: None,
    )
    booley = tmp_path / "booley"
    booley.mkdir()
    observer = SessionObserver(budget=1)
    proc = fake_proc(tmp_path / "proc", {50001: (200, repos.main_wt), 50002: (201, repos.main_wt)})
    monkeypatch.setattr(observer, "_registry", lambda: SessionRegistry(booley, proc_root=proc))
    async with sdk_calls(application, observer, proc, monkeypatch) as call:
        await call(50001, "probe", {})
        await call(50002, "probe", {})
    first, second = contexts
    assert first.attribution is not None and second.attribution is not None
    assert first.attribution.worktree_key == second.attribution.worktree_key != ""
    assert first.attribution.key in second.other_session_keys
    assert "another session shares" in second.presentation_warning


@pytest.mark.asyncio
async def test_boundary_lookup_failure_degrades_to_the_server_cwd(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.mcp import peer_checkout

    application, seen = capturing_application(LINT_DEFINITION["schema"])
    proc = fake_proc(tmp_path / "proc", {50001: (200, repos.main_wt)})

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("lookup exploded")

    async with sdk_calls(application, None, proc, monkeypatch) as call:
        monkeypatch.setattr(peer_checkout, "caller_checkout", broken)
        response = await call(50001, "tool0", {})
    assert response.status_code == 200, response.text
    assert seen == [{}]
