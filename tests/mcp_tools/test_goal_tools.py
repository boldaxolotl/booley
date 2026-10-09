"""The Goal Mode MCP tools: schema, Interactive visibility, dispatch (ADR 0067 D13)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from booley.core.project_dir import reset_cache
from booley.goals.model import goal_arg_json_schema
from booley.mcp import goal_tools
from booley.mcp import server as mcp_server
from booley.mcp.application import UnknownMcpToolError

GOAL_TOOLS = {"goal_enter", "goal_finish", "goal_propose_change", "goal_status"}


@pytest.fixture(autouse=True)
def _plain_server_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An MCP server process with no mode, allowlist, set."""
    for name in (
        "BOOLEY_NESTED_AGENT",
        "BOOLEY_MCP_TOOLS",
        "BOOLEY_MCP_MODE",
        "BOOLEY_COVERAGE_CAMPAIGN",
    ):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    monkeypatch.setattr(mcp_server, "_bwave_mcp_tools_for_mode", lambda: [])
    reset_cache()
    yield
    reset_cache()


def _listed() -> set[str]:
    return {definition["name"] for definition in mcp_server._all_mcp_tool_defs([])}


def _application():
    return mcp_server._build_mcp_application([], [], mcp_server._McpLifetime(None, None))


def _call(name: str, arguments: dict[str, Any]):
    return asyncio.run(_application().call_tool(name, arguments))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_goal_enter_schema_reuses_the_goal_argument_vocabulary() -> None:
    schema = goal_tools.goal_enter_schema()

    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["goals"]["items"] == goal_arg_json_schema()
    assert schema["required"] == ["work_dir", "slug", "goals"]


@pytest.mark.parametrize(
    ("arguments", "valid"),
    [
        ({"work_dir": "/w", "slug": "fix", "goals": [{"family": "lint", "target": "t"}]}, True),
        ({"slug": "fix", "goals": [{"family": "lint", "target": "t"}]}, False),
        ({"work_dir": "/w", "slug": "Fix!", "goals": [{"family": "lint", "target": "t"}]}, False),
        ({"work_dir": "/w", "slug": "fix", "goals": []}, False),
        ({"work_dir": "/w", "slug": "fix", "goals": [{"family": "nope", "target": "t"}]}, False),
    ],
)
def test_goal_enter_schema_accepts_and_rejects(arguments: dict[str, Any], valid: bool) -> None:
    validator = Draft202012Validator(goal_tools.goal_enter_schema())

    assert validator.is_valid(arguments) is valid


# ---------------------------------------------------------------------------
# Visibility (D13)
# ---------------------------------------------------------------------------


def test_goal_tools_hidden_outside_interactive_mode(monkeypatch: pytest.MonkeyPatch) -> None:

    assert not _listed() & GOAL_TOOLS


def test_goal_tools_hidden_from_nested_specialist_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
    monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "goal_enter")

    assert not _listed() & GOAL_TOOLS


def test_goal_tools_listed_in_an_interactive_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")

    assert _listed() >= GOAL_TOOLS


# ---------------------------------------------------------------------------
# Direct calls
# ---------------------------------------------------------------------------

_ENTER = {"work_dir": "/nowhere", "slug": "fix", "goals": [{"family": "lint", "target": "t"}]}


@pytest.mark.parametrize("name", sorted(GOAL_TOOLS))
def test_calling_a_goal_tool_outside_interactive_mode_is_unknown(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:

    with pytest.raises(UnknownMcpToolError):
        _call(name, _ENTER if name == "goal_enter" else {"work_dir": "/nowhere"})


def test_dispatch_refuses_a_hidden_goal_tool_even_if_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The special-tool router re-checks visibility, not only the catalog."""
    monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)

    result = asyncio.run(
        mcp_server._dispatch_special_mcp_tool("goal_enter", dict(_ENTER), None, [])  # type: ignore[arg-type]
    )

    assert result is None


@pytest.mark.parametrize("name", ["goal_finish"])
def test_finish_requires_explicit_retry_binding(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")

    payload = _call(name, {"work_dir": "/nowhere"})

    assert payload.is_error is True
    assert "record_id" in payload.content[0].text


def test_goal_enter_without_work_dir_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")

    payload = _call("goal_enter", {"slug": "fix", "goals": [{"family": "lint", "target": "t"}]})

    assert payload.is_error is True
    assert "work_dir" in payload.content[0].text


def test_goal_enter_refusal_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")

    payload = _call("goal_enter", _ENTER)

    assert payload.is_error is True
    assert payload.content[0].text.startswith("ERROR: Goal Mode was not entered:")
