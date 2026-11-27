from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import evaluate_contract, format_problems
from tests.architecture.import_graph import (
    SourceModule,
    SourceTreeAnalysis,
    analyze_imports,
    analyze_source_tree,
    mutual_package_pairs,
)

_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "booley"

_SIMULATION_RECORD_STORAGE_TYPES = frozenset(
    {
        "SimulationRecordStore",
        "SimulationRecordRecovery",
        "WorkItemRecovery",
        "SharedBuildRecovery",
    }
)
_SIMULATION_RECORD_STORAGE_OWNERS = frozenset(
    {
        "booley.flows.sim.record.store",
        "booley.flows.sim.record.coordinator",
        "booley.flows.sim.record.inspection",
    }
)
_SIMULATION_RECORD_PREFIX = "booley.flows.sim.record"
_SIMULATION_RECORD_SCAN = "simulation-record-storage-types"
_TARGET_SCAN = "target-mechanics"


@pytest.fixture(scope="module")
def production_source() -> SourceTreeAnalysis:
    """The production tree, parsed once for every whole-source assertion in this module."""
    return analyze_source_tree(
        _SOURCE_ROOT,
        {
            _SIMULATION_RECORD_SCAN: _simulation_record_storage_scan,
            _TARGET_SCAN: _target_mechanics_scan,
        },
    )


def test_production_source_dependencies_obey_approved_contract(
    production_source: SourceTreeAnalysis,
) -> None:
    dependencies = production_source.dependencies

    problems = evaluate_contract(dependencies, BOOLEY_SOURCE_DEPENDENCY_CONTRACT)

    assert not problems, (
        "Source dependency contract failures:\n"
        + format_problems(problems)
        + "\nDo not add a permission to make the import pass. Move the shared piece below both"
        " packages; the module docstring of tests/architecture/booley_contract.py says where"
        " shared code lives and how to change the contract. To see what a change moved, run"
        " `python3 tests/architecture/compare_report.py --before-ref <base> --after-ref <head>"
        " --repo-root .` and attach it to the PR."
    )
    # Flows reach Harness only through what the command layer supplies (#3062).
    assert ("booley.flows", "booley.harness") not in mutual_package_pairs(dependencies)
    assert not [
        dependency
        for dependency in dependencies
        if dependency.source.startswith("booley.flows")
        and dependency.target.startswith("booley.harness")
    ]


def test_job_and_report_owners_have_no_dependency_path_to_mcp_adapter(
    production_source: SourceTreeAnalysis,
) -> None:
    dependencies = production_source.dependencies
    edges: dict[str, set[str]] = {}
    for dependency in dependencies:
        edges.setdefault(dependency.source, set()).add(dependency.target)
    for owner in (
        "booley.mcp.job_lifecycle",
        "booley.mcp.job_results",
        "booley.mcp.endpoint_process",
        "booley.mcp.report_encoding",
    ):
        pending, visited = [owner], set()
        while pending:
            module = pending.pop()
            if module in visited:
                continue
            assert module != "booley.mcp.server", f"{owner} depends on the MCP adapter"
            visited.add(module)
            pending.extend(edges.get(module, set()) - visited)


def test_every_simulation_record_storage_permission_is_a_current_exact_edge(
    production_source: SourceTreeAnalysis,
) -> None:
    dependencies = production_source.dependencies
    edges = {(item.source, item.target) for item in dependencies}
    permissions = tuple(
        item for item in BOOLEY_SOURCE_DEPENDENCY_CONTRACT.permissions if item.rule == "D31"
    )

    assert permissions
    assert all((item.source, item.target) in edges for item in permissions)


def test_simulation_record_storage_types_do_not_escape_their_exact_owners(
    production_source: SourceTreeAnalysis,
) -> None:
    violations = list(production_source.findings(_SIMULATION_RECORD_SCAN))

    assert not violations, "Simulation Record storage types escaped their owners:\n" + "\n".join(
        violations
    )


def _simulation_record_storage_scan(module: SourceModule, tree: ast.Module) -> list[str]:
    return _simulation_record_storage_violations_for_source(tree, module.name, module.is_package)


def _simulation_record_storage_violations_for_source(
    tree: ast.Module, module: str, is_package: bool
) -> list[str]:
    if module in _SIMULATION_RECORD_STORAGE_OWNERS:
        return []
    return _simulation_record_storage_type_violations(tree, module, is_package)


def _simulation_record_storage_type_violations(
    tree: ast.Module, module: str, is_package: bool
) -> list[str]:
    aliases: dict[str, str] = {"booley": "booley"}
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_SIMULATION_RECORD_PREFIX):
                    bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
                    aliases[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom):
            imported_from = _resolved_import_from(module, is_package, node)
            if imported_from is None or not imported_from.startswith(_SIMULATION_RECORD_PREFIX):
                continue
            for alias in node.names:
                if alias.name in _SIMULATION_RECORD_STORAGE_TYPES or alias.name == "*":
                    violations.append(f"{module}:{node.lineno}: {alias.name}")
                else:
                    aliases[alias.asname or alias.name] = f"{imported_from}.{alias.name}"
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Attribute)
            or node.attr not in _SIMULATION_RECORD_STORAGE_TYPES
        ):
            continue
        dotted = _dotted_name(node)
        root, separator, suffix = dotted.partition(".")
        resolved = aliases.get(root)
        if (
            resolved is not None
            and separator
            and f"{resolved}.{suffix}".startswith(_SIMULATION_RECORD_PREFIX)
        ):
            violations.append(f"{module}:{node.lineno}: {node.attr}")
    return violations


def _resolved_import_from(module: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = module if is_package else module.rpartition(".")[0]
    parts = package.split(".")
    parents = node.level - 1
    if parents >= len(parts):
        return None
    base = parts[: len(parts) - parents]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


_ESCAPE_SOURCES = (
    "from booley.flows.sim.record.coordinator import SimulationRecordStore\n",
    "from booley.flows.sim.record.coordinator import SimulationRecordStore as Store\n",
    "import booley.flows.sim.record.coordinator\n"
    "booley.flows.sim.record.coordinator.SimulationRecordRecovery\n",
    "import booley.flows.sim.record.coordinator as coordinator\ncoordinator.WorkItemRecovery\n",
    "from booley.flows.sim.record import coordinator\ncoordinator.SharedBuildRecovery\n",
    "def deferred():\n    from booley.flows.sim.record.coordinator import SimulationRecordStore\n",
    "if enabled:\n    from booley.flows.sim.record.coordinator import SimulationRecordRecovery\n",
    "from typing import TYPE_CHECKING\n"
    "if TYPE_CHECKING:\n"
    "    from booley.flows.sim.record.coordinator import WorkItemRecovery\n",
)
_RELATIVE_SOURCE = "from .record.coordinator import SimulationRecordStore\n"
_PUBLIC_IMPORT = "from booley.flows.sim.record.store import SimulationRecordStore\n"


@pytest.mark.parametrize(
    ("source", "module", "is_package", "flagged"),
    [
        *((source, "booley.flows.sim.rogue", False, True) for source in _ESCAPE_SOURCES),
        # A relative import resolves against the package only for a package module.
        (_RELATIVE_SOURCE, "booley.flows.sim", True, True),
        (_RELATIVE_SOURCE, "booley.flows.sim", False, False),
        *(
            (_PUBLIC_IMPORT, owner, False, False)
            for owner in sorted(_SIMULATION_RECORD_STORAGE_OWNERS)
        ),
    ],
)
def test_simulation_record_storage_type_gate(
    source: str, module: str, is_package: bool, flagged: bool
) -> None:
    """Escape forms are flagged, a package-relative import resolves, exact owners are exempt."""
    tree = ast.parse(source)

    violations = _simulation_record_storage_violations_for_source(tree, module, is_package)

    assert bool(violations) is flagged


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
        "resolve_target_selection",
        "selectable_core_closure",
        "selectable_core_closure_for_refs",
        "select_target",
        "select_targets",
        "target_cocotb_modules",
        "target_declarations",
        "target_eda_tools",
        "sim_target_has_untagged_tb",
        "target_referenced_files",
        "target_source_files",
        "TargetSourceInspector",
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


def _target_mechanics_scan(module: SourceModule, tree: ast.Module) -> list[str]:
    relative = module.path.relative_to(_SOURCE_ROOT.resolve()).as_posix()
    if relative in _TARGET_ADAPTER_PATHS:
        return []
    return _target_mechanics_violations(tree, relative)


def _target_mechanics_violations(tree: ast.Module, relative: str) -> list[str]:
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
                # Strip the underscore so a private alias of a public mechanism is still flagged.
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
            # Same underscore normalization as the import check above.
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


def test_only_target_adapters_use_low_level_target_mechanics(
    production_source: SourceTreeAnalysis,
) -> None:
    """Keep discovery/selection behind TargetCatalog and the FuseSoC adapter."""
    violations = list(production_source.findings(_TARGET_SCAN))

    assert not violations, "Low-level Target mechanics escaped their adapters:\n" + "\n".join(
        violations
    )


# The underscore-prefixed names below are seeded source text: the gate must flag a private
# alias of a public mechanism, and no public name expresses that spelling.
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
        "from booley.fusesoc.target_inspection import _TargetSourceInspector\n"
        "_TargetSourceInspector('.')\n",
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
def test_low_level_target_gate_resolves_import_aliases(source: str) -> None:
    assert _target_mechanics_violations(ast.parse(source), "consumer.py")


def test_shared_health_has_no_transitive_command_or_sdk_dependency(
    production_source: SourceTreeAnalysis,
) -> None:
    edges: dict[str, set[str]] = {}
    for item in production_source.dependencies:
        edges.setdefault(item.source, set()).add(item.target)
    pending = {
        "booley.audit.health",
        "booley.runtime.health",
        "booley.runtime.doctor_stamp",
        "booley.runtime.health_presentation",
    }
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        assert not current.startswith(("booley.harness", "booley.mcp", "booley.specialists")), (
            current
        )
        pending.update(edges.get(current, set()) - visited)


def test_health_consumers_reach_shared_health_without_the_doctor_worker(
    production_source: SourceTreeAnalysis,
) -> None:
    consumers = {"booley.mcp.server"}
    shared = {"booley.runtime.health", "booley.runtime.health_presentation"}
    worker = "booley.harness.auto_doctor"
    targets: dict[str, set[str]] = {consumer: set() for consumer in consumers}
    for edge in production_source.dependencies:
        if edge.source in consumers:
            targets[edge.source].add(edge.target)
    for consumer, imported in targets.items():
        # Proves the analyzer saw this consumer's health imports, deferred ones included.
        assert imported & shared, consumer
        assert worker not in imported, consumer
    assert (_SOURCE_ROOT / "harness" / "auto_doctor.py").is_file()


def test_mutation_flow_may_compose_the_simulation_flow(tmp_path: Path) -> None:
    """The one-way dependency the Mutation Flow is built on stays allowed."""
    root = tmp_path / "booley"
    for relative, text in (
        ("flows/sim/build.py", "def prepare(): ...\n"),
        ("flows/mutation/flow.py", "from booley.flows.sim.build import prepare\n"),
    ):
        path = root / relative
        for package in (root, *(root / parent for parent in Path(relative).parents[:-1])):
            package.mkdir(parents=True, exist_ok=True)
            (package / "__init__.py").touch()
        path.write_text(text, encoding="utf-8")

    report = format_problems(
        evaluate_contract(analyze_imports(root), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)
    )

    assert "D42" not in report
    assert "D8" not in report


def test_every_d26_diagnostic_owner_names_an_importable_module() -> None:
    """A renamed or deleted owner would leave D26 blind: the rule would select nothing."""
    selectors = [
        selector
        for rule in BOOLEY_SOURCE_DEPENDENCY_CONTRACT.rules
        if rule.identifier == "D26"
        for selector in rule.sources
    ]

    assert selectors
    missing = [
        selector.module
        for selector in selectors
        if selector.kind == "exact" and importlib.util.find_spec(selector.module) is None
    ]
    assert not missing, f"D26 names modules that no longer exist: {missing}"
