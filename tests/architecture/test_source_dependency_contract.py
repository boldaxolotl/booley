from __future__ import annotations

import ast
from pathlib import Path

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
        "minimal_selector",
        "resolve_public_ref",
        "resolve_ref",
        "resolve_target",
        "resolve_target_selection",
        "setup_command",
        "target_cocotb_modules",
        "target_declarations",
        "target_eda_tools",
        "try_resolve_target",
    }
)


def test_only_target_adapters_use_low_level_target_mechanics() -> None:
    """Keep discovery/selection behind TargetCatalog and the FuseSoC adapter."""
    violations: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        if relative.startswith("fusesoc/") or relative == "targets/catalog.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "fusesoc_registry"
                and node.attr in _LOW_LEVEL_TARGET_MECHANICS
            ):
                violations.append(f"{relative}:{node.lineno}: {node.attr}")
            if isinstance(node, ast.ImportFrom) and node.module in {
                "booley.fusesoc.fusesoc_registry",
                "booley.targets.target",
            }:
                violations.extend(
                    f"{relative}:{node.lineno}: {alias.name}"
                    for alias in node.names
                    if alias.name in _LOW_LEVEL_TARGET_MECHANICS
                )

    assert not violations, "Low-level Target mechanics escaped their adapters:\n" + "\n".join(
        violations
    )
