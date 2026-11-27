"""Per-message peer lookup through the real stateful SDK transport."""

import functools
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent

from booley.mcp import agent_thread, server, session_peer
from booley.mcp.application import McpApplication, McpDispatchResult
from tests.mcp_tools.test_peer_checkout import fake_proc


def _proc_peers(tmp_path, monkeypatch):
    """Two fake client processes, one per port, each with its own T3 Code thread."""
    proc = fake_proc(tmp_path / "proc", {50001: (98765, None), 50002: (98766, None)})
    for pid in (98765, 98766):
        (proc / str(pid) / "environ").write_bytes(f"T3CODE_THREAD_ID=t3-{pid}\0".encode())
    lookup = session_peer.peer_identity
    monkeypatch.setattr(
        session_peer, "peer_identity", lambda *args, **_k: lookup(*args, proc_root=proc)
    )
    # The server names no /proc of its own, so the thread read is pointed at the fake one.
    monkeypatch.setattr(
        server,
        "resolve_agent_thread",
        functools.partial(agent_thread.resolve_agent_thread, proc_root=proc),
    )


def _thread(context):
    return None if context.agent is None else context.agent.thread


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_each_http_post_uses_its_own_peer_in_one_sdk_session(
    tmp_path, monkeypatch, stateless
):
    _proc_peers(tmp_path, monkeypatch)
    seen = []

    async def dispatch(_name, _arguments, _source, context):
        seen.append(_thread(context))
        return McpDispatchResult([TextContent(type="text", text="ready")], False)

    application = McpApplication(
        [{"name": "probe", "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    lifetime = SimpleNamespace(
        mark_activity=lambda: None,
        mark_mcp_endpoint_start=lambda: None,
        mark_mcp_endpoint_end=lambda: None,
    )
    sdk = server._build_sdk_server(application, lifetime)
    http = sdk.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=stateless,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    port = 50001

    async def app(scope, receive, send):
        scope = {**scope, "client": ("127.0.0.1", port), "server": ("127.0.0.1", 4321)}
        await session_peer.PeerBoundary(http)(scope, receive, send)

    headers = {"accept": "application/json, text/event-stream"}
    async with (
        sdk.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers=headers,
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "claude-code", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            client.headers["mcp-session-id"] = session_id
        client.headers["mcp-protocol-version"] = "2025-11-25"
        await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        for number, next_port in enumerate((50001, 50002), 2):
            port = next_port
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": number,
                    "method": "tools/call",
                    "params": {"name": "probe", "arguments": {"work_dir": str(tmp_path)}},
                },
            )
            assert response.status_code == 200, response.text
    assert seen == ([None, None] if stateless else ["t3-98765", "t3-98766"])


@pytest.mark.asyncio
async def test_reused_sdk_handler_task_reads_message_request_not_inherited_peer(
    tmp_path, monkeypatch
):
    import asyncio

    from mcp.server.context import ServerRequestContext
    from mcp.shared.message import ServerMessageMetadata
    from mcp.types import CallToolRequestParams
    from starlette.requests import Request

    _proc_peers(tmp_path, monkeypatch)
    queue = asyncio.Queue()
    session = SimpleNamespace(
        client_params=SimpleNamespace(client_info=SimpleNamespace(name="claude-code")),
        client_capabilities=None,
    )
    seen = []

    async def worker():
        for _ in range(2):
            metadata = await queue.get()
            ctx = ServerRequestContext(
                session=session,
                lifespan_context={},
                protocol_version="2025-11-25",
                method="tools/call",
                request=metadata.request_context,
            )
            params = CallToolRequestParams(name="probe", arguments={"work_dir": str(tmp_path)})
            seen.append(_thread(await server._request_context(ctx, params, None)))

    task = None

    async def downstream(scope, _receive, _send):
        nonlocal task
        if task is None:
            task = asyncio.create_task(worker())
        await queue.put(ServerMessageMetadata(request_context=Request(scope)))

    for port in (50001, 50002):
        await session_peer.PeerBoundary(downstream)(
            {
                "type": "http",
                "client": ("127.0.0.1", port),
                "server": ("127.0.0.1", 4321),
                "headers": [],
            },
            None,
            None,
        )
    await task
    assert seen == ["t3-98765", "t3-98766"]
