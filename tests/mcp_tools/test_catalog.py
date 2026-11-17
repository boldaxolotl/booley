"""Catalog ownership and exposure."""

import asyncio
import os
from copy import deepcopy

import pytest

from booley.mcp import catalog, server
from booley.mcp.application import McpApplication
from booley.mcp.registry import discover_mcp_tools

MODES = (
    {},
    {"BOOLEY_MCP_MODE": "interactive"},
    {"BOOLEY_MCP_MODE": "interactive", "BOOLEY_MCP_DEBUG_TOOLS": "1"},
    {"BOOLEY_NESTED_AGENT": "1", "BOOLEY_NESTED_MCP_TOOLS": "sim"},
    {"BOOLEY_MCP_TOOLS": "lint"},
    {"BOOLEY_MCP_TOOLS": ""},
    {"BOOLEY_NESTED_AGENT": "1", "BOOLEY_NESTED_MCP_TOOLS": "sim", "BOOLEY_MCP_TOOLS": "lint"},
    {"BOOLEY_MCP_MODE": "interactive", "BOOLEY_MCP_TOOLS": "lint"},
    {
        "BOOLEY_NESTED_AGENT": "1",
        "BOOLEY_NESTED_MCP_TOOLS": "coverage_evidence",
        "BOOLEY_COVERAGE_RECORD": "record",
        "BOOLEY_COVERAGE_PROJECT": "/project",
    },
)
EXPOSURE_KEYS = set().union(*MODES)


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    for key in EXPOSURE_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def test_single_catalog_assembly_preserves_modes(monkeypatch):
    owner = getattr(catalog, "visible_definitions", None)
    assert callable(owner), "catalog assembly owner missing"
    custom = {"name": "z", "module": "custom", "schema": {}, "custom_path": "/custom.py"}
    for values in MODES:
        for key in EXPOSURE_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        exposure = catalog.Exposure.from_mapping(os.environ)
        endpoints, errors = catalog.discover_builtin_definitions(discover_mcp_tools(), exposure)
        assert not errors
        original = deepcopy(custom)
        definitions = owner(exposure, [*endpoints, custom])
        assert custom == original
        assert definitions[-1] is custom
        assert definitions == sorted(definitions, key=lambda tool: tool["name"])
        # server.py exposes no public builder; main() runs the stdio transport.
        lifetime = server._McpLifetime(None, None)  # the builder's required argument
        # Same builder as above: the application the stdio transport serves.
        tools = server._build_mcp_application([*endpoints, custom], [], lifetime).list_tools()
        assert [(t.name, t.input_schema) for t in tools] == [
            (d["name"], d["schema"]) for d in definitions
        ]


def test_server_uses_catalog_owner():
    assert not hasattr(server, "_all_mcp_tool_defs"), "server retains catalog forwarders"


@pytest.mark.parametrize(
    ("tool", "arguments", "accepted"),
    [
        ("booley_status", {"verbose": True}, False),
        ("booley_targets", {"glob": "*", "flow": "sim"}, False),
        ("booley_sleep", {"seconds": 1, "extra": 1}, False),
        ("booley_sleep", {}, False),
        ("booley_sleep", {"seconds": -1}, False),
        ("booley_sleep", {"seconds": 86401}, False),
        ("booley_sleep", {"seconds": "5"}, False),
        ("booley_status", {}, True),
        ("booley_targets", {}, True),
        ("booley_targets", {"for_flow": "sim", "glob": "soc*"}, True),
        ("booley_sleep", {"seconds": 0}, True),
        ("booley_sleep", {"seconds": 2.5}, True),
    ],
)
def test_utility_tool_schemas_validate_arguments_before_dispatch(tool, arguments, accepted):
    """Malformed utility-tool input gets a schema error and no dispatched call."""
    exposure = catalog.Exposure.from_mapping(
        {"BOOLEY_MCP_MODE": "interactive", "BOOLEY_MCP_DEBUG_TOOLS": "1"}
    )
    definition = next(d for d in catalog.visible_definitions(exposure, []) if d["name"] == tool)
    calls: list[tuple[str, dict]] = []

    async def dispatch(name, received, _source):
        calls.append((name, received))
        return []

    application = McpApplication(
        [definition],
        dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=pytest.fail,
    )
    payload = asyncio.run(application.call_tool(tool, arguments))

    assert payload.is_error is not accepted
    if accepted:
        assert calls == [(tool, arguments)]
    else:
        assert payload.content[0].text.startswith("Invalid tool arguments at $")
        assert calls == []
