"""Goal routing instructions agree with every advertised built-in MCP schema."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from booley.mcp import server
from booley.mcp.registry import discover_mcp_tools

REPO_ROOT = Path(__file__).resolve().parents[2]


def _builtin_routing_catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Cover Interactive/debug and the bound Analyst's single-capability server."""
    for key in (
        "BOOLEY_NESTED_AGENT",
        "BOOLEY_NESTED_MCP_TOOLS",
        "BOOLEY_MCP_TOOLS",
        "BOOLEY_COVERAGE_CAMPAIGN",
        "BOOLEY_COVERAGE_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")
    catalog: dict[str, bool] = {}
    for nested in (False, True):
        if nested:
            monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
            monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "coverage_evidence")
            monkeypatch.setenv("BOOLEY_COVERAGE_CAMPAIGN", "documentation-audit")
            monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", "/documentation-audit")
        endpoints, errors = server._discover_builtin_mcp_tools(discover_mcp_tools())
        application = server._build_mcp_application(
            endpoints, errors, server._McpLifetime(None, None)
        )
        assert not errors, errors
        tools = application.list_tools()
        assert tools, "the routing audit must exercise advertised tools"
        for tool in tools:
            accepts = "work_dir" in tool.input_schema.get("properties", {})
            assert catalog.get(tool.name, accepts) == accepts
            catalog[tool.name] = accepts
    return catalog


def test_documented_work_dir_inventory_matches_registered_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = (REPO_ROOT / "docs/user/USAGE.md").read_text(encoding="utf-8")
    assert "Built-in MCP worktree routing:" in usage
    table = usage.split("Built-in MCP worktree routing:\n\n", maxsplit=1)[1].split("\n\n")[0]
    documented: dict[str, bool] = {}
    for row in table.splitlines()[2:]:
        cells = row.strip("|").split("|")
        assert len(cells) == 2, row
        for accepts, cell in zip((True, False), cells, strict=True):
            for name in re.findall(r"`([a-z_]+)`", cell):
                assert name not in documented, f"duplicate inventory entry: {name}"
                documented[name] = accepts
    assert documented == _builtin_routing_catalog(monkeypatch)


def test_current_instructions_never_require_work_dir_on_every_call() -> None:
    documents = [
        *(REPO_ROOT / "docs/user").rglob("*.md"),
        *(REPO_ROOT / "src/booley/data").rglob("*.md"),
        REPO_ROOT / "docs/internals/MCP-TOOLS.md",
        REPO_ROOT / "docs/internals/ARCHITECTURE.md",
    ]
    blanket = re.compile(r"work_dir[^.]{0,160}\bon every (?:Booley|MCP) call", re.IGNORECASE)
    incorrect = [
        str(path.relative_to(REPO_ROOT))
        for path in documents
        if blanket.search(" ".join(path.read_text(encoding="utf-8").split()))
    ]
    assert not incorrect, f"blanket work_dir instruction in: {incorrect}"


@pytest.mark.parametrize(
    "path",
    (
        "src/booley/data/skills/booley-goal/SKILL.md",
        "src/booley/data/cheatsheet.md",
        "docs/user/FLOW_REFERENCE.md",
    ),
)
def test_concise_guidance_names_supported_utilities_and_exceptions(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = " ".join((REPO_ROOT / path).read_text(encoding="utf-8").split())
    catalog = _builtin_routing_catalog(monkeypatch)
    exclusions = re.findall(r"([^.]+) accept no `work_dir`\.", text)
    assert len(exclusions) == 1, f"{path} must identify tools accepting no work_dir"
    assert set(re.findall(r"`([a-z_]+)`", exclusions[0])) == {
        name for name, accepts in catalog.items() if not accepts
    }
    for name in ("booley_targets", "booley_report"):
        assert f"`{name}`" in text, f"{path} must name {name} in its routing guidance"


@pytest.mark.parametrize(
    "path", ["docs/user/USAGE.md", "src/booley/data/skills/booley-goal/SKILL.md"]
)
def test_goal_evidence_instructions_exclude_cli(path):
    text = (REPO_ROOT / path).read_text()
    assert "CLI runs never record Goal evidence" in text
    assert "Goal-bound" in text and "MCP" in text and "`work_dir`" in text
