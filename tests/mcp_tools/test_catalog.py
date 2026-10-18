"""Catalog ownership, exposure and strict runtime-derived guidance."""

import os
from unittest.mock import Mock

import pytest

from booley.mcp import catalog, server
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
        "BOOLEY_COVERAGE_CAMPAIGN": "campaign",
        "BOOLEY_COVERAGE_PROJECT": "/project",
    },
)
EXPOSURE_KEYS = set().union(*MODES)


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    getattr(catalog.work_dir_guidance, "cache_clear", lambda: None)()
    for key in EXPOSURE_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    getattr(catalog.work_dir_guidance, "cache_clear", lambda: None)()


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
        definitions = owner(exposure, [*endpoints, custom])
        assert definitions[-1] is custom
        assert definitions == sorted(definitions, key=lambda tool: tool["name"])
        assert not any(tool.get("is_goal") for tool in definitions)
        tools = server._build_mcp_application(
            [*endpoints, custom], [], server._McpLifetime(None, None)
        ).list_tools()
        assert [(t.name, t.input_schema) for t in tools] == [
            (d["name"], d["schema"]) for d in definitions
        ]


def test_default_inventory_matches_actual_interactive_tools_list(monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    before = dict(os.environ)
    definitions = catalog._default_interactive_definitions()
    accepting, nonaccepting = catalog.partition_work_dir(definitions)
    assert {d["name"] for d in nonaccepting} == {
        "booley_cancel",
        "booley_poll",
        "booley_status",
        "bwave",
    }
    assert accepting and dict(os.environ) == before
    endpoints, errors = catalog.discover_builtin_definitions(
        discover_mcp_tools(), catalog.Exposure()
    )
    tools = server._build_mcp_application(
        endpoints, errors, server._McpLifetime(None, None)
    ).list_tools()
    assert {d["name"]: d["schema"] for d in definitions} == {t.name: t.input_schema for t in tools}


def test_guidance_discovery_failure_raises(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "_default_interactive_definitions",
        Mock(side_effect=RuntimeError("schema scan unavailable")),
    )
    with pytest.raises(RuntimeError, match="schema scan unavailable"):
        catalog.work_dir_guidance()


def test_strict_discovery_preserves_errors_and_schema_invariant(monkeypatch):
    monkeypatch.setattr(catalog, "_import_and_extract", lambda *a, **kw: (None, "missing schema"))
    with pytest.raises(
        RuntimeError, match="Interactive builtin discovery incomplete: missing schema"
    ):
        catalog._default_interactive_definitions()
    with pytest.raises(ValueError, match="new_endpoint lacks work_dir"):
        catalog.partition_work_dir(
            [{"name": "new_endpoint", "module": "new_endpoint", "schema": {}}]
        )


def test_server_uses_catalog_owner():
    assert not hasattr(server, "_all_mcp_tool_defs"), "server retains catalog forwarders"
