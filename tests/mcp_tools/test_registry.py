"""Tests for the endpoint registry module.

Covers discovery, AST parsing, config filtering, skip-list,
and edge cases (empty dirs, syntax errors, missing attributes).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from booley.mcp.registry import (
    BUILTIN_FLOW_PACKAGES,
    SKIP_MODULES,
    McpToolInfo,
    _get_base_names,
    _get_constant_value,
    _scan_builtin_flows,
    _scan_directory,
    discover_mcp_tools,
    extract_mcp_tool_info,
)

# ---------------------------------------------------------------------------
# Helpers вЂ” create mock endpoint .py files inside tmp_path
# ---------------------------------------------------------------------------


def _write_endpoint_file(directory: Path, filename: str, content: str) -> Path:
    """Write a Python file into *directory* and return its path."""
    p = directory / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


VALID_MCP_TOOL_SRC = """\
    from booley.mcp.base import McpTool

    class RunSim(McpTool):
        name = "run_sim"
        description = "Run an RTL simulation"
        code_modifying = False

        def execute(self):
            pass
"""

VALID_MCP_TOOL_CODE_MODIFYING = """\
    from booley.mcp.base import McpTool

    class ApplyPatch(McpTool):
        name = "apply_patch"
        description = "Apply a code patch"
        code_modifying = True

        def execute(self):
            pass
"""

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

MISSING_NAME_SRC = """\
    from booley.mcp.base import McpTool

    class Broken(McpTool):
        description = "I have no name"
"""

MISSING_DESCRIPTION_SRC = """\
    from booley.mcp.base import McpTool

    class Broken(McpTool):
        name = "orphan"
"""

SYNTAX_ERROR_SRC = """\
    def broken(
        # missing closing paren
"""

ATTRIBUTE_BASE_SRC = """\
    import booley.mcp.base

    class Fancy(booley.mcp.base.McpTool):
        name = "fancy"
        description = "Uses attribute-style base"
"""

MULTIPLE_CLASSES_SRC = """\
    from booley.mcp.base import McpTool

    class First(McpTool):
        name = "first_endpoint"
        description = "First"

    class Second(McpTool):
        name = "second_endpoint"
        description = "Second"
"""


# ---------------------------------------------------------------------------
# McpToolInfo dataclass
# ---------------------------------------------------------------------------


class TestMcpToolInfo:
    def test_frozen(self):
        ti = McpToolInfo(name="x", path="x.py", description="d")
        with pytest.raises(AttributeError):
            ti.name = "y"  # type: ignore[misc]

    def test_defaults(self):
        ti = McpToolInfo(name="x", path="x.py", description="d")
        assert ti.code_modifying is False

    def test_code_modifying_flag(self):
        ti = McpToolInfo(name="x", path="x.py", description="d", code_modifying=True)
        assert ti.code_modifying is True


# ---------------------------------------------------------------------------
# _get_constant_value
# ---------------------------------------------------------------------------


class TestGetConstantValue:
    def test_string(self):
        import ast

        node = ast.parse('"hello"', mode="eval").body
        assert _get_constant_value(node) == "hello"

    def test_bool(self):
        import ast

        node = ast.parse("True", mode="eval").body
        assert _get_constant_value(node) is True

    def test_non_constant_returns_none(self):
        import ast

        node = ast.parse("x + 1", mode="eval").body
        assert _get_constant_value(node) is None


# ---------------------------------------------------------------------------
# _get_base_names
# ---------------------------------------------------------------------------


class TestGetBaseNames:
    def _parse_class(self, src: str):
        import ast

        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                return node
        raise ValueError("no class found")

    def test_simple_name(self):
        cls = self._parse_class("class Foo(Endpoint): pass")
        assert _get_base_names(cls) == {"Endpoint"}

    def test_attribute_name(self):
        cls = self._parse_class("class Foo(module.Endpoint): pass")
        assert _get_base_names(cls) == {"Endpoint"}

    def test_multiple_bases(self):
        cls = self._parse_class("class Foo(Endpoint, BooleyFlow): pass")
        assert _get_base_names(cls) == {"Endpoint", "BooleyFlow"}

    def test_no_bases(self):
        cls = self._parse_class("class Foo: pass")
        assert _get_base_names(cls) == set()


# ---------------------------------------------------------------------------
# extract_mcp_tool_info
# ---------------------------------------------------------------------------


class TestExtractMcpToolInfo:
    def test_valid_endpoint(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "run_sim.py", VALID_MCP_TOOL_SRC)
        info = extract_mcp_tool_info(p, builtin=True, package="mcp")
        assert info is not None
        assert info.name == "run_sim"
        assert info.description == "Run an RTL simulation"
        assert info.code_modifying is False
        assert info.path == "mcp/run_sim.py"

    def test_code_modifying_endpoint(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "apply_patch.py", VALID_MCP_TOOL_CODE_MODIFYING)
        info = extract_mcp_tool_info(p, builtin=True)
        assert info is not None
        assert info.code_modifying is True

    def test_flow_endpoint(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "lint.py", FLOW_SRC)
        info = extract_mcp_tool_info(p, builtin=True)
        assert info is not None
        assert info.name == "lint_check"

    def test_specialist(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "debug.py", SPECIALIST_SRC)
        info = extract_mcp_tool_info(p, builtin=True)
        assert info is not None
        assert info.name == "debug_agent"

    def test_no_endpoint_class_returns_none(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "helper.py", NO_ENDPOINT_CLASS_SRC)
        assert extract_mcp_tool_info(p, builtin=True) is None

    def test_missing_name_returns_none(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "broken.py", MISSING_NAME_SRC)
        assert extract_mcp_tool_info(p, builtin=True) is None

    def test_missing_description_returns_none(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "orphan.py", MISSING_DESCRIPTION_SRC)
        assert extract_mcp_tool_info(p, builtin=True) is None

    def test_syntax_error_returns_none(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "bad.py", SYNTAX_ERROR_SRC)
        assert extract_mcp_tool_info(p, builtin=True) is None

    def test_attribute_style_base(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "fancy.py", ATTRIBUTE_BASE_SRC)
        info = extract_mcp_tool_info(p, builtin=True)
        assert info is not None
        assert info.name == "fancy"

    def test_builtin_false_stores_full_path(self, tmp_path):
        p = _write_endpoint_file(tmp_path, "custom.py", VALID_MCP_TOOL_SRC)
        info = extract_mcp_tool_info(p, builtin=False)
        assert info is not None
        assert info.path == str(p)

    def test_first_matching_class_wins(self, tmp_path):
        """When a file has multiple Endpoint subclasses, the first one is returned."""
        p = _write_endpoint_file(tmp_path, "multi.py", MULTIPLE_CLASSES_SRC)
        info = extract_mcp_tool_info(p, builtin=True)
        assert info is not None
        assert info.name == "first_endpoint"

    def test_nonexistent_file_returns_none(self, tmp_path):
        p = tmp_path / "ghost.py"
        assert extract_mcp_tool_info(p, builtin=True) is None


# ---------------------------------------------------------------------------
# _scan_directory
# ---------------------------------------------------------------------------


class TestScanDirectory:
    def test_empty_directory(self, tmp_path):
        assert _scan_directory(tmp_path, {}, builtin=True) == []

    def test_nonexistent_directory(self, tmp_path):
        assert _scan_directory(tmp_path / "nope", {}, builtin=True) == []

    def test_discovers_valid_endpoints(self, tmp_path):
        _write_endpoint_file(tmp_path, "run_sim.py", VALID_MCP_TOOL_SRC)
        _write_endpoint_file(tmp_path, "lint.py", FLOW_SRC)
        results = _scan_directory(tmp_path, {}, builtin=True, package="mcp")
        names = {t.name for t in results}
        assert names == {"lint_check", "run_sim"}

    def test_skips_underscored_files(self, tmp_path):
        _write_endpoint_file(tmp_path, "_internal.py", VALID_MCP_TOOL_SRC)
        assert _scan_directory(tmp_path, {}, builtin=True) == []

    def test_skips_skip_modules(self, tmp_path):
        for mod_name in ["base", "registry", "__init__"]:
            _write_endpoint_file(tmp_path, f"{mod_name}.py", VALID_MCP_TOOL_SRC)
        assert _scan_directory(tmp_path, {}, builtin=True) == []

    def test_config_disables_endpoint(self, tmp_path):
        _write_endpoint_file(tmp_path, "run_sim.py", VALID_MCP_TOOL_SRC)
        config = {"run_sim": {"enabled": False}}
        results = _scan_directory(tmp_path, config, builtin=True)
        assert results == []

    def test_config_enables_endpoint_explicitly(self, tmp_path):
        _write_endpoint_file(tmp_path, "run_sim.py", VALID_MCP_TOOL_SRC)
        config = {"run_sim": {"enabled": True}}
        results = _scan_directory(tmp_path, config, builtin=True)
        assert len(results) == 1
        assert results[0].name == "run_sim"

    def test_config_default_is_enabled(self, tmp_path):
        """Endpoints not mentioned in config are enabled by default."""
        _write_endpoint_file(tmp_path, "run_sim.py", VALID_MCP_TOOL_SRC)
        config = {"some_other_endpoint": {"enabled": False}}
        results = _scan_directory(tmp_path, config, builtin=True)
        assert len(results) == 1

    def test_results_sorted_by_filename(self, tmp_path):
        _write_endpoint_file(tmp_path, "z_endpoint.py", VALID_MCP_TOOL_SRC)
        # Need a distinct endpoint name for the second file
        alt_src = VALID_MCP_TOOL_SRC.replace("run_sim", "alpha_sim")
        _write_endpoint_file(tmp_path, "a_endpoint.py", alt_src)
        results = _scan_directory(tmp_path, {}, builtin=True, package="mcp")
        # a_endpoint.py comes before z_endpoint.py alphabetically
        assert results[0].path == "mcp/a_endpoint.py"
        assert results[1].path == "mcp/z_endpoint.py"

    def test_skips_non_endpoint_files(self, tmp_path):
        _write_endpoint_file(tmp_path, "helper.py", NO_ENDPOINT_CLASS_SRC)
        assert _scan_directory(tmp_path, {}, builtin=True) == []

    def test_skips_syntax_error_files(self, tmp_path):
        _write_endpoint_file(tmp_path, "broken.py", SYNTAX_ERROR_SRC)
        _write_endpoint_file(tmp_path, "good.py", VALID_MCP_TOOL_SRC)
        results = _scan_directory(tmp_path, {}, builtin=True)
        assert len(results) == 1
        assert results[0].name == "run_sim"


# ---------------------------------------------------------------------------
# discover_mcp_tools (integration)
# ---------------------------------------------------------------------------


class TestDiscoverMcpTools:
    def test_builtin_flow_packages_use_explicit_manifest(self, tmp_path):
        flows_dir = tmp_path / "flows"
        for package_name in BUILTIN_FLOW_PACKAGES:
            package_dir = flows_dir / package_name
            package_dir.mkdir(parents=True)
            source = FLOW_SRC.replace('name = "lint_check"', f'name = "{package_name}"')
            _write_endpoint_file(package_dir, "flow.py", source)

        results = _scan_builtin_flows(flows_dir, {})

        assert [result.name for result in results] == list(BUILTIN_FLOW_PACKAGES)
        assert [result.path for result in results] == [
            f"flows/{package_name}/flow.py" for package_name in BUILTIN_FLOW_PACKAGES
        ]

    def test_builtin_flow_manifest_ignores_unregistered_package(self, tmp_path):
        package_dir = tmp_path / "flows" / "surprise"
        package_dir.mkdir(parents=True)
        _write_endpoint_file(package_dir, "flow.py", FLOW_SRC)

        assert _scan_builtin_flows(tmp_path / "flows", {}) == []

    def test_builtin_only(self, tmp_path):
        """Discover from a single builtin endpoints directory."""
        endpoint_dir = tmp_path / "mcp"
        endpoint_dir.mkdir()
        _write_endpoint_file(endpoint_dir, "sim.py", VALID_MCP_TOOL_SRC)
        results = discover_mcp_tools(booley_src=tmp_path)
        assert len(results) == 1
        assert results[0].name == "run_sim"

    def test_builtin_plus_custom(self, tmp_path):
        """Both builtin and project-level endpoints are discovered."""
        builtin_dir = tmp_path / "builtin" / "mcp"
        builtin_dir.mkdir(parents=True)
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        _write_endpoint_file(builtin_dir, "sim.py", VALID_MCP_TOOL_SRC)
        _write_endpoint_file(custom_dir, "my_endpoint.py", FLOW_SRC)
        results = discover_mcp_tools(
            booley_src=builtin_dir.parent,
            project_mcp_tools_dir=custom_dir,
        )
        names = {t.name for t in results}
        assert names == {"run_sim", "lint_check"}

    def test_custom_dir_nonexistent_is_fine(self, tmp_path):
        """Non-existent project_mcp_tools_dir is silently ignored."""
        endpoint_dir = tmp_path / "mcp"
        endpoint_dir.mkdir()
        _write_endpoint_file(endpoint_dir, "sim.py", VALID_MCP_TOOL_SRC)
        results = discover_mcp_tools(
            booley_src=tmp_path,
            project_mcp_tools_dir=tmp_path / "nonexistent",
        )
        assert len(results) == 1

    def test_custom_dir_none_is_fine(self, tmp_path):
        endpoint_dir = tmp_path / "mcp"
        endpoint_dir.mkdir()
        _write_endpoint_file(endpoint_dir, "sim.py", VALID_MCP_TOOL_SRC)
        results = discover_mcp_tools(booley_src=tmp_path, project_mcp_tools_dir=None)
        assert len(results) == 1

    def test_empty_builtin_dir(self, tmp_path):
        endpoint_dir = tmp_path / "mcp"
        endpoint_dir.mkdir()
        results = discover_mcp_tools(booley_src=tmp_path)
        assert results == []

    def test_endpoint_config_filtering(self, tmp_path):
        endpoint_dir = tmp_path / "mcp"
        endpoint_dir.mkdir()
        _write_endpoint_file(endpoint_dir, "sim.py", VALID_MCP_TOOL_SRC)
        _write_endpoint_file(endpoint_dir, "lint.py", FLOW_SRC)
        results = discover_mcp_tools(
            booley_src=tmp_path,
            mcp_tool_config={"run_sim": {"enabled": False}},
        )
        assert len(results) == 1
        assert results[0].name == "lint_check"

    def test_endpoint_config_none_defaults_empty(self, tmp_path):
        endpoint_dir = tmp_path / "mcp"
        endpoint_dir.mkdir()
        _write_endpoint_file(endpoint_dir, "sim.py", VALID_MCP_TOOL_SRC)
        results = discover_mcp_tools(booley_src=tmp_path, mcp_tool_config=None)
        assert len(results) == 1

    def test_builtin_mcp_tools_use_package_path(self, tmp_path):
        endpoint_dir = tmp_path / "mcp"
        endpoint_dir.mkdir()
        _write_endpoint_file(endpoint_dir, "sim.py", VALID_MCP_TOOL_SRC)
        results = discover_mcp_tools(booley_src=tmp_path)
        assert results[0].path == "mcp/sim.py"

    def test_custom_mcp_tools_use_full_path(self, tmp_path):
        builtin_dir = tmp_path / "builtin" / "mcp"
        builtin_dir.mkdir(parents=True)
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        p = _write_endpoint_file(custom_dir, "my.py", VALID_MCP_TOOL_SRC)
        results = discover_mcp_tools(
            booley_src=builtin_dir.parent,
            project_mcp_tools_dir=custom_dir,
        )
        assert results[0].path == str(p)


# ---------------------------------------------------------------------------
# SKIP_MODULES sanity
# ---------------------------------------------------------------------------


class TestSkipModules:
    def test_contains_expected_entries(self):
        for name in ("base", "registry", "__init__", "specialist"):
            assert name in SKIP_MODULES

    def test_is_frozenset(self):
        assert isinstance(SKIP_MODULES, frozenset)

    def test_all_skip_modules_filtered(self, tmp_path):
        """Every entry in SKIP_MODULES is actually filtered out."""
        for mod in SKIP_MODULES:
            if mod.startswith("__"):
                continue  # __init__ also caught by underscore check
            _write_endpoint_file(tmp_path, f"{mod}.py", VALID_MCP_TOOL_SRC)
        results = _scan_directory(tmp_path, {}, builtin=True)
        assert results == []

    # Endpoint infrastructure entries kept defensively even though they carry no
    # discoverable Endpoint subclass today (base defines Endpoint itself, etc.).
    _INFRA_ENTRIES = frozenset({"__init__", "base", "specialist", "registry"})

    def test_no_redundant_helper_entries(self):
        """SKIP_MODULES must not re-grow support-module entries.

        Both discovery paths gate on ``extract_mcp_tool_info()`` (import-free AST
        check), so a module with no Endpoint subclass is skipped automatically —
        listing it here would be dead weight that rots as modules are renamed.
        Every non-infrastructure entry must exist AND actually parse as a endpoint
        module (i.e. it is a deliberately *hidden* endpoint, the set's one job).
        """
        endpoint_dir = Path(__file__).resolve().parents[2] / "src" / "booley" / "specialists"
        for mod in SKIP_MODULES - self._INFRA_ENTRIES:
            py_file = endpoint_dir / f"{mod}.py"
            assert py_file.is_file(), f"stale SKIP_MODULES entry: {mod} (no such module)"
            assert extract_mcp_tool_info(py_file, builtin=True) is not None, (
                f"redundant SKIP_MODULES entry: {mod} has no Endpoint subclass, so the "
                "extract_mcp_tool_info() gate already skips it — delete the entry"
            )


# ---------------------------------------------------------------------------
# per-endpoint availability
# ---------------------------------------------------------------------------


class TestPerMcpToolAvailability:
    def test_only_explicitly_disabled_endpoint_is_filtered(self, tmp_path):
        for endpoint_name in ("reviewer", "submit_run_report"):
            src = VALID_MCP_TOOL_SRC.replace(
                'name = "run_sim"',
                f'name = "{endpoint_name}"',
            )
            _write_endpoint_file(tmp_path, f"{endpoint_name}.py", src)
        _write_endpoint_file(tmp_path, "sim.py", VALID_MCP_TOOL_SRC)

        results = _scan_directory(
            tmp_path,
            {"submit_run_report": {"enabled": False}},
            builtin=True,
        )

        names = {t.name for t in results}
        assert names == {"run_sim", "reviewer"}

    def test_mcp_tools_are_enabled_by_default(self, tmp_path):
        src = VALID_MCP_TOOL_SRC.replace(
            'name = "run_sim"',
            'name = "submit_run_report"',
        )
        _write_endpoint_file(tmp_path, "submit_run_report.py", src)

        results = _scan_directory(tmp_path, {}, builtin=True)

        names = {t.name for t in results}
        assert names == {"submit_run_report"}
