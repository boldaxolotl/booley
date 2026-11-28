"""Find hand-written bool traps in `src/booley` (#5838).

A bool trap is one boolean expression that tests the same value with
`isinstance(..., bool)` and with `isinstance(..., int)` or `float`, such as
`isinstance(x, bool) or not isinstance(x, int)`. `booley.core.boundary` owns that
guard and is not scanned. The count elsewhere in the package may only shrink:
`tests/ci/test_bool_trap_ratchet.py` caps it.
"""

from __future__ import annotations

import ast
from pathlib import Path

BOUNDARY_MODULE = "core/boundary.py"
_NUMBER_TYPES = {"int", "float"}


def _type_names(node: ast.expr) -> set[str]:
    """Return the builtin names in an `isinstance` type argument (tuple or `|` union)."""
    if isinstance(node, ast.Tuple):
        return set().union(*(_type_names(item) for item in node.elts))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _type_names(node.left) | _type_names(node.right)
    return {node.id} if isinstance(node, ast.Name) else set()


def _isinstance_test(node: ast.expr) -> tuple[str, set[str]] | None:
    """Return (checked value, type names) for `isinstance(v, T)` or `not isinstance(v, T)`."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        and len(node.args) == 2
    ):
        return None
    return ast.dump(node.args[0]), _type_names(node.args[1])


def _operands(node: ast.BoolOp, chained: set[ast.BoolOp]) -> list[ast.expr]:
    """Return *node*'s operands, flattening parenthesized same-operator chains into *chained*."""
    operands: list[ast.expr] = []
    for value in node.values:
        if isinstance(value, ast.BoolOp) and type(value.op) is type(node.op):
            chained.add(value)
            operands.extend(_operands(value, chained))
        else:
            operands.append(value)
    return operands


def bool_traps(source: str, path: str) -> list[str]:
    """Return `"<path>:<line>"` once per value each bool-trap expression in *source* guards."""
    chained: set[ast.BoolOp] = set()
    hits: list[str] = []
    for node in ast.walk(ast.parse(source, filename=path)):  # outer expressions come first
        if not isinstance(node, ast.BoolOp) or node in chained:
            continue
        tests = [test for value in _operands(node, chained) if (test := _isinstance_test(value))]
        bools = {value for value, names in tests if "bool" in names}
        numbers = {
            value for value, names in tests if names & _NUMBER_TYPES and "bool" not in names
        }
        hits.extend(f"{path}:{node.lineno}" for _ in bools & numbers)
    return hits


def package_bool_traps(package: Path) -> list[str]:
    """Return every bool trap under *package*, skipping the boundary module."""
    hits: list[str] = []
    for module in sorted(package.rglob("*.py")):
        relative = module.relative_to(package).as_posix()
        if relative != BOUNDARY_MODULE:
            hits.extend(bool_traps(module.read_text(encoding="utf-8"), relative))
    return hits


def ratchet_violations(sites: list[str], ceiling: int) -> list[str]:
    """Return one message when the bool-trap count differs from *ceiling*, else none."""
    if len(sites) > ceiling:
        return [
            f"{len(sites)} hand-written bool traps exceed the ceiling of {ceiling}; use the "
            "booley.core.boundary helpers (require_int, require_positive_int, "
            "as_positive_int, as_int) instead:\n  " + "\n  ".join(sites)
        ]
    if len(sites) < ceiling:
        return [f"bool traps shrank to {len(sites)}: lower BOOL_TRAP_CEILING to {len(sites)}"]
    return []
