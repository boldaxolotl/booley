"""Static dependency-direction checks for the package architecture."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "src/booley"


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
    return imports


def _assert_no_import_prefixes(paths: list[Path], forbidden: tuple[str, ...]) -> None:
    violations: list[str] = []
    for path in paths:
        for module in sorted(_absolute_imports(path)):
            if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden):
                violations.append(f"{path.relative_to(_ROOT)} -> {module}")
    assert not violations, "dependency direction violations:\n" + "\n".join(violations)


def _python_files(package: str) -> list[Path]:
    return sorted((_PACKAGE / package).glob("*.py"))


def test_configuration_and_domain_packages_do_not_depend_on_agent_layers() -> None:
    paths = _python_files("config") + _python_files("fusesoc") + _python_files("targets")
    _assert_no_import_prefixes(
        paths,
        ("booley.harness", "booley.mcp", "booley.specialists"),
    )


def test_specialists_do_not_depend_on_harness_or_mcp_server() -> None:
    _assert_no_import_prefixes(
        _python_files("specialists"),
        ("booley.harness", "booley.mcp.server"),
    )


def test_mcp_infrastructure_does_not_depend_on_specialists_or_harness() -> None:
    infrastructure = [path for path in _python_files("mcp") if path.name != "server.py"]
    _assert_no_import_prefixes(
        infrastructure,
        ("booley.harness", "booley.specialists"),
    )


def test_runtime_does_not_depend_on_agent_facing_layers() -> None:
    _assert_no_import_prefixes(
        _python_files("runtime"),
        ("booley.mcp", "booley.specialists"),
    )


def test_shared_runtime_does_not_depend_on_harness() -> None:
    composition_entrypoints = {"heartbeat.py", "incontainer_register.py"}
    shared_runtime = [
        path for path in _python_files("runtime") if path.name not in composition_entrypoints
    ]
    _assert_no_import_prefixes(shared_runtime, ("booley.harness",))
