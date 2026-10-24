from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import evaluate_contract, format_problems
from tests.architecture.import_graph import (
    SourceModule,
    SourceTreeAnalysis,
    analyze_imports,
    analyze_source_tree,
)

_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "booley"

_CAMPAIGN_STORAGE_TYPES = frozenset(
    {"CampaignStore", "CampaignRecovery", "WorkItemRecovery", "SharedBuildRecovery"}
)
_CAMPAIGN_STORAGE_OWNERS = frozenset(
    {
        "booley.flows.sim.campaign.store",
        "booley.flows.sim.campaign.coordinator",
        "booley.flows.sim.campaign.child_protocol",
        "booley.flows.sim.campaign.inspection",
    }
)
_CAMPAIGN_PREFIX = "booley.flows.sim.campaign"
_CAMPAIGN_SCAN = "campaign-storage-types"
_TARGET_SCAN = "target-mechanics"


@pytest.fixture(scope="module")
def production_source() -> SourceTreeAnalysis:
    """The production tree, parsed once for every whole-source assertion in this module."""
    return analyze_source_tree(
        _SOURCE_ROOT,
        {_CAMPAIGN_SCAN: _campaign_storage_scan, _TARGET_SCAN: _target_mechanics_scan},
    )


def test_production_source_dependencies_obey_approved_contract(
    production_source: SourceTreeAnalysis,
) -> None:
    dependencies = production_source.dependencies

    problems = evaluate_contract(dependencies, BOOLEY_SOURCE_DEPENDENCY_CONTRACT)

    assert not problems, "Source dependency contract failures:\n" + format_problems(problems)


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


def test_every_campaign_storage_permission_is_a_current_exact_edge(
    production_source: SourceTreeAnalysis,
) -> None:
    dependencies = production_source.dependencies
    edges = {(item.source, item.target) for item in dependencies}
    permissions = tuple(
        item for item in BOOLEY_SOURCE_DEPENDENCY_CONTRACT.permissions if item.rule == "D31"
    )

    assert permissions
    assert all((item.source, item.target) in edges for item in permissions)


def test_campaign_storage_types_do_not_escape_their_exact_owners(
    production_source: SourceTreeAnalysis,
) -> None:
    violations = list(production_source.findings(_CAMPAIGN_SCAN))

    assert not violations, "Campaign storage types escaped their owners:\n" + "\n".join(violations)


def _campaign_storage_scan(module: SourceModule, tree: ast.Module) -> list[str]:
    return _campaign_storage_violations_for_source(tree, module.name, module.is_package)


def _campaign_storage_violations_for_source(
    tree: ast.Module, module: str, is_package: bool
) -> list[str]:
    if module in _CAMPAIGN_STORAGE_OWNERS:
        return []
    return _campaign_storage_type_violations(tree, module, is_package)


def _campaign_storage_type_violations(
    tree: ast.Module, module: str, is_package: bool
) -> list[str]:
    aliases: dict[str, str] = {"booley": "booley"}
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_CAMPAIGN_PREFIX):
                    bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
                    aliases[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom):
            imported_from = _resolved_import_from(module, is_package, node)
            if imported_from is None or not imported_from.startswith(_CAMPAIGN_PREFIX):
                continue
            for alias in node.names:
                if alias.name in _CAMPAIGN_STORAGE_TYPES or alias.name == "*":
                    violations.append(f"{module}:{node.lineno}: {alias.name}")
                else:
                    aliases[alias.asname or alias.name] = f"{imported_from}.{alias.name}"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in _CAMPAIGN_STORAGE_TYPES:
            continue
        dotted = _dotted_name(node)
        root, separator, suffix = dotted.partition(".")
        resolved = aliases.get(root)
        if (
            resolved is not None
            and separator
            and f"{resolved}.{suffix}".startswith(_CAMPAIGN_PREFIX)
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


@pytest.mark.parametrize(
    "source",
    [
        "from booley.flows.sim.campaign.coordinator import CampaignStore\n",
        "from booley.flows.sim.campaign.coordinator import CampaignStore as Store\n",
        "import booley.flows.sim.campaign.coordinator\n"
        "booley.flows.sim.campaign.coordinator.CampaignRecovery\n",
        "import booley.flows.sim.campaign.coordinator as coordinator\n"
        "coordinator.WorkItemRecovery\n",
        "from booley.flows.sim.campaign import coordinator\ncoordinator.SharedBuildRecovery\n",
        "def deferred():\n    from booley.flows.sim.campaign.coordinator import CampaignStore\n",
        "if enabled:\n    from booley.flows.sim.campaign.coordinator import CampaignRecovery\n",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from booley.flows.sim.campaign.coordinator import WorkItemRecovery\n",
    ],
)
def test_campaign_storage_type_gate_resolves_static_escape_forms(source: str) -> None:
    assert _campaign_storage_type_violations(ast.parse(source), "booley.flows.sim.rogue", False)


def test_campaign_storage_type_gate_resolves_package_relative_imports() -> None:
    tree = ast.parse("from .campaign.coordinator import CampaignStore\n")

    assert _campaign_storage_type_violations(tree, "booley.flows.sim", True)
    assert not _campaign_storage_type_violations(tree, "booley.flows.sim", False)


@pytest.mark.parametrize("owner", sorted(_CAMPAIGN_STORAGE_OWNERS))
def test_campaign_storage_type_gate_allows_exact_owners(owner: str) -> None:
    tree = ast.parse("from booley.flows.sim.campaign.store import CampaignStore\n")

    violations = _campaign_storage_violations_for_source(tree, owner, False)

    assert violations == []


def test_eda_runtime_spec_compatibility_module_is_deleted() -> None:
    """Prevent callers from drifting back to the former EDA-owned issuance path."""
    assert not (_SOURCE_ROOT / "eda" / "provisioning" / "runtime_spec.py").exists()


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


def test_only_target_adapters_use_low_level_target_mechanics(
    production_source: SourceTreeAnalysis,
) -> None:
    """Keep discovery/selection behind TargetCatalog and the FuseSoC adapter."""
    violations = list(production_source.findings(_TARGET_SCAN))

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
def test_low_level_target_gate_resolves_import_aliases(source: str) -> None:
    assert _target_mechanics_violations(ast.parse(source), "consumer.py")


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
        "import booley.runtime.agent_backend\n",
        "def helper():\n    from booley.runtime import agent_backend\n",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from booley.runtime.agent_backend import AgentBackend\n",
    ],
)
def test_config_runtime_rule_catches_all_static_import_locations(tmp_path, statement):
    root = tmp_path / "booley"
    for package in (root, root / "config", root / "runtime"):
        package.mkdir(exist_ok=True)
        (package / "__init__.py").touch()
    (root / "runtime" / "agent_backend.py").write_text("class AgentBackend: pass\n")
    (root / "config" / "seed.py").write_text(statement)

    problems = evaluate_contract(analyze_imports(root), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)
    report = format_problems(problems)

    assert "D18" in report
    assert "booley.config.seed" in report
    assert "booley.runtime.agent_backend" in report


@pytest.mark.parametrize(
    "statement,target,rule",
    [
        ("import booley.flows.execution\n", "booley.flows.execution", "D19"),
        (
            "def helper():\n    from booley.flows import execution\n",
            "booley.flows.execution",
            "D19",
        ),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    import booley.flows.execution\n",
            "booley.flows.execution",
            "D19",
        ),
        (
            "def helper():\n    from booley.runtime import session_issuance\n",
            "booley.runtime.session_issuance",
            "D20",
        ),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from booley.runtime import issuance_invalidation\n",
            "booley.runtime.issuance_invalidation",
            "D20",
        ),
    ],
)
def test_eda_separation_rules_catch_all_static_import_locations(tmp_path, statement, target, rule):
    root = tmp_path / "booley"
    for package in (root, root / "eda", root / "flows", root / "runtime"):
        package.mkdir(exist_ok=True)
        (package / "__init__.py").touch()
    (root / "flows" / "execution.py").write_text("def run(): pass\n")
    (root / "runtime" / "session_issuance.py").write_text("value = 1\n")
    (root / "runtime" / "issuance_invalidation.py").write_text("value = 1\n")
    (root / "eda" / "policy.py").write_text(statement)

    problems = evaluate_contract(analyze_imports(root), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)
    report = format_problems(problems)

    assert rule in report
    assert "booley.eda.policy" in report
    assert target in report


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


@pytest.mark.parametrize("owner", ["host_diagnostics", "setup.readiness"])
@pytest.mark.parametrize("target", ["doctor", "init_cmd", "booley", "colors", "setup.common"])
@pytest.mark.parametrize("location", ["ordinary", "deferred", "type-only"])
def test_diagnostic_owners_do_not_acquire_command_or_rendering_knowledge(
    tmp_path, owner, target, location
):
    root = tmp_path / "booley"
    for package in (root, root / "harness", root / "harness" / "setup"):
        package.mkdir(exist_ok=True)
        (package / "__init__.py").touch()
    statement = f"import booley.harness.{target}\n"
    if location == "deferred":
        statement = "def helper():\n    " + statement
    elif location == "type-only":
        statement = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    " + statement
    (root / "harness" / (target.replace(".", "/") + ".py")).write_text("value = 1\n")
    (root / "harness" / (owner.replace(".", "/") + ".py")).write_text(statement)
    problems = evaluate_contract(analyze_imports(root), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)
    report = format_problems(problems)
    assert "D26" in report
    assert f"booley.harness.{owner}" in report
    assert f"booley.harness.{target}" in report


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
    consumers = {"booley.mcp.server", "booley.harness.dashboard.model"}
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
