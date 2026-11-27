"""A call without ``work_dir`` runs in the checkout its calling agent works in (#2606)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent

from booley.mcp import server, session_peer
from booley.mcp.application import McpApplication, McpDispatchResult, McpRequestContext
from booley.runtime.pid import ProcessIdentity
from tests.conftest import symlink_or_skip
from tests.git_support import git

SERVER_PORT = 4321  # Matches the 10E1 endpoint written by ``fake_proc``.
SCOPE = "pid:[17]"
IDENTITY = ProcessIdentity(98765, SCOPE, 101)


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


def _fake_process(root: Path, pid: int, inode: str):
    directory = root / str(pid)
    (directory / "fd").mkdir(parents=True)
    (directory / "ns").mkdir()
    symlink_or_skip(directory / "ns/pid", Path(SCOPE))
    symlink_or_skip(directory / "fd/7", Path(f"socket:[{inode}]"))
    (directory / "stat").write_text(f"{pid} (client) S " + "0 " * 18 + "101 0\n")


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


# --- Which process: the loopback peer -------------------------------------------------


@pytest.mark.asyncio
async def test_reserved_headers_are_stripped_and_interleaved_requests_isolated():
    observed = []

    async def downstream(scope, _receive, _send):
        observed.append(scope["headers"])
        await asyncio.sleep(0)
        assert dict(scope["headers"])[b"x-booley-peer-port"] == str(scope["client"][1]).encode()

    app = session_peer.PeerBoundary(downstream)
    await asyncio.gather(
        *(
            app(
                {
                    "type": "http",
                    "client": ("127.0.0.1", port),
                    "server": ("127.0.0.1", 4321),
                    "headers": [
                        (b"X-Booley-Peer-Port", b"spoof"),
                        (b"x-booley-peer-port", b"spoof2"),
                        (b"safe", b"keep"),
                    ],
                },
                None,
                None,
            )
            for port in (50001, 50002)
        )
    )
    assert [dict(headers)[b"x-booley-peer-port"] for headers in observed] == [b"50001", b"50002"]
    assert session_peer.observed_peer(None) is None


def test_peer_endpoint_and_inode_unique_revalidation(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net/tcp").write_text("header\n0: 0100007F:C351 0100007F:10E1 01 0 0 0 0 0 123\n")
    _fake_process(proc, 98765, "123")
    args = (("127.0.0.1", 50001), ("127.0.0.1", 4321))
    assert session_peer.peer_identity(*args, proc_root=proc) == IDENTITY
    assert session_peer.peer_identity(("127.0.0.1", 50002), args[1], proc_root=proc) is None
    _fake_process(proc, 98766, "123")
    assert session_peer.peer_identity(*args, proc_root=proc) is None


def test_socket_pid_reuse_revalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net/tcp").write_text("header\n0: 0100007F:C351 0100007F:10E1 01 0 0 0 0 0 123\n")
    _fake_process(proc, 98765, "123")
    identities = iter((IDENTITY, ProcessIdentity(IDENTITY.pid, SCOPE, 102)))
    monkeypatch.setattr(
        session_peer, "capture_process_identity", lambda *_a, **_k: next(identities)
    )
    assert (
        session_peer.peer_identity(("127.0.0.1", 50001), ("127.0.0.1", 4321), proc_root=proc)
        is None
    )


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
    # No printable client name: the peer is not known to be a session process.
    unknown = peer_checkout.caller_checkout(request, client_name="", metadata={}, proc_root=proc)
    assert unknown.checkout is None
    # A Codex process cwd names no thread: without workspace metadata the call is refused.
    codex = peer_checkout.caller_checkout(
        request, client_name="codex-mcp-client", metadata={}, proc_root=proc
    )
    assert codex.checkout is None and codex.refusal == peer_checkout.NO_WORKSPACE_REFUSAL


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


def test_caller_checkout_falls_back_to_the_server_cwd_on_unreadable_state(
    tmp_path: Path, repos: SimpleNamespace
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
    # The same unreadable workspace is not a missing one: Codex keeps the server cwd.
    workspace = {"x-codex-turn-metadata": {"workspaces": {str(repos.main_wt): {}}}}
    codex = peer_checkout.caller_checkout(
        request, client_name="codex", metadata=workspace, proc_root=proc
    )
    assert codex.checkout is None and codex.refusal is None


def test_codex_workspace_with_an_unreadable_git_marker_does_not_hide_a_later_worktree(
    tmp_path: Path, repos: SimpleNamespace
) -> None:
    from booley.mcp import peer_checkout

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_bytes(b"not a gitdir marker\n")
    metadata = {"x-codex-turn-metadata": {"workspaces": {str(broken): {}, str(repos.main_wt): {}}}}

    result = peer_checkout.caller_checkout(None, client_name="codex", metadata=metadata)

    assert result.checkout == repos.main_wt and result.refusal is None


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


CUSTOM_DEFINITION = {
    "name": "utility_custom",
    "description": "Project-defined",
    "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    "module": "utility_custom",
    "is_custom": True,
    "custom_path": "utility_custom.py",
    "default_timeout": 60,
}


@pytest.mark.asyncio
async def test_routed_caller_is_refused_by_an_endpoint_without_work_dir(
    repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AsyncMock(return_value=(0, "done", "", False))
    monkeypatch.setattr(server, "_run_inline_endpoint", runner)
    application = server._build_mcp_application(
        [CUSTOM_DEFINITION], [], server._McpLifetime(None, None)
    )
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
    sdk = server._build_sdk_server(application, server._McpLifetime(None, None))
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

        async def call(
            port: int, tool: str, arguments: dict[str, Any], meta: dict[str, Any] | None = None
        ) -> httpx.Response:
            current["port"] = port
            body = {"name": tool, "arguments": arguments} | ({"_meta": meta} if meta else {})
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
    async with sdk_calls(application, proc, monkeypatch) as call:
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
async def test_codex_call_routes_by_the_workspace_in_its_turn_metadata(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AsyncMock(return_value=(0, "done", "", False))
    monkeypatch.setattr(server, "_run_inline_endpoint", runner)
    application = server._build_mcp_application(
        [LINT_DEFINITION, CUSTOM_DEFINITION], [], server._McpLifetime(None, None)
    )
    # The shared app-server's own cwd names no thread, so only the metadata can route.
    proc = fake_proc(tmp_path / "proc", {50001: (200, repos.elsewhere)})

    def turn(*roots: Path) -> dict[str, Any]:
        return {"x-codex-turn-metadata": {"workspaces": {str(root): {} for root in roots}}}

    async with sdk_calls(application, proc, monkeypatch, "codex-mcp-client") as call:
        results = [
            (await call(50001, tool, arguments, meta)).json()["result"]
            for tool, arguments, meta in [
                ("lint", {}, turn(repos.main_wt)),
                ("lint", {}, turn(repos.main)),
                ("lint", {}, turn(repos.elsewhere)),
                ("lint", {}, turn(repos.main, repos.other_wt, repos.main_wt)),
                ("lint", {}, {"x-codex-turn-metadata": {}}),
                ("lint", {"work_dir": str(repos.other_wt)}, {"x-codex-turn-metadata": {}}),
                ("utility_custom", {}, turn(repos.main_wt)),
            ]
        ]
    commands = [item.args[1] for item in runner.await_args_list]
    routed = [command[-1] if "--work-dir" in command else None for command in commands]
    assert routed == [str(repos.main_wt), None, None, str(repos.main_wt), str(repos.other_wt)]
    assert [result["isError"] for result in results] == [False] * 4 + [True, False, True]
    assert "Retry the call" in results[4]["content"][0]["text"]
    assert "must declare `work_dir`" in results[6]["content"][0]["text"]


@pytest.mark.asyncio
async def test_status_card_names_the_routed_checkout(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = repos.main / ".booley"
    common.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(common))
    monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")
    proc = fake_proc(tmp_path / "proc", {50002: (201, repos.main), 50001: (200, repos.main_wt)})
    application = server._build_mcp_application([], [], server._McpLifetime(None, None))
    cards: dict[int, str] = {}
    async with sdk_calls(application, proc, monkeypatch) as call:
        for port in (50002, 50001):
            response = await call(port, "booley_status", {})
            assert response.status_code == 200, response.text
            cards[port] = response.json()["result"]["content"][0]["text"]
    assert "Session:" not in cards[50002] + cards[50001]
    assert f"Branch: main\nCheckout: {repos.main} (main checkout)\n" in cards[50002]
    assert f"Branch: main-work\nCheckout: {repos.main_wt} (worktree)\n" in cards[50001]


@pytest.mark.asyncio
async def test_boundary_lookup_failure_degrades_to_the_server_cwd(
    tmp_path: Path, repos: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.mcp import peer_checkout

    application, seen = capturing_application(LINT_DEFINITION["schema"])
    proc = fake_proc(tmp_path / "proc", {50001: (200, repos.main_wt)})

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("lookup exploded")

    async with sdk_calls(application, proc, monkeypatch) as call:
        monkeypatch.setattr(peer_checkout, "caller_checkout", broken)
        response = await call(50001, "tool0", {})
    assert response.status_code == 200, response.text
    assert seen == [{}]
