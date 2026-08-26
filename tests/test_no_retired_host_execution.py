"""Negative architecture guards for retired compatibility surfaces."""

import ast
from pathlib import Path

import pytest

from booley.dev_support.criteria import eligible_eda_tool_criterion_families
from booley.fusesoc.fusesoc_registry import TargetRef
from booley.targets.target_surface import flow_can_drive

_FORBIDDEN_MODULES = ("booley.host_mcp", "booley.mcp_tools", "booley.tools")
_FORBIDDEN_SYMBOLS = (
    "CLASS_HOST",
    "max_host",
    "supported_venues",
    "default_venue",
    "_execute_host",
    "host_mcp_url",
    "host_mcp_spec_wired",
    "write_host_sim_makefile",
    "host_sim_make_command",
    "kill_zombie_flow_processes",
    "legacy_backend",
)
_FORBIDDEN_FUNCTIONS = {
    "do_run",
    "run_openroad_timing",
    "run_opensta",
    "run_sv2v",
    "run_yosys",
}


def test_retired_host_execution_files_are_absent() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not list((root / "src/booley/host_mcp").glob("*.py"))
    assert not (root / "src/booley/venue.py").exists()
    assert not (root / "src/booley/yosys/syn_subprocess.py").exists()
    assert not (root / "src/booley/yosys/synthesis_watchdog.py").exists()
    assert not (root / "src/booley/runtime/zombie_cleanup.py").exists()

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "booley-host-mcp" not in pyproject
    assert "host_mcp/templates" not in pyproject


def test_production_has_no_retired_host_execution_symbols() -> None:
    root = Path(__file__).resolve().parents[1]
    production = root / "src/booley"
    for path in production.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0
        )
        for forbidden in _FORBIDDEN_MODULES:
            assert not any(
                module == forbidden or module.startswith(f"{forbidden}.") for module in imports
            ), f"retired import {forbidden!r} remains in {path}"
        for forbidden in _FORBIDDEN_SYMBOLS:
            assert forbidden not in text, f"{forbidden!r} remains in {path}"
        function_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not (function_names & _FORBIDDEN_FUNCTIONS), (
            f"retired direct EDA launcher remains in {path}: "
            f"{sorted(function_names & _FORBIDDEN_FUNCTIONS)}"
        )


def test_yosys_configure_surface_has_no_run_action() -> None:
    from booley.yosys.run_yosys_syn import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["run"])


def test_retired_packages_and_flat_root_modules_are_absent() -> None:
    root = Path(__file__).resolve().parents[1]
    package = root / "src/booley"
    retired_packages = ("host_mcp", "mcp_tools", "tools")
    for name in retired_packages:
        assert not list((package / name).glob("*.py")), f"retired package remains: booley.{name}"

    root_modules = sorted(path.name for path in package.glob("*.py"))
    assert root_modules == ["__init__.py"], (
        f"booley package-root modules must live in a named subsystem package: {root_modules}"
    )


def test_unsupported_commercial_simulators_have_no_public_eligibility() -> None:
    """Xcelium/VCS may occur in captured logs, never in runnable policy."""
    for tool in ("xcelium", "vcs"):
        ref = TargetRef(
            name="vendor_sim",
            vlnv="vendor:lib:ip:1",
            core_file=Path("vendor.core"),
            eda_tool=tool,
            flow="sim",
        )
        assert not flow_can_drive("sim", ref)
        assert not flow_can_drive("elab", ref)
        assert eligible_eda_tool_criterion_families(tool) == frozenset()
