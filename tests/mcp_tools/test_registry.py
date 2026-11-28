"""Tests for the endpoint registry module.

Covers discovery, AST parsing, config filtering, skip-list,
and edge cases (empty dirs, syntax errors, missing attributes).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from booley.mcp import catalog
from booley.mcp.registry import (
    BUILTIN_FLOW_PACKAGES,
    SKIP_MODULES,
    McpToolInfo,
    discover_mcp_inventory,
    discover_mcp_tools,
    extract_mcp_tool_info,
)


def _write_endpoint_file(directory: Path, filename: str, content: str) -> Path:
    """Write a Python file into *directory* and return its path."""
    p = directory / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _endpoint_source(base: str, name: str) -> str:
    """Source of a one-class endpoint module whose ``name`` is the expression *name*."""
    return f"class Endpoint({base}):\n    name = {name}\n    description = 'Described'\n"


VALID_MCP_TOOL_SRC = """\
    from booley.mcp.base import McpTool

    class RunSim(McpTool):
        name = "run_sim"
        description = "Run an RTL simulation"
        code_modifying = False
"""

VALID_MCP_TOOL_CODE_MODIFYING = VALID_MCP_TOOL_SRC.replace("False", "True").replace(
    "run_sim", "apply_patch"
)

FLOW_SRC = """\
    from booley.flows.base import BooleyFlow

    class LintCheck(BooleyFlow):
        name = "lint_check"
        description = "Run linter"
"""

SPECIALIST_SRC = """\
    from booley.specialists.specialist import Specialist

    class DebugAgent(Specialist):
        name = "debug_agent"
        description = "Autonomous debug agent"
"""

NO_ENDPOINT_CLASS_SRC = """\
    class Helper:
        pass
"""

SYNTAX_ERROR_SRC = """\
    def broken(
        # missing closing paren
"""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (VALID_MCP_TOOL_SRC, ("run_sim", "mcp_tool", False)),
        (VALID_MCP_TOOL_CODE_MODIFYING, ("apply_patch", "mcp_tool", True)),
        (FLOW_SRC, ("lint_check", "flow", False)),
        (SPECIALIST_SRC, ("debug_agent", "specialist", False)),
        (_endpoint_source("module.McpTool", '"fancy"'), ("fancy", "mcp_tool", False)),
        (_endpoint_source("BuiltinFlow[LintRequest]", '"lint"'), ("lint", "flow", False)),
        (_endpoint_source("base.BuiltinFlow[LintRequest]", '"lint"'), ("lint", "flow", False)),
        (
            VALID_MCP_TOOL_SRC + VALID_MCP_TOOL_SRC.replace("run_sim", "second"),
            ("run_sim", "mcp_tool", False),  # the first matching class wins
        ),
        (_endpoint_source("McpTool", "prefix + 'x'"), None),  # a non-constant name
        (NO_ENDPOINT_CLASS_SRC, None),
        (VALID_MCP_TOOL_SRC.replace('    name = "run_sim"\n', ""), None),
        (VALID_MCP_TOOL_SRC.replace('    description = "Run an RTL simulation"\n', ""), None),
        (SYNTAX_ERROR_SRC, None),
    ],
    ids=[
        "tool",
        "code-modifying",
        "flow",
        "specialist",
        "attribute-base",
        "typed-builtin",
        "typed-builtin-attribute",
        "first-class-wins",
        "non-constant-name",
        "no-endpoint-class",
        "missing-name",
        "missing-description",
        "syntax-error",
    ],
)
def test_extract_mcp_tool_info_reads_metadata_without_import(tmp_path, source, expected):
    path = _write_endpoint_file(tmp_path, "endpoint.py", source)

    info = extract_mcp_tool_info(path, builtin=False)

    assert (info and (info.name, info.kind, info.code_modifying)) == expected
    if info is not None:
        assert info.path == str(path)
        assert extract_mcp_tool_info(path, builtin=True, package="mcp").path == "mcp/endpoint.py"


def test_extract_mcp_tool_info_ignores_a_missing_file(tmp_path):
    assert extract_mcp_tool_info(tmp_path / "ghost.py", builtin=True) is None


def test_inventory_scans_builtin_flows_tools_and_custom_directory(tmp_path):
    src = tmp_path / "src"
    for package in BUILTIN_FLOW_PACKAGES:
        (src / "flows" / package).mkdir(parents=True)
        _write_endpoint_file(
            src / "flows" / package, "flow.py", FLOW_SRC.replace("lint_check", package)
        )
    (src / "flows" / "surprise").mkdir()
    _write_endpoint_file(src / "flows" / "surprise", "flow.py", FLOW_SRC)  # not in the manifest
    (src / "mcp").mkdir()
    _write_endpoint_file(src / "mcp", "_internal.py", VALID_MCP_TOOL_SRC)
    for module in SKIP_MODULES:
        _write_endpoint_file(src / "mcp", f"{module}.py", VALID_MCP_TOOL_SRC)
    _write_endpoint_file(src / "mcp", "helper.py", NO_ENDPOINT_CLASS_SRC)
    _write_endpoint_file(src / "mcp", "broken.py", SYNTAX_ERROR_SRC)
    _write_endpoint_file(src / "mcp", "z_sim.py", VALID_MCP_TOOL_SRC)
    _write_endpoint_file(
        src / "mcp", "a_sim.py", VALID_MCP_TOOL_SRC.replace("run_sim", "alpha_sim")
    )
    custom = tmp_path / "custom"
    custom.mkdir()
    custom_file = _write_endpoint_file(custom, "mine.py", SPECIALIST_SRC)

    inventory = discover_mcp_inventory(booley_src=src, project_mcp_tools_dir=custom)

    assert [(item.name, item.path) for item in inventory] == [
        *((package, f"flows/{package}/flow.py") for package in BUILTIN_FLOW_PACKAGES),
        ("alpha_sim", "mcp/a_sim.py"),
        ("run_sim", "mcp/z_sim.py"),
        ("debug_agent", str(custom_file)),
    ]
    assert discover_mcp_inventory(booley_src=tmp_path / "none") == []


@pytest.mark.parametrize(
    ("specialist_config", "flow_config", "expected"),
    [
        ({"debug_agent": {"enabled": False}}, {}, {"lint_check", "run_sim"}),
        ({"debug_agent": {"enabled": True}}, {}, {"debug_agent", "lint_check", "run_sim"}),
        ({}, {"unrelated": {"enabled": False}}, {"debug_agent", "lint_check", "run_sim"}),
        ({}, {"lint_check": {"enabled": False}}, {"debug_agent", "run_sim"}),
    ],
    ids=["disabled-specialist", "enabled-specialist", "unrelated-key", "disabled-flow"],
)
def test_discover_applies_specialist_and_flow_enablement(
    tmp_path, specialist_config, flow_config, expected
):
    (tmp_path / "mcp").mkdir()
    for filename, source in (
        ("debug.py", SPECIALIST_SRC),
        ("lint.py", FLOW_SRC),
        ("sim.py", VALID_MCP_TOOL_SRC),
    ):
        _write_endpoint_file(tmp_path / "mcp", filename, source)
    config = {"specialist_config": specialist_config, "flow_config": flow_config}

    names = {item.name for item in discover_mcp_tools(booley_src=tmp_path, **config)}

    assert names == expected


# Endpoint infrastructure entries kept defensively even though they carry no
# discoverable Endpoint subclass today (base defines Endpoint itself, etc.).
_INFRA_ENTRIES = frozenset({"__init__", "base", "specialist", "registry"})


def test_no_redundant_helper_entries():
    """SKIP_MODULES must not re-grow support-module entries.

    Both discovery paths gate on ``extract_mcp_tool_info()`` (import-free AST
    check), so a module with no Endpoint subclass is skipped automatically;
    listing it here would be dead weight that rots as modules are renamed.
    Every non-infrastructure entry must exist AND actually parse as a endpoint
    module (i.e. it is a deliberately *hidden* endpoint, the set's one job).
    """
    endpoint_dir = Path(__file__).resolve().parents[2] / "src" / "booley" / "specialists"
    for mod in SKIP_MODULES - _INFRA_ENTRIES:
        py_file = endpoint_dir / f"{mod}.py"
        assert py_file.is_file(), f"stale SKIP_MODULES entry: {mod} (no such module)"
        assert extract_mcp_tool_info(py_file, builtin=True) is not None, (
            f"redundant SKIP_MODULES entry: {mod} has no Endpoint subclass, so the "
            "extract_mcp_tool_info() gate already skips it; delete the entry"
        )


def test_endpoints_declare_and_carry_no_criterion_claims():
    """Neither McpToolInfo nor a built-in endpoint class holds a Criteria catalog claim."""
    import dataclasses

    from booley.flows.fpga.flow import FpgaImplFlow
    from booley.flows.lint.flow import LintFlow
    from booley.flows.mutation.flow import MutationFlow
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.specialists.reviewer import ReviewerSpecialist

    names = {field.name for field in dataclasses.fields(McpToolInfo)}
    assert not names & {"satisfies", "satisfies_args"}
    for info in discover_mcp_tools():
        assert not hasattr(info, "satisfies")
        assert not hasattr(info, "satisfies_args")
    for cls in (
        LintFlow,
        MutationFlow,
        FpgaImplFlow,
        SimulateFlow,
        AsicSynthesizeFlow,
        ReviewerSpecialist,
    ):
        assert not hasattr(cls, "satisfies")
        assert not hasattr(cls, "satisfies_args")


def test_mutation_is_a_builtin_flow():
    assert "mutation" in BUILTIN_FLOW_PACKAGES
    info = next(item for item in discover_mcp_tools() if item.name == "mutation")
    assert info.kind == "flow"
    assert info.path == "flows/mutation/flow.py"


def test_no_mutation_tester_endpoint():
    """The Specialist is gone with no alias: neither a Flow nor a Specialist has its name."""
    assert "mutation_tester" not in {item.name for item in discover_mcp_tools()}


def test_interactive_catalog_offers_no_goal_tool(monkeypatch):
    for name in ("BOOLEY_NESTED_AGENT", "BOOLEY_MCP_TOOLS", "BOOLEY_COVERAGE_RECORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")

    builtins = [{"name": "sim", "description": "Run simulation", "schema": {"type": "object"}}]
    names = {
        definition["name"]
        for definition in catalog.visible_definitions(
            catalog.Exposure.from_mapping(os.environ), builtins
        )
    }
    assert not {name for name in names if name.startswith("goal_")}


@pytest.mark.parametrize("disabled", [None, "flow", "specialist", "mcp_tool"])
def test_unfiltered_inventory_composes_existing_discovery_policy(disabled):
    from booley.mcp.registry import discover_mcp_inventory, discover_mcp_tools, filter_mcp_tools

    inventory = discover_mcp_inventory()
    flows = {
        item.name: {"enabled": False} for item in inventory if item.kind == disabled == "flow"
    }
    specialists = {
        item.name: {"enabled": False}
        for item in inventory
        if item.kind == disabled == "specialist"
    }
    assert filter_mcp_tools(
        inventory, specialist_config=specialists, flow_config=flows
    ) == discover_mcp_tools(specialist_config=specialists, flow_config=flows)
    assert filter_mcp_tools(inventory) == inventory


def test_unfiltered_inventory_validates_disabled_specialist_names():
    from booley.mcp.registry import discover_mcp_inventory, filter_mcp_tools

    with pytest.raises(ValueError, match="unknown_specialist"):
        filter_mcp_tools(
            discover_mcp_inventory(), specialist_config={"unknown_specialist": {"enabled": False}}
        )


def test_cached_inventory_preserves_custom_order_exclusions_and_duplicate_metadata(tmp_path):
    from booley.mcp.registry import discover_mcp_inventory

    builtin = tmp_path / "builtin"
    (builtin / "mcp").mkdir(parents=True)
    custom = tmp_path / "custom"
    custom.mkdir()
    _write_endpoint_file(builtin / "mcp", "direct.py", VALID_MCP_TOOL_SRC)
    for filename, content in (
        ("a_flow.py", FLOW_SRC),
        ("b_specialist.py", SPECIALIST_SRC),
        ("c_direct.py", VALID_MCP_TOOL_SRC),
        ("_disabled.py", VALID_MCP_TOOL_CODE_MODIFYING),
        ("base.py", VALID_MCP_TOOL_CODE_MODIFYING),
        ("helper.py", NO_ENDPOINT_CLASS_SRC),
    ):
        _write_endpoint_file(custom, filename, content)
    inventory = discover_mcp_inventory(booley_src=builtin, project_mcp_tools_dir=custom)
    assert [item.name for item in inventory] == ["run_sim", "lint_check", "debug_agent", "run_sim"]
    config = {
        "specialist_config": {"debug_agent": {"enabled": False}},
        "flow_config": {"lint_check": {"enabled": False}},
    }
    expected = discover_mcp_tools(booley_src=builtin, project_mcp_tools_dir=custom, **config)
    assert discover_mcp_tools(inventory=inventory, **config) == expected
    assert [item.name for item in expected] == ["run_sim", "run_sim"]
    assert discover_mcp_tools(inventory=[]) == []


def test_schema_worker_excludes_job_report_and_transport_owners(tmp_path):
    from booley.mcp.catalog_worker import run_schema_worker

    path = tmp_path / "local.py"
    path.write_text(
        "import sys\n"
        "for module in ('booley.mcp.server', 'booley.mcp.job_lifecycle', "
        "'booley.mcp.job_results', 'booley.mcp.report_encoding'):\n"
        "    assert module not in sys.modules, module\n"
        "from booley.mcp.base import McpTool\n"
        "class Local(McpTool):\n"
        "    name = 'local'\n"
        "    description = 'Neutral worker guard'\n"
        "    def _add_args(self, parser): pass\n"
        "    def _run(self, args): pass\n"
    )
    definitions, errors = run_schema_worker({"local": path})
    assert not errors
    assert definitions[0]["name"] == "local"
