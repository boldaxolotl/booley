"""Guard: test-started children with a literal timeout run through the shared helper.

``subprocess.run(timeout=...)`` kills only the direct child, raises a bare
``TimeoutExpired`` with no elapsed time, and on Windows then waits without limit
for pipes a surviving descendant holds (issue #3115). Tests start such children
through ``tests.subprocess_budget.run_bounded_child``, which kills the whole
process tree and fails the test with the command, the limit, the elapsed time
and the partial output. A call that cannot use it keeps its literal timeout with
a one-line ``# Kept on <call>: <reason>`` comment directly above its statement.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: Path prefixes (relative to ``tests/``) still owned by an open sibling sub-issue
#: of the bounded-children audit. Each sibling deletes its entries when it lands.
SIBLING_OWNED_PREFIXES: dict[str, str] = {
    "ci/": "#3416",
    "harness/": "#3415",
    "dev_support/": "#3417",
    "architecture/": "#3417",
    "docs/": "#3417",
    "docker/": "#3419",
    "smoke/": "#3419",
    "docker_cli_guard.py": "#3419",
    "sidecar_image_helpers.py": "#3419",
}

#: ``subprocess`` entry points whose ``timeout=`` keyword the audit covers.
_SUBPROCESS_CALLS = frozenset({"run", "check_output", "check_call", "call", "Popen"})

#: The shared budget constant: a timeout spelled with it is not a literal.
_SHARED_BUDGET_NAME = "SUBPROCESS_TIMEOUT_S"

_KEEP_PREFIX = "# Kept on "


class _TimeoutScanner(ast.NodeVisitor):
    """Collect ``file:line`` sites whose literal timeout lacks a keep comment."""

    def __init__(self, relative_path: str, source: str, tree: ast.Module) -> None:
        self._relative_path = relative_path
        self._lines = source.splitlines()
        self._module_numeric: dict[str, ast.expr] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    self._module_numeric[target.id] = node.value
        self._statements: list[ast.stmt] = []
        self._functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.offenders: list[str] = []

    def _is_numeric(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> bool:
        """Return True when *node* is a number or an expression of numbers."""
        if isinstance(node, ast.Constant):
            return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        if isinstance(node, ast.UnaryOp):
            return self._is_numeric(node.operand, seen)
        if isinstance(node, ast.BinOp):
            return self._is_numeric(node.left, seen) and self._is_numeric(node.right, seen)
        if isinstance(node, ast.IfExp):
            return self._is_numeric(node.body, seen) and self._is_numeric(node.orelse, seen)
        if isinstance(node, ast.Name):
            return self._name_is_numeric(node.id, seen)
        return False

    def _name_is_numeric(self, name: str, seen: frozenset[str]) -> bool:
        """Return True for a module numeric constant or a numeric parameter default."""
        if name == _SHARED_BUDGET_NAME or name in seen:
            return False
        if self._functions:
            args = self._functions[-1].args
            positional = [*args.posonlyargs, *args.args]
            defaults: dict[str, ast.expr] = dict(
                zip(
                    [arg.arg for arg in positional[len(positional) - len(args.defaults) :]],
                    args.defaults,
                    strict=True,
                )
            )
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
                if default is not None:
                    defaults[arg.arg] = default
            if name in defaults:
                return self._is_numeric(defaults[name], seen | {name})
        bound = self._module_numeric.get(name)
        return bound is not None and self._is_numeric(bound, seen | {name})

    def _is_audited_call(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Attribute):
            if func.attr == "communicate":
                return True
            return (
                func.attr in _SUBPROCESS_CALLS
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            )
        return isinstance(func, ast.Name) and func.id == "Popen"

    def _has_keep_comment(self, statement: ast.stmt, call: ast.Call) -> bool:
        for anchor in {call.lineno, statement.lineno}:
            if anchor >= 2 and self._lines[anchor - 2].strip().startswith(_KEEP_PREFIX):
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node)
        self.generic_visit(node)
        self._functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._functions.append(node)
        self.generic_visit(node)
        self._functions.pop()

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.stmt):
            self._statements.append(node)
            super().generic_visit(node)
            self._statements.pop()
        else:
            super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_audited_call(node) and self._statements:
            for keyword in node.keywords:
                if (
                    keyword.arg == "timeout"
                    and self._is_numeric(keyword.value)
                    and not self._has_keep_comment(self._statements[-1], node)
                ):
                    self.offenders.append(f"{self._relative_path}:{node.lineno}")
        self.generic_visit(node)


def _literal_timeout_offenders() -> list[str]:
    """Return every unlabeled literal-timeout child call under ``tests/``."""
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        relative = path.relative_to(TESTS_DIR).as_posix()
        if path == Path(__file__).resolve() or relative.startswith(tuple(SIBLING_OWNED_PREFIXES)):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        scanner = _TimeoutScanner(f"tests/{relative}", source, tree)
        scanner.visit(tree)
        offenders.extend(scanner.offenders)
    return offenders


def test_test_children_use_bounded_helper() -> None:
    """Every literal-timeout child uses ``run_bounded_child`` or carries a keep reason."""
    offenders = _literal_timeout_offenders()
    assert not offenders, (
        "Literal-timeout child processes must run through "
        "tests.subprocess_budget.run_bounded_child, or carry a one-line "
        "'# Kept on <call>: <reason>' comment directly above the call:\n  "
        + "\n  ".join(offenders)
    )
