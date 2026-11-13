"""The Simulation Flow mixins declare only host members SimulateFlow provides (#3808)."""

from __future__ import annotations

import ast
import inspect
from types import ModuleType

from booley.flows.sim import elab_only, standalone, test_selection
from booley.flows.sim.flow import SimulateFlow


def _declared_host_members(module: ModuleType, class_name: str) -> set[str]:
    """Names a mixin declares in its class-level ``if TYPE_CHECKING:`` block."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for stmt in node.body:
            if not (isinstance(stmt, ast.If) and ast.unparse(stmt.test) == "TYPE_CHECKING"):
                continue
            for member in stmt.body:
                if isinstance(member, ast.FunctionDef):
                    names.add(member.name)
                elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    names.add(member.target.id)
    return names


def test_mixin_host_declarations_are_provided_by_simulate_flow() -> None:
    """Pyright cannot see a declared member the host dropped; this does."""
    flow = SimulateFlow()
    flow.begin_invocation()
    for module, class_name in (
        (elab_only, "ElabOnlyMixin"),
        (standalone, "StandaloneMixin"),
        (test_selection, "SimulationTestSelectionMixin"),
    ):
        declared = _declared_host_members(module, class_name)
        assert declared, f"{class_name} declares no host members under TYPE_CHECKING"
        mixin = getattr(module, class_name)
        for name in declared:
            assert name not in vars(mixin), (class_name, name)
            assert hasattr(type(flow), name) or name in vars(flow), (class_name, name)
