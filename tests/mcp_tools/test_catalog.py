"""Shared static schemas preserve tools/list and bounded advisory discovery."""

from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from booley.mcp import catalog, server
from booley.mcp.registry import discover_mcp_tools

EXPOSURE_KEYS = (
    "BOOLEY_MCP_MODE",
    "BOOLEY_MCP_DEBUG_TOOLS",
    "BOOLEY_MCP_TOOLS",
    "BOOLEY_NESTED_AGENT",
    "BOOLEY_NESTED_MCP_TOOLS",
    "BOOLEY_COVERAGE_CAMPAIGN",
    "BOOLEY_COVERAGE_PROJECT",
)


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    catalog.reset_guidance_cache()
    for key in EXPOSURE_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    catalog.reset_guidance_cache()


def _application_tools():
    endpoints, errors = server._discover_builtin_mcp_tools(discover_mcp_tools())
    assert not errors, errors
    return server._build_mcp_application(
        endpoints, errors, server._McpLifetime(None, None)
    ).list_tools()


def test_default_inventory_matches_actual_interactive_tools_list(monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    before = dict(os.environ)
    definitions = catalog._default_interactive_definitions()
    advertised = {tool.name: tool.input_schema for tool in _application_tools()}
    assert definitions and advertised
    assert {tool["name"]: tool["schema"] for tool in definitions} == advertised
    assert dict(os.environ) == before
    accepting, nonaccepting = catalog.partition_work_dir(definitions)
    assert {tool["name"] for tool in nonaccepting} == {
        "booley_cancel",
        "booley_poll",
        "booley_status",
        "bwave",
    }
    assert {tool["name"] for tool in accepting} == set(advertised) - {
        tool["name"] for tool in nonaccepting
    }


@pytest.mark.parametrize(
    "values, present, absent",
    [
        (
            {"BOOLEY_MCP_MODE": "interactive"},
            {"goal_enter", "booley_status", "sim", "bwave"},
            {"booley_sleep", "coverage_evidence"},
        ),
        (
            {"BOOLEY_MCP_MODE": "interactive", "BOOLEY_MCP_DEBUG_TOOLS": "1"},
            {"booley_sleep", "goal_enter"},
            {"coverage_evidence"},
        ),
        (
            {"BOOLEY_NESTED_AGENT": "1", "BOOLEY_NESTED_MCP_TOOLS": "sim"},
            {"sim", "booley_poll", "booley_cancel", "booley_targets"},
            {"goal_enter", "booley_status", "lint", "bwave"},
        ),
        (
            {"BOOLEY_MCP_TOOLS": "lint"},
            {"lint", "booley_poll", "booley_targets"},
            {"sim", "goal_enter", "booley_status", "bwave"},
        ),
        (
            {
                "BOOLEY_NESTED_AGENT": "1",
                "BOOLEY_NESTED_MCP_TOOLS": "coverage_evidence",
                "BOOLEY_COVERAGE_CAMPAIGN": "campaign",
                "BOOLEY_COVERAGE_PROJECT": "/project",
            },
            {"coverage_evidence"},
            {"booley_poll", "booley_targets", "goal_enter", "sim", "bwave"},
        ),
    ],
)
def test_schema_extraction_preserves_exposure(values, present, absent, monkeypatch):
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    actual = {tool.name for tool in _application_tools()}
    assert present <= actual
    assert not absent.intersection(actual)


def test_visibility_wrappers_read_environment_on_every_call(monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    assert server._status_mcp_tool_visible() and server._goal_tools_visible()
    monkeypatch.setenv("BOOLEY_MCP_MODE", "other")
    assert not server._status_mcp_tool_visible() and not server._goal_tools_visible()
    assert not server._sleep_mcp_tool_visible()
    monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")
    assert server._sleep_mcp_tool_visible()


def test_server_builder_hooks_are_late_bound_without_double_gating(monkeypatch):
    # Default mode hides status/Goals; patched wrappers still control assembly.
    monkeypatch.setattr(server, "_status_mcp_tool_visible", lambda: True)
    monkeypatch.setattr(server, "_goal_tools_visible", lambda: True)
    marker = {"name": "patched_bwave", "schema": {"type": "object", "properties": {}}}
    monkeypatch.setattr(server, "_bwave_mcp_tools_for_mode", lambda: [marker])
    names = {tool["name"] for tool in server._all_mcp_tool_defs([])}
    assert {"booley_status", "goal_enter", "patched_bwave"} <= names
    application_marker = {"name": "patched_all", "schema": {"type": "object", "properties": {}}}
    monkeypatch.setattr(server, "_all_mcp_tool_defs", lambda endpoints: [application_marker])
    tools = server._build_mcp_application([], [], server._McpLifetime(None, None)).list_tools()
    assert [tool.name for tool in tools] == ["patched_all"]


def test_catalog_cache_hit_does_not_discover_or_read_exposure(monkeypatch):
    discover = Mock(wraps=catalog._default_interactive_definitions)
    monkeypatch.setattr(catalog, "_default_interactive_definitions", discover)
    first = catalog.default_interactive_work_dir_inventory()
    assert first is not None
    monkeypatch.setattr(
        catalog, "_default_interactive_definitions", lambda: pytest.fail("cache rediscovered")
    )
    monkeypatch.setattr(
        catalog.Exposure, "from_mapping", lambda values: pytest.fail("guidance read exposure")
    )
    assert catalog.default_interactive_work_dir_inventory() is first
    assert discover.call_count == 1


def test_failed_discovery_and_fallback_are_cached(monkeypatch, caplog):
    discover = Mock(side_effect=RuntimeError("schema scan unavailable"))
    monkeypatch.setattr(catalog, "_default_interactive_definitions", discover)
    assert catalog.default_interactive_work_dir_inventory() is None
    assert catalog.default_interactive_work_dir_inventory() is None
    assert discover.call_count == 1
    assert caplog.text.count("Interactive MCP guidance unavailable;") == 1
    catalog.reset_guidance_cache()
    assert catalog.default_interactive_work_dir_inventory() is None
    assert discover.call_count == 2


def test_strict_discovery_preserves_errors_and_schema_invariant(monkeypatch):
    monkeypatch.setattr(
        catalog, "_import_and_extract", lambda *args, **kwargs: (None, "missing schema")
    )
    with pytest.raises(
        RuntimeError, match="Interactive builtin discovery incomplete: missing schema"
    ):
        catalog._default_interactive_definitions()
    definition = {"name": "new_endpoint", "module": "new_endpoint", "schema": {"properties": {}}}
    with pytest.raises(ValueError, match="new_endpoint lacks work_dir"):
        catalog.partition_work_dir([definition])


def test_assembler_is_ungated_and_preserves_dispatch_metadata():
    endpoint = {"name": "z", "module": "custom", "schema": {}, "custom_path": "/custom.py"}
    utility = {"name": "a", "schema": {}}
    before = deepcopy(endpoint)
    assert catalog.assemble_tool_definitions([endpoint], [utility]) == [utility, endpoint]
    assert endpoint == before


@pytest.mark.parametrize(
    "first,second",
    [("booley.goals.rules", "booley.mcp.server"), ("booley.mcp.server", "booley.goals.rules")],
)
def test_fresh_process_import_orders(first, second):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {first}; import {second}; from booley.goals.rules import goal_mode_rules; assert 'Pass `work_dir`' in goal_mode_rules()",
        ],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(Path(__file__).resolve().parents[2] / "src"),
                    os.environ.get("PYTHONPATH", ""),
                )
            ),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_goal_schema_import_failure_degrades_renderer_once(monkeypatch, caplog):
    import builtins

    from booley.goals.rules import work_dir_guidance

    original_import = builtins.__import__
    attempts = []

    def import_without_goal_schemas(name, *args, **kwargs):
        if name == "booley.mcp.goal_tools":
            attempts.append(name)
            raise ImportError("injected Goal schema import failure")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_goal_schemas)
    first = work_dir_guidance()
    assert "Utilities that accept no `work_dir` follow their advertised schemas" in first
    assert "While any Goal Record occupies" in first
    assert work_dir_guidance() == first
    assert len(attempts) == 1
    assert caplog.text.count("Interactive MCP guidance unavailable;") == 1
