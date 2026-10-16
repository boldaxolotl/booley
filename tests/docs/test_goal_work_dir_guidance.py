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
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
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


def _inventory_names(text: str) -> set[str]:
    """Read designated MCP-name columns in Markdown and terminal inventories."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    headings = r"(?:MCP tool|Step/tool|Booley Flow|Specialist)"
    names = set()
    markdown = rf"(?m)^\| {headings} \|[^\n]*\n\|[-: |]+\n((?:\|[^\n]*(?:\n|$))+)"
    terminal = rf"(?m)^  {headings} {{3,}}[^\n]*\n  [─ ]+\n((?:  \S[^\n]*(?:\n|$))+)"
    for rows in re.findall(markdown, text):
        names.update(re.findall(rf"(?m)^\|\s*`({_IDENTIFIER})(?:\([^`]*\))?`\s*\|", rows))
    for rows in re.findall(terminal, text):
        names.update(re.findall(rf"(?m)^  ({_IDENTIFIER})(?:\([^)]*\))? {{3,}}", rows))
    return names


def _routing_list_names(text: str) -> set[str]:
    """Extract bounded advertised inventories, keeping parameters out of name slots."""
    names = set()
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
        re.findall(
            rf"`({_IDENTIFIER})`",
            text.rsplit("Pass `work_dir`", maxsplit=1)[-1].split(". ", maxsplit=1)[0],
        )
        if "Pass `work_dir`" in text
        else ()
    )
    # Utility names in the accepting list follow a capability category; commas
    # and conjunctions bound names without interpreting parameters or English.
    accepting = rf"Goal (?:tools|calls)((?:(?:, (?:and )?| and )`?{_IDENTIFIER}`?)+)"
    for inventory in re.findall(accepting, text):
        names.update(set(re.findall(_IDENTIFIER, inventory)) - {"and"})
    return names


def _guidance_names(text: str) -> set[str]:
    """Extract tool positions independently of the registered names."""
    text = re.sub(r"`python3 [^`]+`", "", text)
    text = re.sub(r"```(?:python|bash|sh)\n.*?```", "", text, flags=re.DOTALL)
    names = _inventory_names(text)
    text = " ".join(text.split())
    if text.startswith("| MCP tool |"):
        names.update(re.findall(rf"`({_IDENTIFIER})`", text))
    names.update(re.findall(rf"\b({_IDENTIFIER})\(", text))
    for pattern in (
        rf"\b(?i:call|check|use|MCP(?: tool)?|rerun command) `({_IDENTIFIER})`",
        rf"(?:name from|`missing` in|producer remedy in) `({_IDENTIFIER})`",
        rf"`({_IDENTIFIER})` (?:lists|with `work_dir`)",
        rf"(?:approval through|Goal change through) `({_IDENTIFIER})`",
        rf"operation_id to ({_IDENTIFIER})\b",
        rf"(?:Use the|through the) ({_IDENTIFIER}) tool",
        rf"`({_IDENTIFIER})` tool exposes",
        rf"(?:sees only|expose only) `({_IDENTIFIER})`",
        rf"Discovery must expose exactly ({_IDENTIFIER})",
        rf"mcp__booley__({_IDENTIFIER})\b",
    ):
        names.update(re.findall(pattern, text))
    names.update(_routing_list_names(text))
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
    sources.update(
        {path: (REPO_ROOT / path).read_text(encoding="utf-8") for path in ROUTING_DOCUMENTS}
    )
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
        "`.core` `tests.toml` `flow_options.booley.doctor` "
        "Bare `booley` prints help. `full` prints every testcase."
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
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert start in text, f"audit anchor disappeared: {path}: {start}"
    return text.split(start, 1)[1].split(end, 1)[0]


def test_audited_guidance_regions_match_their_actual_mode(monkeypatch):
    modes = {
        "interactive": set(_builtin_routing_catalog(monkeypatch)),
        "Analyst": set(_qualified_catalog(monkeypatch, "Analyst")),
    }
    original_open = Path.open

    def legacy_locale_open(path, *args, **kwargs):
        # Windows can default to cp1252: only an explicit UTF-8 document read
        # should override that locale when checking the real audited files.
        if path.suffix == ".md" and kwargs.get("encoding") in (None, "locale"):
            kwargs["encoding"] = "cp1252"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", legacy_locale_open)
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


@pytest.mark.parametrize("rendered", [False, True], ids=["markdown", "terminal"])
@pytest.mark.parametrize("name", ["goal_enter", "sim", "coverage_analyst"])
def test_guidance_guard_rejects_unknown_actual_inventory_cell(name, rendered, monkeypatch):
    from booley.harness.render_md import render

    text = (REPO_ROOT / "src/booley/data/cheatsheet.md").read_text(encoding="utf-8")
    available = set(_builtin_routing_catalog(monkeypatch))
    original = render(text) if rendered else text
    _assert_guidance_names(original, available, "original cheat inventory")
    changed = text.replace(f"| `{name}` |", "| `not_a_registered_tool` |", 1)
    assert changed != text
    changed = render(changed) if rendered else changed
    with pytest.raises(AssertionError, match="not_a_registered_tool"):
        _assert_guidance_names(changed, available, "mutated cheat inventory")


@pytest.mark.parametrize(
    "path, phrase",
    [
        pytest.param("src/booley/data/skills/booley-goal/SKILL.md", phrase, id=label)
        for label, phrase in (
            ("capital-call", "Call `goal_finish`"),
            ("lowercase-check", "check `goal_status`"),
            ("use", "Use `booley_targets`"),
            ("mcp-noun", "MCP `goal_status`"),
            ("name-from", "name from `goal_status`"),
            ("state-in", "`missing` in `goal_status`"),
            ("remedy-in", "producer remedy in `goal_status`"),
            ("rerun-command", "rerun command `goal_status`"),
            ("with-work-dir", "`goal_finish` with `work_dir`"),
        )
    ]
    + [
        pytest.param("src/booley/data/cheatsheet.md", "`goal_status` lists", id="lists"),
        *[
            pytest.param(path, "Goal tools, `booley_targets`", id=f"accepting-{index}")
            for index, path in enumerate(ROUTING_DOCUMENTS[:3])
        ],
        pytest.param("qa/shared/GOAL-CHILD.md", "Goal calls, `booley_targets`", id="accepting-qa"),
    ],
)
def test_guidance_guard_rejects_unknown_actual_prose_tool(path, phrase, monkeypatch):
    text = " ".join((REPO_ROOT / path).read_text(encoding="utf-8").split())
    available = set(_builtin_routing_catalog(monkeypatch))
    _assert_guidance_names(text, available, "original prose")
    assert phrase in text
    # These are MCP-name positions; parameter/config spans alongside them stay intact.
    names = re.findall(r"`([a-z_]+)`", phrase)
    name = names[0] if names[0] != "missing" else names[1]
    changed = text.replace(phrase, phrase.replace(f"`{name}`", "`not_a_registered_tool`"), 1)
    with pytest.raises(AssertionError, match="not_a_registered_tool"):
        _assert_guidance_names(changed, available, "mutated prose")


def test_guidance_guard_rejects_unknown_plain_accepting_utility():
    text = "Pass work_dir to Booley Flows, Specialists, Goal tools, not_a_registered_tool."
    with pytest.raises(AssertionError, match="not_a_registered_tool"):
        _assert_guidance_names(text, {"booley_targets"}, "plain routing producer")
