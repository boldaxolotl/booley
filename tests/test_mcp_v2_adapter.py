"""Real MCP 2.x coverage for Booley's transport adapter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mcp import Client, MCPError
from mcp.client.subscriptions import ToolsListChanged
from mcp.server.transport_security import TransportSecurityMiddleware
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, CallToolRequestParams
from starlette.requests import Request

import tests.mcp_runner_fake as fake
from booley.mcp import server as mcp_server
from booley.runtime.booley_dir import reset_cache
from tests.time_budget import hang_guard_s, scaled


def _server(tmp_path, monkeypatch):
    """Build the SDK server over the real catalog; its endpoint child process is faked."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    reset_cache()
    fake.install(monkeypatch)
    # Private seam: only `main()` builds the SDK server, and it serves until stopped.
    return mcp_server._build_server()[0]


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    """A server started where no Project is selected: a live catalog over built-ins only."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    yield _server(tmp_path, monkeypatch)
    reset_cache()


@pytest.fixture
def source_sdk(tmp_path, monkeypatch):
    """A server started in Booley's own Source Checkout: built-in tools, no live catalog."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n", encoding="utf-8"
    )
    # Private seam: real discovery refuses a Source Checkout; schema-only adapters serve the
    # built-in tools without selecting a Project, and no public entry builds that server.
    monkeypatch.setattr(mcp_server, "_discover_booley_mcp_tools", lambda: ([], []))
    yield _server(tmp_path, monkeypatch)
    reset_cache()


@pytest.fixture
def catalog_dir(tmp_path, monkeypatch):
    """A Project whose Booley Directory the running catalog watches."""
    directory = tmp_path / ".booley"
    directory.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    return directory


@pytest.fixture
def project_sdk(tmp_path, monkeypatch, catalog_dir):
    """The SDK server built over `catalog_dir`."""
    yield _server(tmp_path, monkeypatch)
    reset_cache()


def _http_app(sdk):
    # Private seam: `main()` serves the HTTP app on a fixed loopback port until stopped.
    return mcp_server._streamable_http_app(sdk)


def _peer_state(changes) -> tuple[int, bool]:
    # Private state: ToolChanges exposes no peer count; clients see only notifications.
    return len(changes._peers), changes._running


def test_modern_inprocess_client_lists_and_calls_without_initialize(source_sdk) -> None:
    sdk = source_sdk

    async def exercise() -> None:
        async with Client(sdk, mode="2026-07-28") as client:
            assert client.protocol_version == "2026-07-28"
            listed = await client.list_tools()
            assert "bwave" in [tool.name for tool in listed.tools]
            called = await client.call_tool("bwave", {"extra_args": ["--help"]})
            assert called.is_error is False
            assert "EXIT_CODE: 0" in called.content[0].text

    asyncio.run(exercise())


def test_schema_failure_is_tool_error_but_unknown_tool_is_protocol_error(source_sdk) -> None:
    sdk = source_sdk

    async def exercise() -> None:
        async with Client(sdk, mode="2026-07-28") as client:
            invalid = await client.call_tool("bwave", {"unexpected": True})
            assert invalid.is_error is True
            assert "Invalid tool arguments at $" in invalid.content[0].text
            with pytest.raises(MCPError) as raised:
                await client.call_tool("missing", {})
            assert raised.value.code == INVALID_PARAMS

    asyncio.run(exercise())


def test_http_adapter_rejects_host_and_origin_injection(source_sdk) -> None:
    sdk = source_sdk
    _http_app(sdk)
    settings = sdk._session_manager.security_settings
    middleware = TransportSecurityMiddleware(settings)

    def request(host: str, origin: str | None = None) -> Request:
        headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        return Request({"type": "http", "method": "POST", "headers": headers})

    hostile_host = asyncio.run(middleware.validate_request(request("attacker.invalid"), True))
    hostile_origin = asyncio.run(
        middleware.validate_request(request("localhost", "https://attacker.invalid"), True)
    )
    loopback = asyncio.run(middleware.validate_request(request("localhost"), True))

    assert hostile_host is not None and hostile_host.status_code == 421
    assert hostile_origin is not None and hostile_origin.status_code == 403
    assert loopback is None


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (MCPError(INVALID_PARAMS, "known protocol error"), INVALID_PARAMS),
        (RuntimeError("private detail"), INTERNAL_ERROR),
    ],
    ids=["known", "unexpected"],
)
def test_application_call_translates_only_unexpected_errors(
    sdk, monkeypatch, error, expected_code
) -> None:
    fake.install(monkeypatch, error)

    async def exercise() -> None:
        async with Client(sdk, mode="2026-07-28") as client:
            with pytest.raises(MCPError) as raised:
                await client.call_tool("bwave", {"extra_args": ["--help"]})
            assert raised.value.code == expected_code
            if expected_code == INTERNAL_ERROR:
                assert raised.value.message == "Internal server error"

    asyncio.run(exercise())


def test_context_free_application_call_translates_only_unexpected_errors() -> None:
    # Private seam: the SDK always supplies a request context and an observer; only a direct
    # call reaches the context-free arm.
    params = CallToolRequestParams(name="broken", arguments={})
    for error, code in (
        (MCPError(INVALID_PARAMS, "known"), INVALID_PARAMS),
        (RuntimeError("x"), INTERNAL_ERROR),
    ):

        async def call_tool(_name, _arguments, error=error):
            raise error

        with pytest.raises(MCPError) as raised:
            asyncio.run(
                mcp_server._call_application_tool(SimpleNamespace(call_tool=call_tool), params)
            )
        assert raised.value.code == code


async def _notification_exercise(sdk, directory, *, edit_before_list=False):
    received = False
    async with Client(sdk, mode="2026-07-28") as client:
        try:
            async with client.listen(tools_list_changed=True) as subscription:
                (directory / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
                if edit_before_list:
                    await client.list_tools()
                event = await asyncio.wait_for(anext(subscription), hang_guard_s())
                assert isinstance(event, ToolsListChanged)
                received = True
        except (MCPError, TimeoutError):
            pass
    assert received, "running catalog must notify without a list request"


def test_running_catalog_sends_list_changed(project_sdk, catalog_dir):
    sdk = project_sdk
    from tests.mcp_tools.test_live_catalog import write_endpoint

    directory = catalog_dir
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
                event = await asyncio.wait_for(anext(subscription), hang_guard_s())
                assert isinstance(event, ToolsListChanged)
                # Settle this generation before editing again. Each invalidation
                # must still arrive before its first tools/list request.
                await client.list_tools()

    asyncio.run(exercise())


def test_list_refresh_does_not_consume_pending_notification(project_sdk, catalog_dir):
    asyncio.run(_notification_exercise(project_sdk, catalog_dir, edit_before_list=True))


async def _http_notification_case(sdk, directory, *, legacy, concurrent=False):
    import socket
    from contextlib import asynccontextmanager

    import uvicorn

    app = _http_app(sdk)

    @asynccontextmanager
    async def serving():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            host = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
            task = asyncio.create_task(host.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(hang_guard_s()):
                    while not host.started:
                        await asyncio.sleep(0.01)
                yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
            finally:
                host.should_exit = True
                await asyncio.wait_for(task, hang_guard_s())

    async with serving() as url:
        if concurrent:
            await _concurrent_http_notifications(url, directory, sdk)
            return
        if not legacy:
            await _notification_exercise(url, directory)
            return
        await _legacy_http_notification(url, directory)


def test_http_json_client_receives_catalog_change_subscription(project_sdk, catalog_dir):
    asyncio.run(_http_notification_case(project_sdk, catalog_dir, legacy=False))


def test_legacy_http_json_client_receives_catalog_change(project_sdk, catalog_dir):
    asyncio.run(_http_notification_case(project_sdk, catalog_dir, legacy=True))


def test_concurrent_http_clients_share_poller_and_survive_peer_disconnect(
    project_sdk, catalog_dir
):
    asyncio.run(_http_notification_case(project_sdk, catalog_dir, legacy=False, concurrent=True))


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
                asyncio.gather(anext(first_events), anext(second_events)), hang_guard_s()
            )
            assert all(isinstance(event, ToolsListChanged) for event in events)
            assert "sim" not in {tool.name for tool in (await second.list_tools()).tools}
        assert _peer_state(sdk.lifespan.__self__)[1]
        (directory / "booley.toml").write_text("[flows.sim]\nenabled = true\n")
        event = await asyncio.wait_for(anext(first_events), hang_guard_s())
        assert isinstance(event, ToolsListChanged)
        assert "sim" in {tool.name for tool in (await first.list_tools()).tools}


async def _legacy_http_notification(url, directory):
    import httpx

    async with httpx.AsyncClient(timeout=hang_guard_s()) as client:
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
            url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        received = False
        async with client.stream("GET", url, headers=headers) as stream:
            (directory / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
            try:
                async with asyncio.timeout(hang_guard_s()):
                    async for line in stream.aiter_lines():
                        if "notifications/tools/list_changed" in line:
                            received = True
                            break
            except TimeoutError:
                pass
        assert received, "running catalog must notify without a list request"


def test_legacy_peer_cleanup_and_modern_peer_exclusion(sdk):
    changes = sdk.lifespan.__self__

    async def exercise():
        async with Client(sdk, mode="legacy") as client:
            await client.list_tools()
            assert _peer_state(changes) == (1, True)
        assert _peer_state(changes) == (0, False)
        async with Client(sdk, mode="2026-07-28") as client:
            await client.list_tools()
            assert _peer_state(changes)[0] == 0
        assert _peer_state(changes) == (0, False)

    asyncio.run(exercise())


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
            # Private seam: shortens the send timer the slow peer must outlive.
            monkeypatch.setattr("booley.mcp.tool_changes._SEND_TIMEOUT_SECONDS", scaled(0.05))
            await first.publish()
            assert sent == ["good"]
            assert _peer_state(first)[0] == 1
            assert _peer_state(second)[0] == 0
        assert _peer_state(first)[0] == 0

    asyncio.run(exercise())


def test_legacy_http_idle_expiry_removes_notification_peer(sdk):
    import httpx

    changes = sdk.lifespan.__self__
    app = _http_app(sdk)
    sdk._session_manager.session_idle_timeout = scaled(0.05)

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
            assert _peer_state(changes)[0] == 1
            async with asyncio.timeout(hang_guard_s()):
                while _peer_state(changes)[0]:
                    await asyncio.sleep(0.01)
            assert sdk._session_manager._server_instances == {}
        assert _peer_state(changes)[1] is False

    asyncio.run(exercise())


def test_stdio_run_cancellation_stops_catalog_poller(sdk):
    import anyio

    changes = sdk.lifespan.__self__

    async def exercise():
        incoming, reads = anyio.create_memory_object_stream(1)
        writes, outgoing = anyio.create_memory_object_stream(1)
        async with incoming, reads, writes, outgoing:
            task = asyncio.create_task(sdk.run(reads, writes, sdk.create_initialization_options()))
            async with asyncio.timeout(hang_guard_s()):
                while not _peer_state(changes)[1]:
                    await asyncio.sleep(0)
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), hang_guard_s())
            assert _peer_state(changes) == (0, False)
            assert not [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done()
            ]

    asyncio.run(exercise())
