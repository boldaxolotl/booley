"""The Goal Mode rules text carries the red-first bug-fix guidance (issue #1097)."""

from __future__ import annotations

import os
import re
from copy import deepcopy

import pytest

from booley.goals.rules import goal_mode_rules
from booley.mcp import (
    catalog,
)
from booley.mcp.catalog import work_dir_guidance
from booley.mcp.registry import discover_mcp_tools


@pytest.fixture(autouse=True)
def isolated_guidance_cache():
    catalog.work_dir_guidance.cache_clear()
    yield
    catalog.work_dir_guidance.cache_clear()


def test_rules_tell_the_agent_to_reproduce_a_bug_red_first() -> None:
    rules = goal_mode_rules(routing_guidance=work_dir_guidance())
    assert "confirm the test fails" in rules
    assert "before changing code" in rules
    assert "for the reported bug, not for an unrelated build or setup error" in rules
    assert "If it already passes" in rules
    assert "tell the human in chat" in rules
    assert "`goal_propose_change`" in rules


def test_rules_do_not_claim_red_first_is_enforced() -> None:
    # Goal acceptance does not check for a red run yet, so the text must not say it does.
    rules = goal_mode_rules(routing_guidance=work_dir_guidance())
    assert "needs a recorded failing run" not in rules
    assert "fail -> pass" not in rules


def test_rules_distinguish_routed_calls_from_tools_without_work_dir() -> None:
    rules = goal_mode_rules(routing_guidance=work_dir_guidance())
    assert "every Booley call" not in rules
    assert "`booley_targets`" in rules
    assert "`booley_report`" in rules
    for name in (
        "booley_status",
        "booley_poll",
        "booley_cancel",
        "bwave",
    ):
        assert f"`{name}`" in rules
    assert "accept no `work_dir`" in rules
    assert "Project-defined endpoints" in rules


def test_work_dir_guidance_tracks_catalog_schema_changes(monkeypatch):
    definitions = [
        {"name": "utility_z", "schema": {"properties": {}}},
        {"name": "utility_a", "schema": {"properties": {"work_dir": {}}}},
        {
            "name": "configured_endpoint",
            "module": "endpoint",
            "schema": {"properties": {"work_dir": {}}},
        },
    ]
    monkeypatch.setattr(catalog, "_default_interactive_definitions", lambda: deepcopy(definitions))
    first = work_dir_guidance()
    assert "Goal tools, `utility_a`." in first
    assert "`utility_z` accept no `work_dir`" in first
    assert "configured_endpoint" not in first
    definitions[0]["schema"]["properties"]["work_dir"] = {}
    definitions[1]["schema"]["properties"].clear()
    catalog.work_dir_guidance.cache_clear()
    second = work_dir_guidance()
    assert "Goal tools, `utility_z`." in second
    assert "`utility_a` accept no `work_dir`" in second
    assert second == work_dir_guidance()


def test_work_dir_guidance_uses_default_interactive_catalog(monkeypatch):
    expected = work_dir_guidance()
    catalog.work_dir_guidance.cache_clear()
    for key, value in {
        "BOOLEY_MCP_MODE": "other",
        "BOOLEY_MCP_DEBUG_TOOLS": "1",
        "BOOLEY_NESTED_AGENT": "1",
        "BOOLEY_NESTED_MCP_TOOLS": "coverage_evidence",
        "BOOLEY_MCP_TOOLS": "lint",
        "BOOLEY_COVERAGE_CAMPAIGN": "campaign",
        "BOOLEY_COVERAGE_PROJECT": "/project",
    }.items():
        monkeypatch.setenv(key, value)
    before = dict(os.environ)
    assert work_dir_guidance() == expected
    assert dict(os.environ) == before


def test_guidance_does_not_enumerate_configurable_endpoints():
    endpoint_names = {info.name for info in discover_mcp_tools()}
    names = set(re.findall(r"`([a-z_]+)`", work_dir_guidance()))
    assert not endpoint_names.intersection(names)
    assert {"booley_report", "booley_targets"} <= names


def test_nonaccepting_configurable_endpoint_uses_safe_fallback(monkeypatch):
    definitions = [
        {"name": "configured_endpoint", "module": "endpoint", "schema": {"properties": {}}}
    ]
    with pytest.raises(ValueError, match="configured_endpoint lacks work_dir"):
        catalog.partition_work_dir(definitions)
    monkeypatch.setattr(catalog, "_default_interactive_definitions", lambda: definitions)
    with pytest.raises(ValueError, match="configured_endpoint lacks work_dir"):
        work_dir_guidance()
