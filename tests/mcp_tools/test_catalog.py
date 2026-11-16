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
        tools = server._build_mcp_application(
            [*endpoints, custom], [], server._McpLifetime(None, None)
        ).list_tools()
        assert [(t.name, t.input_schema) for t in tools] == [
            (d["name"], d["schema"]) for d in definitions
        ]


def test_server_uses_catalog_owner():
    assert not hasattr(server, "_all_mcp_tool_defs"), "server retains catalog forwarders"


@pytest.mark.parametrize(
    ("definition", "arguments"),
    [
        (catalog._status_mcp_tool_def, {"verbose": True}),
        (catalog._targets_mcp_tool_def, {"glob": "*", "flow": "sim"}),
        (catalog._sleep_mcp_tool_def, {"seconds": 1, "extra": 1}),
        (catalog._sleep_mcp_tool_def, {}),
        (catalog._sleep_mcp_tool_def, {"seconds": -1}),
        (catalog._sleep_mcp_tool_def, {"seconds": 86401}),
        (catalog._sleep_mcp_tool_def, {"seconds": "5"}),
    ],
)
def test_utility_tool_schemas_reject_bad_arguments_before_dispatch(definition, arguments):
    """Agents get a schema error, never a dispatched call, for malformed utility-tool input."""
    calls: list[str] = []

    async def dispatch(name, _arguments, _source):
        calls.append(name)
        return []

    application = McpApplication(
        [definition()],
        dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=pytest.fail,
    )
    payload = asyncio.run(application.call_tool(definition()["name"], arguments))

    assert payload.is_error
    assert payload.content[0].text.startswith("Invalid tool arguments at $")
    assert calls == []


@pytest.mark.parametrize(
    ("definition", "arguments"),
    [
        (catalog._status_mcp_tool_def, {}),
        (catalog._targets_mcp_tool_def, {}),
        (catalog._targets_mcp_tool_def, {"for_flow": "sim", "glob": "soc*"}),
        (catalog._sleep_mcp_tool_def, {"seconds": 0}),
        (catalog._sleep_mcp_tool_def, {"seconds": 2.5}),
    ],
)
def test_utility_tool_schemas_dispatch_well_formed_arguments(definition, arguments):
    calls: list[tuple[str, dict]] = []

    async def dispatch(name, received, _source):
        calls.append((name, received))
        return []

    application = McpApplication(
        [definition()],
        dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=pytest.fail,
    )
    asyncio.run(application.call_tool(definition()["name"], arguments))

    assert calls == [(definition()["name"], arguments)]
