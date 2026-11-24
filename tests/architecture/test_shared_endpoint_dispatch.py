"""Shared endpoint lifecycle code reads declared defaults, not names or attributes."""

from __future__ import annotations

import ast
from pathlib import Path

FLOWS = Path(__file__).resolve().parents[2] / "src" / "booley" / "flows"

# The Target-selection field only the Simulation Flow owns. One constant serves both
# checks below.
# `_prepared_target_selection` stays a string: the guarded bug is a by-name lookup of this
# field, and no public boundary exposes the field name.
PREPARED_TARGET_SELECTION = "_prepared_target_selection"  # no public alias: field read by name

# Hooks and fields the shared lifecycle must call directly: every endpoint
# declares them (EndpointState supplies the neutral default).
# The underscore-prefixed entries stay: the guarded bug is a getattr/hasattr lookup of exactly
# these private hook and field names, and no public boundary exposes them.
DECLARED_NAMES = frozenset(
    {
        "refresh_report_detail",
        "transcript_path",
        "_transcript_path",
        "terminal_progress",
        "_automatic_target_notice",  # no public alias: looked up by name
        "_automatic_target_notice_published",  # no public alias: looked up by name
        "_pre_run_head",  # no public alias: looked up by name
        "_eda_tools",  # no public alias: looked up by name
        "prepare_target_endpoint",
        "_skip_report_detail_refresh",  # no public alias: looked up by name
        "prepare_endpoint_invocation",
        "manifest_names_targets",
    }
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
            for operand in (child.left, *child.comparators)
            # A literal tuple/list/set operand (``name in ("sim", ...)``) is searched too.
            for part in (
                operand.elts if isinstance(operand, ast.Tuple | ast.List | ast.Set) else (operand,)
            )
        )
    ]


def test_shared_endpoint_report_code_has_no_sim_name_checks():
    for module in ("endpoint_reporting.py", "endpoint_session.py"):
        compared = _compares_with_sim(_parse(FLOWS / module))
        assert compared == [], f"{module} compares a name with 'sim': {compared}"

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


# Simulation Record state the shared acceptance code must reach only through
# the declared hooks, never by attribute name.
# `_simulation_record_outcomes` stays: same by-name lookup bug, no public boundary exposes it.
SIMULATION_ACCEPTANCE_NAMES = frozenset(
    {
        "_simulation_record_outcomes",  # no public alias: looked up by name
        "publish_simulation_record_projections",
    }
)


def test_shared_endpoint_code_looks_up_no_declared_or_simulation_names():
    names = DECLARED_NAMES | SIMULATION_ACCEPTANCE_NAMES
    lookups = sorted(
        f"{path.name}:{lookup}"
        for path in FLOWS.glob("endpoint_*.py")
        for lookup in _named_lookups(_parse(path), names)
    )
    assert lookups == [], f"declared hooks or Simulation state looked up by name: {lookups}"


# Simulation-only names the shared preparation may not look up by name: the
# Simulation Flow reaches the lifecycle through its PreparedInvocation.
SIMULATION_PREPARATION_NAMES = frozenset(
    {
        "prepare_simulation_endpoint",
        "run_prepared_simulation",
        PREPARED_TARGET_SELECTION,
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
    "prepared_invocation.py",
    "endpoint_state.py",
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
            f"{module}:{line}: reads {PREPARED_TARGET_SELECTION}"
            for line in _attribute_reads(tree, PREPARED_TARGET_SELECTION)
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
