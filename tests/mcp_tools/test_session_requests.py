"""Per-message HTTP attribution through the real stateful SDK transport."""

import contextlib
import itertools
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent

from booley.mcp import server, session_peer, session_registry
from booley.mcp.application import McpApplication, McpDispatchResult
from booley.mcp.session_observer import SessionObserver
from booley.mcp.session_registry import SessionRegistry
from tests.mcp_tools.test_session_registry import SCOPE, _fake_process


def _proc_peers(tmp_path):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net/tcp").write_text(
        "header\n0: 0100007F:C351 0100007F:10E1 01 0 0 0 0 0 123\n"
        "1: 0100007F:C352 0100007F:10E1 01 0 0 0 0 0 124\n"
    )
    _fake_process(proc, 98765, "123")
    _fake_process(proc, 98766, "124")
    return proc


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_each_http_post_uses_its_own_peer_in_one_sdk_session(
    tmp_path, monkeypatch, stateless
):
    proc = _proc_peers(tmp_path)
    lookup = session_peer.peer_identity
    monkeypatch.setattr(
        session_peer, "peer_identity", lambda *args, **_k: lookup(*args, proc_root=proc)
    )
    monkeypatch.setattr(session_registry, "namespace", lambda *_: SCOPE)
    monkeypatch.setattr(session_registry, "_worktree_facts", lambda p: (p, "fixture", "main"))
    seen = []

    async def dispatch(_name, _arguments, _source, context):
        seen.append(context.attribution.process.pid if context.attribution.process else None)
        return McpDispatchResult([TextContent(type="text", text="ready")], False)

    application = McpApplication(
        [{"name": "probe", "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )
    observer = SessionObserver(budget=1)
    monkeypatch.setattr(observer, "_registry", lambda: SessionRegistry(tmp_path))
    lifetime = SimpleNamespace(
        sessions=observer,
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
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/list",
            },
        )
        assert response.status_code == 200, response.text
    assert seen == ([None, None] if stateless else [98765, 98766])
    if not stateless:
        rows = SessionRegistry(tmp_path).snapshot().rows
        assert len(rows) == 2
        assert all(row.attribution.work_dir == str(tmp_path) for row in rows)
        assert all(row.calls[-1].tool == "probe" for row in rows)


@pytest.mark.asyncio
async def test_reused_sdk_handler_task_reads_message_request_not_inherited_peer(
    tmp_path, monkeypatch
):
    import asyncio

    from mcp.server.context import ServerRequestContext
    from mcp.shared.message import ServerMessageMetadata
    from mcp.types import CallToolRequestParams
    from starlette.requests import Request

    proc = _proc_peers(tmp_path)
    lookup = session_peer.peer_identity
    monkeypatch.setattr(
        session_peer, "peer_identity", lambda *args, **_k: lookup(*args, proc_root=proc)
    )
    monkeypatch.setattr(session_registry, "_worktree_facts", lambda p: (p, "fixture", "main"))
    observer = SessionObserver(budget=1)
    monkeypatch.setattr(observer, "_registry", lambda: SessionRegistry(tmp_path))
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
            request = await server._observed_request_context(
                ctx,
                CallToolRequestParams(name="probe", arguments={"work_dir": str(tmp_path)}),
                observer,
            )
            seen.append(request.attribution.process.pid if request.attribution.process else None)

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
    assert seen == [98765, 98766]


def _probe_application():
    async def dispatch(_name, _arguments, _source, _context):
        return McpDispatchResult([TextContent(type="text", text="ready")], False)

    return McpApplication(
        [{"name": "probe", "schema": {"type": "object"}}],
        dispatch=dispatch,
        request_dispatch=dispatch,
        canonicalize=lambda n: n,
        on_discovery_error=lambda _: None,
    )


@contextlib.asynccontextmanager
async def _sdk_session(observer, client_name):
    """One stateful SDK session over ASGI whose peer lookup names no process."""
    lifetime = SimpleNamespace(
        sessions=observer,
        mark_activity=lambda: None,
        mark_mcp_endpoint_start=lambda: None,
        mark_mcp_endpoint_end=lambda: None,
    )
    sdk = server._build_sdk_server(_probe_application(), lifetime)
    http = sdk.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=False,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def app(scope, receive, send):
        scope = {**scope, "client": ("127.0.0.1", 50001), "server": ("127.0.0.1", 4321)}
        await session_peer.PeerBoundary(http)(scope, receive, send)

    ids = itertools.count(1)
    async with (
        sdk.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"accept": "application/json, text/event-stream"},
        ) as client,
    ):

        async def post(method, params=None):
            body = {"jsonrpc": "2.0", "id": next(ids), "method": method}
            if params is not None:
                body["params"] = params
            response = await client.post("/mcp", json=body)
            assert response.status_code == 200, response.text
            return response

        response = await post(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "1"},
            },
        )
        client.headers["mcp-session-id"] = response.headers["mcp-session-id"]
        client.headers["mcp-protocol-version"] = "2025-11-25"
        await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        yield post


def _observer_without_peer(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(session_peer, "peer_identity", lambda *_a, **_k: None)
    monkeypatch.setattr(session_registry, "namespace", lambda *_: SCOPE)
    monkeypatch.setattr(session_registry, "_worktree_facts", lambda p: (p, "fixture", "main"))
    observer = SessionObserver(now=lambda: clock[0], budget=1)
    monkeypatch.setattr(observer, "_registry", lambda: SessionRegistry(tmp_path))
    return observer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_name", "meta"),
    [
        pytest.param("claude-code", None, id="claude-code"),
        pytest.param("codex-mcp-client", {"threadId": "listing"}, id="codex"),
    ],
)
async def test_tools_list_alone_creates_no_session_row(tmp_path, monkeypatch, client_name, meta):
    observer = _observer_without_peer(tmp_path, monkeypatch, [100.0])
    async with _sdk_session(observer, client_name) as post:
        for _ in range(2):
            await post("tools/list", None if meta is None else {"_meta": meta})
    assert SessionRegistry(tmp_path).snapshot().rows == ()
    assert not list((tmp_path / "runtime" / "sessions").glob("*.json"))


@pytest.mark.asyncio
async def test_tools_list_never_refreshes_an_existing_row(tmp_path, monkeypatch):
    clock = [100.0]
    observer = _observer_without_peer(tmp_path, monkeypatch, clock)
    row = SessionRegistry(tmp_path).path("codex:listing")
    meta = {"threadId": "listing"}
    async with _sdk_session(observer, "codex-mcp-client") as post:
        await post(
            "tools/call",
            {"name": "probe", "arguments": {"work_dir": str(tmp_path)}, "_meta": meta},
        )
        if observer._pending is not None:  # Let the call's registry write finish first.
            await observer._pending
        before = row.read_bytes()
        clock[0] = 200.0
        await post("tools/list", {"_meta": meta})
    assert SessionRegistry(tmp_path).snapshot().rows[0].last_call_at == "1970-01-01T00:01:40Z"
    assert row.read_bytes() == before
