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
