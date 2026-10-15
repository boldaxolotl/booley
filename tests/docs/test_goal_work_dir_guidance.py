"""Goal routing instructions agree with every advertised built-in MCP schema."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.mcp import server
from booley.mcp.registry import discover_mcp_tools

REPO_ROOT = Path(__file__).resolve().parents[2]


def _builtin_routing_catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    return {
        tool.name: "work_dir" in tool.input_schema.get("properties", {})
        for tool in _interactive_tools(monkeypatch)
    }


def _qualified_catalog(monkeypatch: pytest.MonkeyPatch, mode: str) -> dict[str, bool]:
    _interactive_tools(monkeypatch)
    if mode == "diagnostic":
        monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")
    else:
        monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
        monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "coverage_evidence")
        monkeypatch.setenv("BOOLEY_COVERAGE_CAMPAIGN", "documentation-audit")
        monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", "/documentation-audit")
    endpoints, errors = server._discover_builtin_mcp_tools(discover_mcp_tools())
    assert not errors, errors
    tools = server._build_mcp_application(
        endpoints, errors, server._McpLifetime(None, None)
    ).list_tools()
    assert tools
    return {tool.name: "work_dir" in tool.input_schema.get("properties", {}) for tool in tools}


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
    interactive = _builtin_routing_catalog(monkeypatch)
    assert {
        name: accepts for name, accepts in documented.items() if name in interactive
    } == interactive
    for mode, name in (("diagnostic", "booley_sleep"), ("Analyst", "coverage_evidence")):
        assert name not in interactive
        assert documented[name] is False
        assert _qualified_catalog(monkeypatch, mode)[name] is False
    assert set(documented) - set(interactive) == {"booley_sleep", "coverage_evidence"}
    assert "only when diagnostic tools are enabled" in usage
    assert "only in the Coverage Analyst's nested server" in usage


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
        "qa/shared/GOAL-CHILD.md",
    ),
)
def test_concise_guidance_names_supported_utilities_and_exceptions(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = " ".join((REPO_ROOT / path).read_text(encoding="utf-8").split())
    catalog = _builtin_routing_catalog(monkeypatch)
    exclusions = re.findall(
        r"([^.]+) (?:accept no `work_dir`\.|have no `work_dir` argument)", text
    )
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


# Audited routing producers and handwritten copies; qualified mode inventories
# are verified separately above rather than exempting names globally.
ROUTING_DOCUMENTS = (
    "src/booley/data/skills/booley-goal/SKILL.md",
    "src/booley/data/cheatsheet.md",
    "docs/user/FLOW_REFERENCE.md",
    "qa/shared/GOAL-CHILD.md",
)
_IDENTIFIER = r"[a-z][a-z0-9_]*"


def _guidance_names(text: str) -> set[str]:
    """Extract tool positions independently of the registered names."""
    text = re.sub(r"`python3 [^`]+`", "", text)
    text = re.sub(r"```(?:python|bash|sh)\n.*?```", "", text, flags=re.DOTALL)
    text = " ".join(text.split())
    names = set()
    if text.startswith("| MCP tool |"):
        names.update(re.findall(rf"`({_IDENTIFIER})`", text))
    names.update(re.findall(rf"\b({_IDENTIFIER})\(", text))
    for pattern in (
        rf"Check `({_IDENTIFIER})`",
        rf"(?:approval through|Goal change through) `({_IDENTIFIER})`",
        rf"operation_id to ({_IDENTIFIER})\b",
        rf"MCP tool `({_IDENTIFIER})`",
        rf"(?:call|Check) `({_IDENTIFIER})`",
        rf"(?:Use the|through the) ({_IDENTIFIER}) tool",
        rf"`({_IDENTIFIER})` tool exposes",
        rf"(?:sees only|expose only) `({_IDENTIFIER})`",
        rf"Discovery must expose exactly ({_IDENTIFIER})",
        rf"mcp__booley__({_IDENTIFIER})\b",
    ):
        names.update(re.findall(pattern, text))
    for pattern in (
        r"Available MCP tools: ([^.]+)",
        r"long-running endpoint \(([^)]+)\)",
    ):
        for inventory in re.findall(pattern, text):
            names.update(re.findall(rf"\b({_IDENTIFIER})\b", inventory))
    for pattern in (r"(?:usual set is|servers list) ([^;\n]+)",):
        for inventory in re.findall(pattern, text):
            names.update(re.findall(rf"`({_IDENTIFIER})`", inventory.split(" without ")[0]))
    for inventory in re.findall(
        r"([^.]+?) (?:accept no `?work_dir`?|have no `work_dir` argument)", text
    ):
        if "`" in inventory:
            names.update(re.findall(rf"`({_IDENTIFIER})`", inventory))
        else:
            names.update(re.findall(rf"\b({_IDENTIFIER})\b", inventory))
            names.difference_update({"and", "WARNING", "Pass", "work_dir"})
    names.update(
        re.findall(rf"`({_IDENTIFIER})`", text.split("Pass `work_dir`")[-1].split(". ")[0])
        if "Pass `work_dir`" in text
        else ()
    )
    # Strip wire prefixes after collecting whole invocation identifiers.
    return {name.removeprefix("mcp__booley__") for name in names}


def _assert_guidance_names(text: str, available: set[str], source: str) -> None:
    unavailable = _guidance_names(text) - available
    assert not unavailable, f"{source}: unavailable Interactive MCP tools: {sorted(unavailable)}"


def _interactive_tools(monkeypatch: pytest.MonkeyPatch):
    for key in (
        "BOOLEY_NESTED_AGENT",
        "BOOLEY_NESTED_MCP_TOOLS",
        "BOOLEY_MCP_TOOLS",
        "BOOLEY_COVERAGE_CAMPAIGN",
        "BOOLEY_COVERAGE_PROJECT",
        "BOOLEY_MCP_DEBUG_TOOLS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    endpoints, errors = server._discover_builtin_mcp_tools(discover_mcp_tools())
    assert not errors, errors
    tools = server._build_mcp_application(
        endpoints, errors, server._McpLifetime(None, None)
    ).list_tools()
    assert tools
    return tools


def test_agent_facing_guidance_names_only_interactive_tools(monkeypatch, capsys):
    from booley.goals import warnings
    from booley.goals.binding import GoalBindingError
    from booley.goals.rules import goal_mode_rules
    from booley.harness import booley as cli
    from booley.mcp.call_context import require_goal_work_dir

    available = {tool.name for tool in _interactive_tools(monkeypatch)}
    record = SimpleNamespace(worktree_path="/audit/worktree")
    monkeypatch.setattr(warnings, "protected_warnings", lambda *args: ())
    warning = warnings.goal_warnings(record, Path("/audit"), work_dir=None)
    store = SimpleNamespace(list_active=lambda: SimpleNamespace(corrupt=(), records=(record,)))
    with pytest.raises(GoalBindingError) as refusal:
        require_goal_work_dir({}, store)
    sources = {"Goal rules": goal_mode_rules(), "warning": warning, "refusal": str(refusal.value)}
    sources.update({path: (REPO_ROOT / path).read_text() for path in ROUTING_DOCUMENTS})
    for flag in ("--goals", "--all"):
        assert cli._cmd_cheat(cli._build_parser().parse_args(["cheat", flag]), None) == 0
        sources[f"booley cheat {flag}"] = capsys.readouterr().out
    failures = []
    for source, text in sources.items():
        try:
            _assert_guidance_names(text, available, source)
        except AssertionError as exc:
            failures.append(str(exc))
    assert not failures, "unavailable Interactive MCP tools: " + "; ".join(failures)


@pytest.mark.parametrize("name", ["booley_status", "not_a_registered_tool"])
@pytest.mark.parametrize(
    "template",
    [
        "| MCP tool | Description | `{}` | example |",
        "MCP tool `{}`",
        "call `{}`",
        "Use the {} tool",
        "through the {} tool",
        "`{}` tool exposes",
        "sees only `{}`",
        "expose only `{}`",
        "Discovery must expose exactly {}",
        "Available MCP tools: {}.",
        "long-running endpoint ({})",
        "usual set is `{}`;",
        "servers list `{}` without a rollout switch",
        "`{}()`",
        "mcp__booley__{}()",
        "Check `{}`",
        "approval through `{}`",
        "Goal change through `{}`",
        "operation_id to {}",
        "`{}` accept no `work_dir`.",
    ],
)
def test_guidance_guard_rejects_unknown_tool_name(name, template):
    text = (
        template.format(name)
        + " Pass record_id and a caller-stable operation_id. tests.toml .core"
    )
    if name == "booley_status":
        _assert_guidance_names(text, {name}, "fixture")
    else:
        with pytest.raises(AssertionError, match="not_a_registered_tool"):
            _assert_guidance_names(text, {"booley_status"}, "fixture")


def test_guidance_guard_ignores_parameters_and_english():
    text = (
        "to run; to meet a Goal; to recover its saved result; to finish; to Booley Flows. "
        "Pass record_id and a caller-stable operation_id. work_dir (absolute root). "
        "`.core` `tests.toml` `flow_options.booley.doctor`"
    )
    assert not _guidance_names(text)


# Each entry identifies an audited source and a bounded guidance region. CLI,
# configuration and proposed custom-endpoint examples outside it are not MCP claims.
GUIDANCE_REGIONS = (
    (
        "src/booley/data/skills/booley-setup/AGENTS_TEMPLATE.md",
        "- Inside the Sandbox,",
        "- If `booley_status`",
        "interactive",
    ),
    (
        "src/booley/data/skills/booley-setup/AGENTS_TEMPLATE.md",
        "- Interactive Mode:",
        "\n",
        "interactive",
    ),
    ("docs/internals/ARCHITECTURE.md", "## Goal Mode", "\n## ", "interactive"),
    ("docs/internals/MCP-TOOLS.md", "- Interactive, non-nested servers list", "\n", "interactive"),
    (
        "docs/internals/MCP-TOOLS.md",
        "The agent selects the arguments and invokes",
        "\n\n",
        "interactive",
    ),
    (
        "docs/internals/MCP-TOOLS.md",
        "The model prompt contains only a compact Campaign reference.",
        "\nChanging filters",
        "Analyst",
    ),
    ("qa/missions/coverage/MISSION.md", "### 8. cov-analyst", "### 9.", "Analyst"),
    ("qa/shared/coverage/RUNBOOK.md", "Set QA_PROVIDER_ROOT", "Candidate cases", "Analyst"),
)


def _region(path, start, end):
    text = (REPO_ROOT / path).read_text()
    assert start in text, f"audit anchor disappeared: {path}: {start}"
    return text.split(start, 1)[1].split(end, 1)[0]


def test_audited_guidance_regions_match_their_actual_mode(monkeypatch):
    modes = {
        "interactive": set(_builtin_routing_catalog(monkeypatch)),
        "Analyst": set(_qualified_catalog(monkeypatch, "Analyst")),
    }
    for path, start, end, mode in GUIDANCE_REGIONS:
        _assert_guidance_names(start + _region(path, start, end), modes[mode], f"{path}: {start}")


def test_generated_status_and_descriptions_name_available_tools(monkeypatch):
    tools = _interactive_tools(monkeypatch)
    names = {tool.name for tool in tools}
    monkeypatch.setattr(server, "_interactive_session_id", lambda: "guidance-audit")
    _assert_guidance_names(server._format_status_card(sorted(names)), names, "status card")
    for tool in tools:
        _assert_guidance_names(tool.description, names, f"description: {tool.name}")


def test_nested_analyst_prompt_names_match_bound_catalog(monkeypatch, tmp_path):
    from booley.specialists.coverage_analysis import _analysis_prompt
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist

    names = set(_qualified_catalog(monkeypatch, "Analyst"))
    prompt = _analysis_prompt(SimpleNamespace(model_campaign={}), None, "fixture")
    endpoint = CoverageAnalystSpecialist()
    endpoint.parse_args(["--campaign", "coverage.json", "--work-dir", str(tmp_path)])
    params = endpoint._coverage_agent_params(
        "fixture", tmp_path, tmp_path / "coverage.json", tmp_path / "audit.json"
    )
    assert set(params.nested_mcp_tools) == names
    _assert_guidance_names(prompt, names, "_analysis_prompt: bound Analyst")
    _assert_guidance_names(params.system_prompt, names, "_coverage_agent_params: bound Analyst")


@pytest.mark.parametrize(
    "mode,name", [("diagnostic", "booley_sleep"), ("Analyst", "coverage_evidence")]
)
def test_exceptional_names_require_their_documented_mode(monkeypatch, mode, name):
    interactive = set(_builtin_routing_catalog(monkeypatch))
    with pytest.raises(AssertionError, match=name):
        _assert_guidance_names(f"MCP tool `{name}`", interactive, "unqualified")
    qualified = set(_qualified_catalog(monkeypatch, mode))
    _assert_guidance_names(f"MCP tool `{name}`", qualified, mode)
