"""Audit routing inventories and live tool claims against their owning catalogs."""

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.rules import goal_mode_rules as rules
from booley.mcp import catalog
from booley.mcp import server as mcp

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTING_DOCUMENTS = (
    "src/booley/data/skills/booley-goal/SKILL.md",
    "src/booley/data/cheatsheet.md",
    "docs/user/FLOW-SIM.md",
    "qa/shared/GOAL-CHILD.md",
)


def _tools(monkeypatch, mode="interactive"):
    from tests.mcp_tools.test_catalog import EXPOSURE_KEYS, MODES

    from booley.mcp.registry import discover_mcp_tools

    for key in EXPOSURE_KEYS:
        monkeypatch.delenv(key, raising=False)
    values = MODES[{"interactive": 1, "diagnostic": 2, "Analyst": -1}[mode]]
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    endpoints, errors = catalog.discover_builtin_definitions(
        discover_mcp_tools(), catalog.Exposure.from_mapping(os.environ)
    )
    assert not errors
    return mcp._build_mcp_application(endpoints, errors, mcp._McpLifetime(None, None)).list_tools()


def _assert_guidance_names(text, available, source):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    if "`" not in text:
        text = re.sub(r"\b((?:booley_|goal_|coverage_)[a-z0-9_]+)\b", r"`\1`", text)
    names = set(re.findall(r"mcp__booley__([a-z][a-z0-9_]*)", text))
    names.update(re.findall(r"`((?:booley_|goal_|coverage_)[a-z0-9_]+)`", text))
    for pattern in (
        r"\b(?i:call|check|use|MCP(?: tool)?) `([a-z][a-z0-9_]*)`",
        r"operation_id to ([a-z][a-z0-9_]*)\b",
        r"`([a-z][a-z0-9_]*)\(",
    ):
        names.update(re.findall(pattern, text))
    for inventory in re.findall(r"Available MCP tools: ([^.]+)", text):
        names.update(re.findall(r"\b[a-z][a-z0-9_]*\b", inventory))
    headings = r"(?:MCP tool|Step/tool|Booley Flow|Specialist)"
    markdown = rf"(?m)^\| {headings} \|[^\n]*\n\|[-: |]+\n((?:\|[^\n]*(?:\n|$))+)"
    terminal = rf"(?m)^  {headings} {{3,}}[^\n]*\n  [─ ]+\n((?:  \S[^\n]*(?:\n|$))+)"
    for rows in re.findall(markdown, text):
        names.update(re.findall(r"(?m)^\|\s*`([a-z][a-z0-9_]*)(?:\([^`]*\))?`\s*\|", rows))
    for rows in re.findall(terminal, text):
        names.update(re.findall(r"(?m)^  ([a-z][a-z0-9_]*)(?:\([^)]*\))? {3,}", rows))
    assert not names - available, f"{source}: unavailable MCP tools: {sorted(names - available)}"


def test_documented_work_dir_inventory_matches_registered_schemas(monkeypatch):
    mcp_tools_doc = (REPO_ROOT / "docs/internals/MCP-TOOLS.md").read_text(encoding="utf-8")
    table = mcp_tools_doc.split("Built-in MCP worktree routing:\n\n", 1)[1].split("\n\n")[0]
    documented = {}
    for row in table.splitlines()[2:]:
        for accepts, cell in zip((True, False), row.strip("|").split("|"), strict=True):
            for name in re.findall(r"`([a-z_]+)`", cell):
                assert name not in documented
                documented[name] = accepts
    live = {
        t.name: "work_dir" in t.input_schema.get("properties", {}) for t in _tools(monkeypatch)
    }
    assert {name: value for name, value in documented.items() if name in live} == live
    assert set(documented) - set(live) == {"booley_sleep", "coverage_evidence"}
    for mode, name in (("diagnostic", "booley_sleep"), ("Analyst", "coverage_evidence")):
        assert documented[name] is False
        tool = next(t for t in _tools(monkeypatch, mode) if t.name == name)
        assert "work_dir" not in tool.input_schema["properties"]
        with pytest.raises(AssertionError, match=name):
            _assert_guidance_names(f"MCP tool `{name}`", set(live), "unqualified")
    assert "only when diagnostic tools are enabled" in mcp_tools_doc
    assert "only in the Coverage Analyst's nested server" in mcp_tools_doc


def test_current_instructions_never_require_work_dir_on_every_call():
    paths = [p for d in ("docs/user", "src/booley/data") for p in (REPO_ROOT / d).rglob("*.md")]
    paths += [REPO_ROOT / f"docs/internals/{name}.md" for name in ("MCP-TOOLS", "ARCHITECTURE")]
    for path in paths:
        text = " ".join(path.read_text(encoding="utf-8").split())
        blanket = r"work_dir[^.]{0,160}\bon every (?:Booley|MCP) call"
        assert not re.search(blanket, text, re.I), path


def test_agent_facing_guidance_names_only_interactive_tools(monkeypatch, capsys):
    from booley.goals import warnings
    from booley.goals.binding import GoalBindingError
    from booley.goals.warnings import goal_warnings
    from booley.harness import booley as cli
    from booley.mcp.call_context import require_goal_work_dir

    tools = _tools(monkeypatch)
    names = {t.name for t in tools}
    nonaccepting = {
        t.name for t in tools if "work_dir" not in t.input_schema.get("properties", {})
    }
    guidance = catalog.work_dir_guidance()
    record = SimpleNamespace(worktree_path="/audit/worktree")
    monkeypatch.setattr(warnings, "protected_warnings", lambda *args: ())
    warning = goal_warnings(record, Path("/audit"), work_dir=None, routing_guidance=guidance)
    store = SimpleNamespace(list_active=lambda: SimpleNamespace(corrupt=(), records=(record,)))
    with pytest.raises(GoalBindingError) as refusal:
        require_goal_work_dir({}, store)
    monkeypatch.setattr(mcp, "_interactive_session_id", lambda: "audit")
    sources = [rules(routing_guidance=guidance), mcp._format_status_card(sorted(names))]
    sources += [t.description for t in tools]
    sources += [warning, str(refusal.value)]
    for path in (*ROUTING_DOCUMENTS, "docs/user/GOAL-MODE.md"):
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        # FLOW-SIM.md ends with `[flows.sim]` settings, which name C++ `main()`, not tools.
        text = text.split("\n## Configuration", 1)[0]
        if path in ROUTING_DOCUMENTS:
            exclusions = re.findall(
                r"([^.]+) (?:accept no `work_dir`\.|have no `work_dir` argument)",
                " ".join(text.split()),
            )
            assert len(exclusions) == 1
            assert set(re.findall(r"`([a-z_]+)`", exclusions[0])) == nonaccepting
            assert all(f"`{name}`" in text for name in ("booley_targets", "booley_report"))
            sources.append(text)
        if path in (ROUTING_DOCUMENTS[0], "docs/user/GOAL-MODE.md"):
            assert "CLI runs never record Goal evidence" in text
            assert all(word in text for word in ("Goal-bound", "MCP", "`work_dir`"))
    for flag in ("--goals", "--all"):
        assert cli._cmd_cheat(cli._build_parser().parse_args(["cheat", flag]), None) == 0
        sources.append(capsys.readouterr().out)
    for text in sources:
        _assert_guidance_names(text, names, text[:80])


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


def test_audited_guidance_regions_match_their_actual_mode(monkeypatch):
    modes = {
        mode: {t.name for t in _tools(monkeypatch, mode)} for mode in ("interactive", "Analyst")
    }
    for path, start, end, mode in GUIDANCE_REGIONS:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert start in text, path
        region = start + text.split(start, 1)[1].split(end, 1)[0]
        if path == "qa/missions/coverage/MISSION.md":
            # Verdict enum values are not tool claims; retain the surrounding routing prose.
            a, b = region.index("Verdict classes:"), region.index("sim-fail still reports.")
            region = region[:a] + region[b:]
        _assert_guidance_names(region, modes[mode], path)


def test_nested_analyst_prompt_names_match_bound_catalog(monkeypatch, tmp_path):
    from booley.specialists.coverage_analysis import _analysis_prompt
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist

    names = {t.name for t in _tools(monkeypatch, "Analyst")}
    endpoint = CoverageAnalystSpecialist()
    endpoint.parse_args(["--campaign", "coverage.json", "--work-dir", str(tmp_path)])
    params = endpoint._coverage_agent_params(
        "fixture", tmp_path, tmp_path / "coverage.json", tmp_path / "audit.json"
    )
    assert set(params.nested_mcp_tools) == names
    prompt = _analysis_prompt(SimpleNamespace(model_campaign={}), None, "fixture")
    for text in (prompt, params.system_prompt):
        _assert_guidance_names(text, names, "bound Analyst")
