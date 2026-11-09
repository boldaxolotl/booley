"""Shared endpoint lifecycle code reads declared defaults, not names or attributes."""

from __future__ import annotations

import ast
from pathlib import Path

FLOWS = Path(__file__).resolve().parents[2] / "src" / "booley" / "flows"

# Hooks and fields the shared lifecycle must call directly: every endpoint
# declares them (EndpointState supplies the neutral default).
DECLARED_NAMES = frozenset(
    {
        "refresh_report_detail",
        "refresh_campaign_report_detail",
        "transcript_path",
        "_transcript_path",
        "terminal_progress",
        "_automatic_target_notice",
        "_automatic_target_notice_published",
        "_pre_run_head",
        "_eda_tools",
        "prepare_target_endpoint",
        "_skip_report_detail_refresh",
        "record_flow_owned_acceptance",
        "generic_binding_gate_applies",
        "prepare_endpoint_invocation",
        "manifest_names_targets",
    }
)

# Session functions that choose the report root; none may branch on a Flow name.
REPORT_ROOT_FUNCTIONS = (
    "_prepare_endpoint_report_root",
    "_apply_default_endpoint_report_root",
    "_canonicalize_flow_report_root",
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _compares_with_sim(node: ast.AST) -> list[int]:
    """Line numbers of comparisons against the literal Simulation Flow name."""
    return [
        child.lineno
        for child in ast.walk(node)
        if isinstance(child, ast.Compare)
        and any(
            isinstance(part, ast.Constant) and part.value == "sim"
            for part in (child.left, *child.comparators)
        )
    ]


def test_shared_endpoint_report_code_has_no_sim_name_checks():
    reporting = _compares_with_sim(_parse(FLOWS / "endpoint_reporting.py"))
    assert reporting == [], f"endpoint_reporting.py compares a name with 'sim': {reporting}"

    session = _parse(FLOWS / "endpoint_session.py")
    functions = {
        node.name: node for node in ast.walk(session) if isinstance(node, ast.FunctionDef)
    }
    for name in REPORT_ROOT_FUNCTIONS:
        assert name in functions, f"endpoint_session.{name} is missing"
        assert _compares_with_sim(functions[name]) == [], f"{name} compares a name with 'sim'"

    # The Flow layer stays below the Specialist layer.
    importing = sorted(
        f"{path.relative_to(FLOWS)}:{node.lineno}"
        for path in FLOWS.rglob("*.py")
        for node in ast.walk(_parse(path))
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("booley.specialists")
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("booley.specialists") for alias in node.names)
        )
    )
    assert importing == [], f"booley.flows imports booley.specialists: {importing}"


def test_shared_endpoint_code_calls_declared_hooks():
    lookups = sorted(
        f"{path.name}:{node.lineno} {node.func.id}({node.args[1].value!r})"
        for path in FLOWS.glob("endpoint_*.py")
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr"}
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in DECLARED_NAMES
    )
    assert lookups == [], f"declared hooks looked up by name: {lookups}"


# Simulation Campaign state the shared acceptance code must reach only through
# the declared hooks, never by attribute name.
SIMULATION_ACCEPTANCE_NAMES = frozenset(
    {
        "_simulation_campaign_outcomes",
        "_simulation_acceptance_outcomes",
        "record_campaign_acceptance",
        "committed",
        "transaction_id",
    }
)


def test_shared_acceptance_code_has_no_simulation_branches():
    comparisons = _compares_with_sim(_parse(FLOWS / "endpoint_acceptance.py"))
    assert comparisons == [], f"endpoint_acceptance.py compares a name with 'sim': {comparisons}"

    lookups = sorted(
        f"{path.name}:{node.lineno} {node.func.id}({node.args[1].value!r})"
        for path in FLOWS.glob("endpoint_*.py")
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr"}
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in SIMULATION_ACCEPTANCE_NAMES
    )
    assert lookups == [], f"Simulation acceptance state looked up by name: {lookups}"


# Simulation-only names the shared preparation may not look up by name: the
# Simulation Flow reaches the lifecycle through its PreparedInvocation.
SIMULATION_PREPARATION_NAMES = frozenset(
    {
        "prepare_simulation_endpoint",
        "run_prepared_simulation",
        "report_candidate_workloads",
        "_prepared_target_selection",
        "resume_from",
        "mode",
        "coverage",
        "targets",
    }
)

# Modules shared by every endpoint kind; they must not read the Target-selection field.
SHARED_PREPARATION_MODULES = (
    "endpoint_session.py",
    "endpoint_admission.py",
    "endpoint_report_criteria.py",
    "prepared_invocation.py",
)

# BuiltinFlow methods that prepare an endpoint for every built-in Flow.
BASE_PREPARATION_METHODS = ("prepare_target_endpoint", "prepare_endpoint_invocation")


def _named_lookups(node: ast.AST, names: frozenset[str]) -> list[str]:
    """``getattr``/``hasattr`` calls under *node* whose attribute name is in *names*."""
    return [
        f"line {child.lineno}: {child.func.id}({child.args[1].value!r})"
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id in {"getattr", "hasattr"}
        and len(child.args) >= 2
        and isinstance(child.args[1], ast.Constant)
        and child.args[1].value in names
    ]


def _attribute_reads(node: ast.AST, name: str) -> list[int]:
    """Line numbers of ``<x>.name`` attribute accesses under *node*."""
    return [
        child.lineno
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute) and child.attr == name
    ]


def _instance_dict_membership(node: ast.AST) -> list[int]:
    """Line numbers of ``"name" in <x>.__dict__`` tests (per-instance monkeypatch detection)."""
    return [
        child.lineno
        for child in ast.walk(node)
        if isinstance(child, ast.Compare)
        and any(
            isinstance(comparator, ast.Attribute) and comparator.attr == "__dict__"
            for comparator in child.comparators
        )
    ]


def shared_preparation_violations(filename: str, scope: ast.AST) -> list[str]:
    """Simulation dispatch found in *scope*, a shared module or one shared method."""
    found = [f"{filename}:{line}: compares with 'sim'" for line in _compares_with_sim(scope)]
    found += [
        f"{filename}:{lookup}: Simulation name looked up"
        for lookup in _named_lookups(scope, SIMULATION_PREPARATION_NAMES)
    ]
    return found


def test_shared_preparation_has_no_simulation_dispatch():
    violations: list[str] = []
    for module in SHARED_PREPARATION_MODULES:
        tree = _parse(FLOWS / module)
        violations += shared_preparation_violations(module, tree)
        violations += [
            f"{module}:{line}: reads _prepared_target_selection"
            for line in _attribute_reads(tree, "_prepared_target_selection")
        ]

    # BuiltinFlow owns the Target-selection field, so only its dispatch is checked.
    base = _parse(FLOWS / "base.py")
    methods = {node.name: node for node in ast.walk(base) if isinstance(node, ast.FunctionDef)}
    for name in BASE_PREPARATION_METHODS:
        assert name in methods, f"base.{name} is missing"
        violations += shared_preparation_violations(f"base.{name}", methods[name])

    # A per-instance monkeypatch probe is not a seam: the run goes through the invocation.
    for path in FLOWS.rglob("*.py"):
        violations += [
            f"{path.relative_to(FLOWS)}:{line}: tests an instance __dict__"
            for line in _instance_dict_membership(_parse(path))
        ]
    assert violations == [], f"shared preparation dispatches on Simulation: {violations}"
