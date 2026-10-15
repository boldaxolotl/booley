"""Real MCP 2.x coverage for Booley's transport adapter."""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client, MCPError
from mcp.client.subscriptions import ToolsListChanged
from mcp.server.transport_security import TransportSecurityMiddleware
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, CallToolRequestParams, TextContent
from starlette.requests import Request

from booley.mcp import server as mcp_server


def _test_server(monkeypatch: pytest.MonkeyPatch):
    definition = {
        "name": "booley_status",
        "description": "Status",
        "schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    monkeypatch.setattr(mcp_server, "_reconcile_orphaned_jobs", lambda: None)
    monkeypatch.setattr(mcp_server, "_discover_booley_mcp_tools", lambda: ([], []))
    monkeypatch.setattr(mcp_server, "_build_mcp_tool_index", lambda _tools: {})
    monkeypatch.setattr(mcp_server, "_all_mcp_tool_defs", lambda _tools: [definition])

    async def dispatch_status(name, arguments, jobs, names):
        return [TextContent(type="text", text=f"{name}: ready")]

    monkeypatch.setattr(mcp_server, "_dispatch_special_mcp_tool", dispatch_status)
    return mcp_server._build_server()[0]


def test_modern_inprocess_client_lists_and_calls_without_initialize(monkeypatch) -> None:
    server = _test_server(monkeypatch)

    async def exercise() -> None:
        async with Client(server, mode="2026-07-28") as client:
            assert client.protocol_version == "2026-07-28"
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == ["booley_status"]
            called = await client.call_tool("booley_status", {})
            assert called.is_error is False
            assert called.content[0].text == "booley_status: ready"

    asyncio.run(exercise())


def test_schema_failure_is_tool_error_but_unknown_tool_is_protocol_error(monkeypatch) -> None:
    server = _test_server(monkeypatch)

    async def exercise() -> None:
        async with Client(server, mode="2026-07-28") as client:
            invalid = await client.call_tool("booley_status", {"unexpected": True})
            assert invalid.is_error is True
            assert "Invalid tool arguments at $" in invalid.content[0].text
            with pytest.raises(MCPError) as raised:
                await client.call_tool("missing", {})
            assert raised.value.code == INVALID_PARAMS

    asyncio.run(exercise())


def test_http_adapter_rejects_host_and_origin_injection(monkeypatch) -> None:
    server = _test_server(monkeypatch)
    mcp_server._streamable_http_app(server)
    settings = server._session_manager.security_settings
    middleware = TransportSecurityMiddleware(settings)

    def request(host: str, origin: str | None = None) -> Request:
        headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        return Request({"type": "http", "method": "POST", "headers": headers})

    hostile_host = asyncio.run(middleware.validate_request(request("attacker.invalid"), True))
    hostile_origin = asyncio.run(
        middleware.validate_request(
            request("localhost", "https://attacker.invalid"),
            True,
        )
    )
    loopback = asyncio.run(middleware.validate_request(request("localhost"), True))

    assert hostile_host is not None and hostile_host.status_code == 421
    assert hostile_origin is not None and hostile_origin.status_code == 403
    assert loopback is None


def test_application_dispatch_routes_bwave_and_python_tools(monkeypatch) -> None:
    async def no_special(*_args):
        return None

    async def bwave(name, _arguments):
        return [TextContent(type="text", text="wave")] if name == "bwave" else None

    async def python_tool(name, arguments, definition, counts, jobs):
        assert (name, arguments, definition) == ("sim", {"target": "smoke"}, {"module": "sim"})
        assert counts == {} and jobs is sentinel
        return [TextContent(type="text", text="simulation")]

    sentinel = object()
    monkeypatch.setattr(mcp_server, "_dispatch_special_mcp_tool", no_special)
    monkeypatch.setattr(mcp_server, "_dispatch_bwave", bwave)
    monkeypatch.setattr(mcp_server, "_dispatch_booley_mcp_tool", python_tool)
    monkeypatch.setattr(mcp_server, "_prepend_changed_health_alert", lambda result: result)

    wave = asyncio.run(
        mcp_server._dispatch_application_tool(
            "bwave", {}, {}, jobs=sentinel, status_mcp_tool_names=[], mcp_tool_call_counts={}
        )
    )
    simulation = asyncio.run(
        mcp_server._dispatch_application_tool(
            "sim",
            {"target": "smoke"},
            {"module": "sim"},
            jobs=sentinel,
            status_mcp_tool_names=[],
            mcp_tool_call_counts={},
        )
    )

    assert wave[0].text == "wave"
    assert simulation[0].text == "simulation"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (MCPError(INVALID_PARAMS, "known protocol error"), INVALID_PARAMS),
        (RuntimeError("private detail"), INTERNAL_ERROR),
    ],
)
def test_application_call_translates_only_unexpected_errors(error, expected_code) -> None:
    class FailingApplication:
        async def call_tool(self, _name, _arguments):
            raise error

    params = CallToolRequestParams(name="broken", arguments={})

    with pytest.raises(MCPError) as raised:
        asyncio.run(mcp_server._call_application_tool(FailingApplication(), params))

    assert raised.value.code == expected_code
    if expected_code == INTERNAL_ERROR:
        assert raised.value.message == "Internal server error"


async def _notification_exercise(sdk, directory, *, edit_before_list=False):
    received = False
    async with Client(sdk, mode="2026-07-28") as client:
        try:
            async with client.listen(tools_list_changed=True) as subscription:
                (directory / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
                if edit_before_list:
                    await client.list_tools()
                event = await asyncio.wait_for(anext(subscription), 4)
                assert isinstance(event, ToolsListChanged)
                received = True
        except (MCPError, TimeoutError):
            pass
    assert received, "running catalog must notify without a list request"


def test_running_catalog_sends_list_changed(tmp_path, monkeypatch):
    from booley.runtime.booley_dir import reset_cache
    from tests.mcp_tools.test_live_catalog import write_endpoint

    directory = tmp_path / ".booley"
    directory.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    monkeypatch.chdir(tmp_path)
    reset_cache()
    monkeypatch.setattr(mcp_server, "_reconcile_orphaned_jobs", lambda: None)
    sdk, _ = mcp_server._build_server()
    assert sdk.get_capabilities().tools.list_changed, (
        "running catalog must advertise tools list changes"
    )

    async def exercise():
        async with (
            Client(sdk, mode="2026-07-28") as client,
            client.listen(tools_list_changed=True) as subscription,
        ):
            for action in (
                lambda: (directory / "booley.toml").write_text("[agent]\nmax_turns = 4\n"),
                lambda: write_endpoint(directory),
                lambda: write_endpoint(directory, option="changed"),
                (directory / "mcp_tools/local_check.py").unlink,
            ):
                action()
                event = await asyncio.wait_for(anext(subscription), 4)
                assert isinstance(event, ToolsListChanged)

    try:
        asyncio.run(exercise())
    finally:
        reset_cache()


def test_list_refresh_does_not_consume_pending_notification(tmp_path, monkeypatch):
    from booley.runtime.booley_dir import reset_cache

    directory = tmp_path / ".booley"
    directory.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    monkeypatch.chdir(tmp_path)
    reset_cache()
    monkeypatch.setattr(mcp_server, "_reconcile_orphaned_jobs", lambda: None)
    sdk, _ = mcp_server._build_server()
    try:
        asyncio.run(_notification_exercise(sdk, directory, edit_before_list=True))
    finally:
        reset_cache()


async def _http_notification_case(tmp_path, monkeypatch, *, legacy, concurrent=False):
    import socket
    from contextlib import asynccontextmanager

    import uvicorn

    from booley.runtime.booley_dir import reset_cache

    directory = tmp_path / ".booley"
    directory.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    monkeypatch.chdir(tmp_path)
    reset_cache()
    monkeypatch.setattr(mcp_server, "_reconcile_orphaned_jobs", lambda: None)
    sdk, _ = mcp_server._build_server()
    app = mcp_server._streamable_http_app(sdk)

    @asynccontextmanager
    async def serving():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            host = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
            task = asyncio.create_task(host.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not host.started:
                        await asyncio.sleep(0.01)
                yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
            finally:
                host.should_exit = True
                await asyncio.wait_for(task, 5)

    try:
        async with serving() as url:
            if concurrent:
                await _concurrent_http_notifications(url, directory, sdk)
                return
            if not legacy:
                await _notification_exercise(url, directory)
                return
            await _legacy_http_notification(url, directory)
    finally:
        reset_cache()


def test_http_json_client_receives_catalog_change_subscription(tmp_path, monkeypatch):
    asyncio.run(_http_notification_case(tmp_path, monkeypatch, legacy=False))


def test_legacy_http_json_client_receives_catalog_change(tmp_path, monkeypatch):
    asyncio.run(_http_notification_case(tmp_path, monkeypatch, legacy=True))


def test_concurrent_http_clients_share_poller_and_survive_peer_disconnect(tmp_path, monkeypatch):
    asyncio.run(_http_notification_case(tmp_path, monkeypatch, legacy=False, concurrent=True))


async def _concurrent_http_notifications(url, directory, sdk):
    async with (
        Client(url, mode="2026-07-28") as first,
        first.listen(tools_list_changed=True) as first_events,
    ):
        async with (
            Client(url, mode="2026-07-28") as second,
            second.listen(tools_list_changed=True) as second_events,
        ):
            (directory / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
            events = await asyncio.wait_for(
                asyncio.gather(anext(first_events), anext(second_events)), 4
            )
            assert all(isinstance(event, ToolsListChanged) for event in events)
            assert "sim" not in {tool.name for tool in (await second.list_tools()).tools}
        assert sdk.lifespan.__self__._running
        (directory / "booley.toml").write_text("[flows.sim]\nenabled = true\n")
        event = await asyncio.wait_for(anext(first_events), 4)
        assert isinstance(event, ToolsListChanged)
        assert "sim" in {tool.name for tool in (await first.list_tools()).tools}


async def _legacy_http_notification(url, directory):
    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        headers = {"accept": "application/json, text/event-stream"}
        initialized = await client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "catalog-test", "version": "1"},
                },
            },
        )
        assert initialized.headers["content-type"].startswith("application/json")
        headers["mcp-session-id"] = initialized.headers["mcp-session-id"]
        headers["mcp-protocol-version"] = "2025-11-25"
        # This POST fully closes before the standalone GET carries notifications.
        await client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        received = False
        async with client.stream("GET", url, headers=headers) as stream:
            (directory / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
            try:
                async with asyncio.timeout(4):
                    async for line in stream.aiter_lines():
                        if "notifications/tools/list_changed" in line:
                            received = True
                            break
            except TimeoutError:
                pass
        assert received, "running catalog must notify without a list request"


def test_legacy_peer_cleanup_and_modern_peer_exclusion(tmp_path, monkeypatch):
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    sdk = _test_server(monkeypatch)
    changes = sdk.lifespan.__self__

    async def exercise():
        async with Client(sdk, mode="legacy") as client:
            await client.list_tools()
            assert len(changes._peers) == 1
        assert changes._peers == {}
        assert changes._running is False
        async with Client(sdk, mode="2026-07-28") as client:
            await client.list_tools()
            assert changes._peers == {}
        assert changes._running is False

    try:
        asyncio.run(exercise())
    finally:
        reset_cache()


def test_change_fanout_bounds_failed_peers_and_isolates_servers(monkeypatch):
    from contextlib import AsyncExitStack
    from types import SimpleNamespace

    from booley.mcp.tool_changes import ToolChanges

    first, second = ToolChanges(None), ToolChanges(None)
    sent = []

    async def good_send():
        sent.append("good")

    async def broken_send():
        raise BrokenPipeError("disconnected")

    async def slow_send():
        await asyncio.Event().wait()

    async def next_handler(ctx):
        return None

    async def exercise():
        async with AsyncExitStack() as connections:
            for send in (good_send, broken_send, slow_send):
                stack = await connections.enter_async_context(AsyncExitStack())
                peer = SimpleNamespace(send_tool_list_changed=send, exit_stack=stack)
                context = SimpleNamespace(
                    method="initialize",
                    protocol_version="2025-11-25",
                    session=SimpleNamespace(_connection=peer),
                )
                await first.remember_legacy_peer(context, next_handler)
            monkeypatch.setattr("booley.mcp.tool_changes._SEND_TIMEOUT_SECONDS", 0.05)
            await first.publish()
            assert sent == ["good"]
            assert len(first._peers) == 1
            assert second._peers == {}
        assert first._peers == {}

    asyncio.run(exercise())


def test_legacy_http_idle_expiry_removes_notification_peer(tmp_path, monkeypatch):
    import httpx

    from booley.runtime.booley_dir import reset_cache

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    sdk = _test_server(monkeypatch)
    changes = sdk.lifespan.__self__
    app = mcp_server._streamable_http_app(sdk)
    sdk._session_manager.session_idle_timeout = 0.05

    async def exercise():
        async with (
            sdk._session_manager.run(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost"
            ) as client,
        ):
            response = await client.post(
                "/mcp",
                headers={"accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "idle-test", "version": "1"},
                    },
                },
            )
            assert response.status_code == 200
            assert len(changes._peers) == 1
            async with asyncio.timeout(2):
                while changes._peers:
                    await asyncio.sleep(0.01)
            assert sdk._session_manager._server_instances == {}
        assert changes._running is False

    try:
        asyncio.run(exercise())
    finally:
        reset_cache()


def test_stdio_run_cancellation_stops_catalog_poller(tmp_path, monkeypatch):
    import anyio

    from booley.runtime.booley_dir import reset_cache

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    sdk = _test_server(monkeypatch)
    changes = sdk.lifespan.__self__

    async def exercise():
        incoming, reads = anyio.create_memory_object_stream(1)
        writes, outgoing = anyio.create_memory_object_stream(1)
        async with incoming, reads, writes, outgoing:
            task = asyncio.create_task(sdk.run(reads, writes, sdk.create_initialization_options()))
            async with asyncio.timeout(2):
                while not changes._running:
                    await asyncio.sleep(0)
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            assert changes._running is False
            assert changes._peers == {}
            assert not [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done()
            ]

    try:
        asyncio.run(exercise())
    finally:
        reset_cache()
