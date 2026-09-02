"""Real MCP 2.x coverage for Booley's transport adapter."""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client, MCPError
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
    monkeypatch.setattr(mcp_server, "_reconcile_orphaned_locks", lambda: None)
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
