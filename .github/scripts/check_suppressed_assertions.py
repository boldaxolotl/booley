"""Fail closed on suppressed test assertions; no test modules are imported."""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_SUPPRESS = "contextlib.suppress"
_BROAD = {"Exception", "BaseException", "AssertionError"}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    column: int
    scope: str
    rule: str

    def diagnostic(self) -> str:
        return f"{self.path}:{self.line}:{self.column}: {self.rule} ({self.scope})"


def _local_nodes(node: ast.AST):
    """Yield one lexical scope, without descending into deferred bodies."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (*_FUNCTIONS, ast.ClassDef)):
            yield from _local_nodes(child)


def _qualified(node: ast.AST, bindings: dict[str, set[str]]) -> set[str]:
    if isinstance(node, ast.Name):
        return bindings.get(node.id, {node.id})
    if isinstance(node, ast.Attribute):
        return {f"{value}.{node.attr}" for value in (_qualified(node.value, bindings) or {"?"})}
    return set()


def _bound_names(node: ast.AST, nodes: list[ast.AST]) -> set[str]:
    names = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    names.update(n.name for n in nodes if isinstance(n, (*_FUNCTIONS[:2], ast.ClassDef)))
    for item in nodes:
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in item.names)
    if isinstance(node, _FUNCTIONS):
        args = node.args
        parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        names.update(a.arg for a in parameters if a is not None)
    return names


def _bindings(node: ast.AST, parent: dict[str, set[str]]) -> dict[str, set[str]]:
    """Collect scope-wide possible bindings, including late enclosing imports."""
    result = {name: values.copy() for name, values in parent.items()}
    nodes = list(_local_nodes(node))
    for name in _bound_names(node, nodes):
        result[name] = set()
    for item in nodes:
        if isinstance(item, ast.Import):
            for alias in item.names:
                result.setdefault(alias.asname or alias.name.split(".")[0], set()).add(
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        if isinstance(item, ast.ImportFrom):
            for alias in item.names:
                result.setdefault(alias.asname or alias.name, set()).add(
                    f"{item.module}.{alias.name}"
                )
    assignments = [n for n in nodes if isinstance(n, (ast.Assign, ast.AnnAssign))]
    # Monotone propagation reaches a fixed point in at most one pass per assignment.
    for _ in range(len(assignments) + 1):
        for item in assignments:
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            values = _qualified(item.value, result) if item.value is not None else set()
            for target in targets:
                if isinstance(target, ast.Name):
                    result.setdefault(target.id, set()).update(values)
    return result


def _assertion_call(node: ast.Call, bindings: dict[str, set[str]]) -> bool:
    qualified = _qualified(node.func, bindings)
    if qualified & {"subprocess.check_call", "subprocess.check_output"}:
        return False
    names = qualified.copy()
    names.add(
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    )
    return any(_assertion_name(name.split(".")[-1]) for name in names)


def _assertion_name(name: str) -> bool:
    if name == "check_returncode":
        return False
    return (
        name.startswith(("assert_", "_assert_", "check_", "_check_"))
        or name == "fail"
        or (name.startswith("assert") and len(name) > 6 and name[6].isupper())
    )


class Scanner(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.AST) -> None:
        self.path = path
        self.bindings = _bindings(tree, {})
        self.closure_bindings = self.bindings
        self.scope = "<module>"
        self.suppressed = False
        self.direct: set[int] = set()
        self.allowed_references: set[int] = set()
        self.findings: set[Finding] = set()

    def report(self, node: ast.AST, rule: str) -> None:
        self.findings.add(Finding(self.path, node.lineno, node.col_offset + 1, self.scope, rule))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "contextlib" and any(a.name == "*" for a in node.names):
            self.report(node, "unsupported-contextlib-star-import")

    def visit_Assert(self, node: ast.Assert) -> None:
        if self.suppressed:
            self.report(node, "suppressed-assertion")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if node.value is not None and all(isinstance(target, ast.Name) for target in targets):
            self.allowed_references.add(id(node.value))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit_Assign(node)

    def _reference(self, node: ast.Name | ast.Attribute) -> None:
        if (
            _SUPPRESS in _qualified(node, self.bindings)
            and id(node) not in self.allowed_references
        ):
            self.report(node, "unsupported-suppress-reference")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._reference(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._reference(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _SUPPRESS in _qualified(node.func, self.bindings):
            self.allowed_references.add(id(node.func))
            if id(node) not in self.direct:
                self.report(node, "unsupported-suppress-use")
            if any(
                value.split(".")[-1] in _BROAD | {"failureException"}
                for arg in node.args
                for value in _qualified(arg, self.bindings)
            ):
                self.report(node, "broad-suppression")
            if node.keywords or any(
                not isinstance(arg, (ast.Name, ast.Attribute))
                or not _qualified(arg, self.bindings)
                for arg in node.args
            ):
                self.report(node, "unsupported-suppress-arguments")
        if self.suppressed and _assertion_call(node, self.bindings):
            self.report(node, "suppressed-assertion-call")
        self.generic_visit(node)

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        previous = self.suppressed
        for item in node.items:
            expr = item.context_expr
            is_suppress = isinstance(expr, ast.Call) and _SUPPRESS in _qualified(
                expr.func, self.bindings
            )
            if is_suppress:
                self.direct.add(id(expr))
            self.visit(expr)
            if item.optional_vars:
                self.visit(item.optional_vars)
            self.suppressed |= is_suppress
        for statement in node.body:
            self.visit(statement)
        self.suppressed = previous

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def _body_scope(self, node: ast.AST, name: str, body: list[ast.stmt], deferred: bool) -> None:
        previous = self.bindings, self.closure_bindings, self.scope, self.suppressed
        parent = self.closure_bindings if deferred else self.bindings
        self.bindings = _bindings(node, parent)
        self.scope = name if self.scope == "<module>" else f"{self.scope}.{name}"
        if deferred:
            self.closure_bindings = self.bindings
            self.suppressed = False
        for statement in body:
            self.visit(statement)
        self.bindings, self.closure_bindings, self.scope, self.suppressed = previous

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Defaults and decorators execute now; the function body executes later.
        for expr in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if expr is not None:
                self.visit(expr)
        self._body_scope(node, node.name, node.body, deferred=True)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expr in [*node.args.defaults, *node.args.kw_defaults]:
            if expr is not None:
                self.visit(expr)
        previous = self.bindings, self.suppressed
        self.bindings = _bindings(node, self.closure_bindings)
        self.suppressed = False
        self.visit(node.body)
        self.bindings, self.suppressed = previous

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]:
            self.visit(expr)
        self._body_scope(node, node.name, node.body, deferred=False)


def scan_source(source: str, path: str) -> list[Finding]:
    tree = ast.parse(source, filename=path)
    scanner = Scanner(path, tree)
    scanner.visit(tree)
    return sorted(scanner.findings)


def apply_exemptions(findings: list[Finding], entries: object) -> list[Finding]:
    """Each reviewed justification must match exactly one current assertion."""
    if not isinstance(entries, list):
        raise ValueError("exemptions must be a JSON list")
    remaining = set(findings)
    seen: set[Finding] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "line",
            "column",
            "scope",
            "rule",
            "reason",
            "verification",
            "purpose",
        }:
            raise ValueError("exemption must contain an exact finding, reason and verification")
        if any(
            not isinstance(entry[key], str) or not entry[key].strip()
            for key in ("path", "scope", "rule", "reason", "verification")
        ):
            raise ValueError("exemption text must be nonempty")
        if any(type(entry[key]) is not int or entry[key] < 1 for key in ("line", "column")):
            raise ValueError("exemption positions must be positive integers")
        if entry["purpose"] != "intentional-assertion-input":
            raise ValueError("exemption purpose must be intentional-assertion-input")
        finding = Finding(*(entry[key] for key in ("path", "line", "column", "scope", "rule")))
        if finding.rule not in {
            "suppressed-assertion",
            "suppressed-assertion-call",
            "broad-suppression",
        }:
            raise ValueError("only intentional assertion test input can be exempted")
        if finding in seen or finding not in remaining:
            raise ValueError(f"duplicate or stale exemption: {finding.diagnostic()}")
        seen.add(finding)
        remaining.remove(finding)
    return sorted(remaining)


def check_repository(root: Path) -> tuple[int, list[str]]:
    paths = sorted((root / "tests").rglob("*.py"))
    if not paths:
        raise ValueError("no Python test files found")
    findings = []
    for path in paths:
        findings.extend(
            scan_source(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
        )
    manifest = root / ".github/scripts/suppressed_assertion_exemptions.json"
    remaining = apply_exemptions(findings, json.loads(manifest.read_text(encoding="utf-8")))
    return len(paths), [f.diagnostic() for f in remaining]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        count, diagnostics = check_repository(args.repo)
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        print(f"suppressed-assertions: {exc}")
        return 1
    for diagnostic in diagnostics:
        print(diagnostic)
    print(f"suppressed-assertions: checked {count} Python files; {len(diagnostics)} violations")
    return int(bool(diagnostics))


if __name__ == "__main__":
    raise SystemExit(main())
