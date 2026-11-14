"""Architecture guard: the Simulation Record package defines no "campaign" names."""

from __future__ import annotations

import ast
from pathlib import Path

_RECORD_PACKAGE = Path(__file__).parents[2] / "src" / "booley" / "flows" / "sim" / "record"
_FORBIDDEN = "campaign"


def _defined_names(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(line, name)`` for every identifier a module defines."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((node.lineno, node.name))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = node.args
            every_argument = [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ]
            if arguments.vararg is not None:
                every_argument.append(arguments.vararg)
            if arguments.kwarg is not None:
                every_argument.append(arguments.kwarg)
            found.extend((argument.lineno, argument.arg) for argument in every_argument)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            found.append((node.lineno, node.name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found.extend((node.lineno, alias.asname) for alias in node.names if alias.asname)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in targets
            ) and isinstance(node.value, (ast.List, ast.Tuple)):
                found.extend(
                    (element.lineno, element.value)
                    for element in node.value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
    return found


def test_simulation_record_package_defines_no_campaign_names() -> None:
    sources = sorted(_RECORD_PACKAGE.glob("*.py"))
    assert sources, f"no modules found under {_RECORD_PACKAGE}"
    offenders = [
        f"{source.name}:{line} {name}"
        for source in sources
        for line, name in _defined_names(ast.parse(source.read_bytes()))
        if _FORBIDDEN in name.lower()
    ]
    assert not offenders, "Simulation Record package defines campaign names:\n" + "\n".join(
        offenders
    )
