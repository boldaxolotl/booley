from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import evaluate_contract, format_problems
from tests.architecture.import_graph import analyze_imports

_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "booley"


def test_production_source_dependencies_obey_approved_contract() -> None:
    dependencies = analyze_imports(_SOURCE_ROOT)

    problems = evaluate_contract(dependencies, BOOLEY_SOURCE_DEPENDENCY_CONTRACT)

    assert not problems, "Source dependency contract failures:\n" + format_problems(problems)


_LOW_LEVEL_TARGET_MECHANICS = frozenset(
    {
        "available_targets",
        "doctor_target_seed",
        "doctor_target_selectors",
        "enumerate_targets",
        "enumerate_all",
        "index_core_documents",
        "minimal_selector",
        "resolve_public_ref",
        "resolve_ref",
        "resolve_target",
        "resolve_target_selection",
        "selectable_core_closure",
        "selectable_core_closure_for_refs",
        "select_target",
        "select_targets",
        "setup_command",
        "target_cocotb_modules",
        "target_declarations",
        "target_eda_tools",
        "try_resolve_target",
        "missing_target_sources",
        "preflight_target_sources",
        "sim_target_has_untagged_tb",
        "target_referenced_files",
        "target_source_files",
        "TargetSourceInspector",
        "_TargetSourceInspector",
        "resolve",
        "split_selector",
        "vlnv_key",
    }
)

_LOW_LEVEL_MODULES = frozenset(
    {
        "booley.fusesoc.fusesoc_registry",
        "booley.fusesoc.target_inspection",
        "booley.targets.selection",
    }
)
_TARGET_ADAPTER_PATHS = frozenset(
    {
        "fusesoc/fusesoc_registry.py",
        "fusesoc/target_inspection.py",
        "targets/catalog.py",
    }
)


def _target_mechanics_violations(path: Path, relative: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_aliases: set[str] = set()
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in _LOW_LEVEL_MODULES
            )
        elif isinstance(node, ast.ImportFrom) and node.module in _LOW_LEVEL_MODULES:
            violations.extend(
                f"{relative}:{node.lineno}: {alias.name}"
                for alias in node.names
                if alias.name.lstrip("_") in _LOW_LEVEL_TARGET_MECHANICS
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "booley.fusesoc":
            module_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if f"booley.fusesoc.{alias.name}" in _LOW_LEVEL_MODULES
            )
    for node in ast.walk(tree):
        dotted = _dotted_name(node)
        if (
            isinstance(node, ast.Attribute)
            and any(dotted == f"{module}.{node.attr}" for module in module_aliases)
            and node.attr.lstrip("_") in _LOW_LEVEL_TARGET_MECHANICS
        ):
            violations.append(f"{relative}:{node.lineno}: {node.attr}")
    return violations


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def test_only_target_adapters_use_low_level_target_mechanics() -> None:
    """Keep discovery/selection behind TargetCatalog and the FuseSoC adapter."""
    violations: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        if relative in _TARGET_ADAPTER_PATHS:
            continue
        violations.extend(_target_mechanics_violations(path, relative))

    assert not violations, "Low-level Target mechanics escaped their adapters:\n" + "\n".join(
        violations
    )


@pytest.mark.parametrize(
    "source",
    [
        "import booley.fusesoc.fusesoc_registry as registry\nregistry.resolve_ref('.', 'sim')\n",
        "import booley.fusesoc.fusesoc_registry\n"
        "booley.fusesoc.fusesoc_registry.resolve_ref('.', 'sim')\n",
        "from booley.fusesoc import fusesoc_registry as registry\nregistry.resolve_ref('.', 'sim')\n",
        "from booley.fusesoc.fusesoc_registry import resolve_ref as choose\nchoose('.', 'sim')\n",
        "from booley.fusesoc.target_inspection import TargetSourceInspector\n"
        "TargetSourceInspector('.')\n",
        "from booley.fusesoc.fusesoc_registry import _resolve_ref as choose\nchoose('.', 'sim')\n",
        "from booley.fusesoc import fusesoc_registry as registry\n"
        "registry._target_declarations('.')\n",
        "from booley.fusesoc.fusesoc_registry import _enumerate_all as enumerate_all\n"
        "enumerate_all('.')\n",
        "from booley.fusesoc.fusesoc_registry import _selectable_core_closure_for_refs\n"
        "_selectable_core_closure_for_refs('.', [])\n",
        "from booley.fusesoc.fusesoc_registry import _index_core_documents\n"
        "_index_core_documents('.')\n",
        "from booley.targets.selection import resolve as choose\nchoose({}, 'sim')\n",
    ],
)
def test_low_level_target_gate_resolves_import_aliases(tmp_path: Path, source: str) -> None:
    path = tmp_path / "consumer.py"
    path.write_text(source, encoding="utf-8")

    assert _target_mechanics_violations(path, "consumer.py")


@pytest.mark.parametrize(
    "source",
    (
        "def schema():\n    from booley.mcp.base import McpTool\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import booley.mcp.base\n",
    ),
)
def test_flow_mcp_prohibition_includes_deferred_and_type_only_imports(tmp_path, source):
    from tests.architecture.contract import ArchitectureContract

    root = tmp_path / "booley"
    for package in (root, root / "flows", root / "mcp"):
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text("")
    (root / "mcp/base.py").write_text("class McpTool: pass\n")
    (root / "flows/rogue.py").write_text(source)
    contract = ArchitectureContract(
        rules=tuple(
            rule for rule in BOOLEY_SOURCE_DEPENDENCY_CONTRACT.rules if rule.identifier == "D15"
        ),
    )
    problems = evaluate_contract(analyze_imports(root), contract)
    assert problems
    assert "D15" in format_problems(problems)


@pytest.mark.parametrize(
    "statement",
    [
        "import booley.ticket_board.paths\n",
        "def helper():\n    from booley.ticket_board import paths\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from booley.ticket_board.paths import value\n",
    ],
)
def test_runtime_ticket_board_rule_catches_all_static_import_locations(tmp_path, statement):
    root = tmp_path / "booley"
    for package in (root, root / "runtime", root / "ticket_board"):
        package.mkdir(exist_ok=True)
        (package / "__init__.py").touch()
    (root / "ticket_board" / "paths.py").write_text("value = 1\n")
    (root / "runtime" / "seed.py").write_text(statement)
    problems = evaluate_contract(analyze_imports(root), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)
    report = format_problems(problems)
    assert "D14" in report
    assert "booley.runtime.seed" in report
    assert "booley.ticket_board.paths" in report


@pytest.mark.parametrize(
    "statement",
    [
        "import booley.flows.execution\n",
        "def helper():\n    from booley.flows import execution\n",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from booley.flows.execution import run\n",
    ],
)
def test_criteria_flow_rule_catches_all_static_import_locations(tmp_path, statement):
    root = tmp_path / "booley"
    for package in (root, root / "criteria", root / "flows"):
        package.mkdir(exist_ok=True)
        (package / "__init__.py").touch()
    (root / "flows" / "execution.py").write_text("def run(): pass\n")
    (root / "criteria" / "policy.py").write_text(statement)

    problems = evaluate_contract(analyze_imports(root), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)
    report = format_problems(problems)
    assert "D16" in report
    assert "booley.criteria.policy" in report
    assert "booley.flows.execution" in report
