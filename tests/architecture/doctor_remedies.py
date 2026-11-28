"""Static scan of Doctor FAIL and WARN emission sites against the remedy rule.

``scan_emission_sites`` parses Booley's source and finds every place that reports a
FAIL or WARN: calls of the reporter, of the findings accumulators, of constructors given a
``*Severity.FAIL`` or ``.WARN``, and of literal FAIL or WARN status lines. A sink handed to
another function as a value (a ``_fail`` parameter, a factory's returned check) is followed
into that function, and a helper that forwards its own parameters as the message or fix is
expanded at each call with the arguments bound. ``guard_problems`` compares the result with
the reviewed lists the test module holds.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# The one action-verb allowlist: a remedy clause starts with one of these.
REMEDY_VERBS = frozenset(
    [
        "run",
        "rerun",
        "add",
        "remove",
        "delete",
        "set",
        "rename",
        "install",
        "restore",
        "wait",
        "ask",
        "free",
        "fix",
        "waive",
        "start",
        "rebuild",
        "recreate",
        "move",
        "replace",
        "rewrite",
        "pin",
    ]
)

Verdict = Literal["pass", "violation", "unresolved"]

_HOLE = "\x00"
_QUOTED = re.compile(r"`([^`]+)`|(?<!\w)'([^']+)'(?!\w)")
_CLAUSE = re.compile(r"[.;:,()\n]|\s+(?:or|and|then)\s+|\s+[—-]\s+")
_FORMAT_FIELD = re.compile(r"\{[^{}]*\}")
_BASE_ATTRIBUTES = {"fail_": "fail", "fail": "fail", "warn_": "warn", "warn": "warn"}
_NOT_SINK_RECEIVERS = frozenset({"pytest", "warnings"})
# The scan keys on these Doctor names; `_Reporter` and `_warning_sink` have no public alias.
_REPORTER_CLASS = "_Reporter"  # no public alias: scan keys on the name
_WARNING_SINK = "_warning_sink"  # no public alias: scan keys on the name
_MAX_DEPTH = 6
_MAX_TEXTS = 64


def names_a_remedy(text: str) -> bool:
    """Whether *text* names a remedy: a quoted or backticked span, or a clause led by a verb."""
    for match in _QUOTED.finditer(text):
        span = match.group(1) or match.group(2)
        if any(not char.isspace() and char != _HOLE for char in span):
            return True
    for clause in _CLAUSE.split(text):
        words = clause.split()
        if words and words[0].lower().replace("re-run", "rerun") in REMEDY_VERBS:
            return True
    return False


@dataclass(frozen=True)
class EmissionSite:
    """One FAIL or WARN emission: its stable key, location, and verdict."""

    key: str
    path: str
    line: int
    verdict: Verdict
    form: str


@dataclass(eq=False)
class _Func:
    node: ast.AST
    module: _Module
    parent: _Func | None
    owner_class: str | None
    qualname: str
    params: list[str] = field(default_factory=list[str])
    defaults: dict[str, ast.expr] = field(default_factory=dict[str, ast.expr])
    calls: list[ast.Call] = field(default_factory=list[ast.Call])
    assigns: dict[str, list[ast.expr]] = field(default_factory=dict[str, list[ast.expr]])
    nested: dict[str, _Func] = field(default_factory=dict[str, "_Func"])
    returns: list[ast.expr] = field(default_factory=list[ast.expr])


@dataclass(eq=False)
class _Module:
    rel: str
    dotted: str
    tree: ast.Module
    top: _Func = field(init=False)
    methods: dict[tuple[str, str], _Func] = field(default_factory=dict[tuple[str, str], "_Func"])
    imports: dict[str, tuple[str, str | None]] = field(
        default_factory=dict[str, tuple[str, str | None]]
    )


@dataclass(frozen=True)
class _SinkValue:
    severity: str | None
    check_id: ast.expr | str | None = None
    derived: _Func | None = None


@dataclass(frozen=True)
class _Emit:
    severity: str
    check_id: ast.expr | str | None
    message: ast.expr | None
    fix: ast.expr | None
    form: str


@dataclass(frozen=True)
class _Bound:
    func: _Func
    call: ast.Call


_Env = Mapping[str, list[str] | None]
_NO_SINKS: frozenset[_SinkValue] = frozenset()


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> list[str]:
    arguments = node.args
    return [item.arg for item in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]


def _defaults(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> dict[str, ast.expr]:
    arguments = node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    found = {
        item.arg: value
        for item, value in zip(
            positional[len(positional) - len(arguments.defaults) :],
            arguments.defaults,
            strict=True,
        )
    }
    for item, kw_value in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        if kw_value is not None:
            found[item.arg] = kw_value
    return found


class _Collector:
    """Fill a module's function table: calls, assignments, returns, nested functions."""

    def __init__(self, module: _Module, node_funcs: dict[ast.AST, _Func]) -> None:
        self.module = module
        self.node_funcs = node_funcs
        module.top = _Func(module.tree, module, None, None, "<module>")
        node_funcs[module.tree] = module.top

    def run(self) -> None:
        self._visit(self.module.tree, self.module.top, None)

    def _visit(self, node: ast.AST, func: _Func, owner: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            self._scan_node(child, func, owner)

    def _scan_node(self, node: ast.AST, func: _Func, owner: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._enter_function(node, func, owner)
            return
        if isinstance(node, ast.Lambda):
            new = _Func(node, self.module, func, owner, "<lambda>", _params(node), _defaults(node))
            new.returns.append(node.body)
            self.node_funcs[node] = new
            self._visit(node, new, owner)
            return
        if isinstance(node, ast.ClassDef):
            self._visit(node, func, node.name)
            self._add_field_constructor(node, func)
            return
        self._note_site(node, func)
        self._visit(node, func, owner)

    def _add_field_constructor(self, node: ast.ClassDef, func: _Func) -> None:
        """Give a class without ``__init__`` one taking its annotated fields (a dataclass)."""
        fields = [
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        ]
        if fields and (node.name, "__init__") not in self.module.methods:
            qualname = f"{node.name}.__init__"
            init = _Func(node, self.module, func, node.name, qualname, ["self", *fields])
            self.module.methods[(node.name, "__init__")] = init

    def _enter_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, func: _Func, owner: str | None
    ) -> None:
        in_class = owner is not None and func is self.module.top
        qualname = f"{owner}.{node.name}" if in_class else node.name
        if func is not self.module.top:
            qualname = f"{func.qualname}.{node.name}"
        new = _Func(node, self.module, func, owner, qualname, _params(node), _defaults(node))
        self.node_funcs[node] = new
        if in_class and owner is not None:
            self.module.methods[(owner, node.name)] = new
        else:
            func.nested[node.name] = new
        self._visit(node, new, owner)

    def _note_site(self, node: ast.AST, func: _Func) -> None:
        if isinstance(node, ast.Call):
            func.calls.append(node)
        elif isinstance(node, ast.Return) and node.value is not None:
            func.returns.append(node.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    func.assigns.setdefault(target.id, []).append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            if isinstance(node.target, ast.Name) and node.value is not None:
                func.assigns.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.ImportFrom):
            self._record_from_import(node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    self.module.imports[alias.asname] = (alias.name, None)

    def _record_from_import(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            package = self.module.dotted.split(".")
            if not self.module.rel.endswith("__init__.py"):
                package = package[:-1]
            package = package[: len(package) - (node.level - 1)]
            base = ".".join([*package, *([node.module] if node.module else [])])
        for alias in node.names:
            self.module.imports[alias.asname or alias.name] = (base, alias.name)


def _const_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _argument(call: ast.Call, position: int, *names: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg in names:
            return keyword.value
    return call.args[position] if len(call.args) > position else None


def _is_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _severity_member(node: ast.expr, suffix: str) -> str | None:
    """``fail``/``warn`` for ``<Name or attr ending in suffix>.FAIL|WARN``, else None."""
    if not isinstance(node, ast.Attribute) or node.attr not in {"FAIL", "WARN"}:
        return None
    owner = node.value
    owner_name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
    return node.attr.lower() if str(owner_name).endswith(suffix) else None


def _unwrap_warning(
    message: ast.expr | None,
) -> tuple[ast.expr | str | None, ast.expr | None]:
    """Split ``warning(check_id, text)`` into its parts; any other message passes through."""
    if isinstance(message, ast.Call) and _is_name(message.func, "warning"):
        return _argument(message, 0, "check_id"), _argument(message, 1, "message")
    return None, message


def _strip_str(expr: ast.expr | None) -> ast.expr | None:
    if isinstance(expr, ast.Call) and _is_name(expr.func, "str") and len(expr.args) == 1:
        return expr.args[0]
    return expr


class _Analysis:
    """Cross-module facts: sink parameters, forwarding helpers, and the resulting sites."""

    def __init__(self, root: Path, files: Iterable[Path]) -> None:
        self.node_funcs: dict[ast.AST, _Func] = {}
        self.modules: dict[str, _Module] = {}
        self.funcs: list[_Func] = []
        for path in sorted(files):
            rel = path.relative_to(root).as_posix()
            dotted = ".".join(["booley", *rel.removesuffix(".py").split("/")])
            dotted = dotted.removesuffix(".__init__")
            module = _Module(rel, dotted, ast.parse(path.read_bytes(), filename=str(path)))
            _Collector(module, self.node_funcs).run()
            self.modules[dotted] = module
        self.funcs = list(self.node_funcs.values())
        self.params: dict[_Func, dict[str, set[_SinkValue]]] = {}
        self.derived: dict[_Func, set[str]] = {}
        self.expanded: set[_Func] = set()
        self._solve()

    # -- name and callee resolution -------------------------------------------------

    def callee(self, expr: ast.expr, func: _Func) -> _Func | None:
        """The ``src`` function *expr* names, or None when it is a parameter or unknown."""
        if isinstance(expr, ast.Name):
            return self._named_function(expr.id, func)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            owner = expr.value.id
            if owner in {"self", "cls"}:
                scope: _Func | None = func
                while scope is not None and scope.owner_class is None:
                    scope = scope.parent
                owner_class = scope.owner_class if scope is not None else None
                if owner_class is None:
                    return None
                return func.module.methods.get((owner_class, expr.attr))
            imported = func.module.imports.get(owner)
            if imported is not None:
                target = self._module_of(*imported)
                return target.top.nested.get(expr.attr) if target else None
        return None

    def _named_function(self, name: str, func: _Func) -> _Func | None:
        scope: _Func | None = func
        while scope is not None:
            if name in scope.params:
                return None
            if name in scope.nested:
                return scope.nested[name]
            scope = scope.parent
        constructor = func.module.methods.get((name, "__init__"))
        if constructor is not None:
            return constructor
        imported = func.module.imports.get(name)
        if imported is None or imported[1] is None:
            return None
        target = self.modules.get(imported[0])
        if target is None:
            return None
        return target.top.nested.get(imported[1]) or target.methods.get((imported[1], "__init__"))

    def _module_of(self, dotted: str, name: str | None) -> _Module | None:
        return self.modules.get(f"{dotted}.{name}" if name else dotted)

    # -- sink values ----------------------------------------------------------------

    def sink_of(  # noqa: PLR0911 - one return per expression kind
        self, expr: ast.expr, func: _Func, depth: int = 0
    ) -> frozenset[_SinkValue]:
        """The sink values *expr* can hold: bound reporter methods, sink parameters, helpers."""
        if depth > _MAX_DEPTH:
            return _NO_SINKS
        if isinstance(expr, ast.Attribute):
            severity = _base_severity(expr)
            if severity is not None:
                return frozenset({_SinkValue(severity)})
            return self._derived_value(self.callee(expr, func))
        if isinstance(expr, ast.Name):
            return self._name_sink(expr, func, depth)
        if isinstance(expr, ast.Call):
            return self._call_sink(expr, func, depth)
        if isinstance(expr, ast.Lambda):
            return self._derived_value(self.node_funcs.get(expr))
        if isinstance(expr, ast.IfExp):
            return self.sink_of(expr.body, func, depth + 1) | self.sink_of(
                expr.orelse, func, depth + 1
            )
        if isinstance(expr, ast.BoolOp):
            found: frozenset[_SinkValue] = _NO_SINKS
            for value in expr.values:
                found |= self.sink_of(value, func, depth + 1)
            return found
        return _NO_SINKS

    def _derived_value(self, callee: _Func | None) -> frozenset[_SinkValue]:
        if callee is not None and callee in self.derived:
            return frozenset({_SinkValue(None, None, callee)})
        return _NO_SINKS

    def _name_sink(self, expr: ast.Name, func: _Func, depth: int) -> frozenset[_SinkValue]:
        scope: _Func | None = func
        while scope is not None:
            found: frozenset[_SinkValue] = _NO_SINKS
            if expr.id not in scope.params and expr.id not in scope.assigns:
                scope = scope.parent
                continue
            for value in scope.assigns.get(expr.id, ()):
                found |= self.sink_of(value, scope, depth + 1)
            if expr.id in scope.params and not found:
                # A parameter rebound in the body (``_warn = _warning_sink(_warn, ...)``) is
                # the rebound sink; counting the original too would double every call.
                found = frozenset(self.params.get(scope, {}).get(expr.id, ()))
            return found
        return self._derived_value(self.callee(expr, func))

    def _call_sink(self, expr: ast.Call, func: _Func, depth: int) -> frozenset[_SinkValue]:
        if _is_name(expr.func, _WARNING_SINK):
            return frozenset({_SinkValue("warn", _argument(expr, 1, "check_id"))})
        callee = self.callee(expr.func, func)
        found: frozenset[_SinkValue] = _NO_SINKS
        if callee is not None:
            for value in callee.returns:
                found |= self.sink_of(value, callee, depth + 1)
        return found

    # -- emissions ------------------------------------------------------------------

    def emissions(self, call: ast.Call, func: _Func) -> list[_Emit | _Bound]:
        """The emissions one call performs: direct ones and helper calls to expand."""
        special = self._special_emission(call, func)
        if special is not None:
            return [special]
        found: list[_Emit | _Bound] = []
        for sink in self.sink_of(call.func, func):
            if sink.derived is not None:
                found.append(_Bound(sink.derived, call))
                continue
            check_id, message = _unwrap_warning(_argument(call, 0, "msg", "message"))
            check_id = check_id or sink.check_id or _argument(call, 99, "check_id")
            assert sink.severity is not None
            found.append(
                _Emit(sink.severity, check_id, message, _argument(call, 1, "fix"), "sink")
            )
        return found

    def _special_emission(self, call: ast.Call, func: _Func) -> _Emit | None:
        first = _argument(call, 0, "severity", "status")
        if first is None:
            return None
        severity = _severity_member(first, "Severity")
        if severity is not None:
            check_id = _argument(call, 3, "check_id")
            return _Emit(
                severity,
                check_id,
                _argument(call, 1, "message"),
                _argument(call, 2, "fix"),
                "ctor",
            )
        status = _severity_member(first, "LineStatus")
        named = (
            call.func.attr
            if isinstance(call.func, ast.Attribute)
            else getattr(call.func, "id", "")
        )
        if status is not None and named == "status_line" and not self._in_reporter(func):
            return _Emit(status, None, _argument(call, 1, "message"), None, "status_line")
        return None

    @staticmethod
    def _in_reporter(func: _Func) -> bool:
        scope: _Func | None = func
        while scope is not None:
            if scope.owner_class == _REPORTER_CLASS:
                return True
            scope = scope.parent
        return False

    # -- fixed point ----------------------------------------------------------------

    def _solve(self) -> None:
        for _ in range(len(self.funcs) + 1):
            changed = False
            for func in self.funcs:
                for call in func.calls:
                    changed |= self._spread_taint(call, func)
            for func in self.funcs:
                forwarded = self._forwarded_params(func)
                if forwarded - self.derived.get(func, set()):
                    self.derived.setdefault(func, set()).update(forwarded)
                    changed = True
            if not changed:
                return

    def _spread_taint(self, call: ast.Call, func: _Func) -> bool:
        """Mark the parameters of *call*'s callee that receive a sink value."""
        if _is_name(call.func, _WARNING_SINK):
            return False
        callee = self.callee(call.func, func)
        if callee is None:
            return False
        changed = False
        for name, argument in _bind_arguments(callee, call):
            sinks = self.sink_of(argument, func)
            known = self.params.setdefault(callee, {}).setdefault(name, set())
            if not sinks <= known:
                known |= sinks
                changed = True
        return changed

    def _forwarded_params(self, func: _Func) -> set[str]:
        """The parameters *func* hands on as a message or fix."""
        names: set[str] = set()
        for call in func.calls:
            for item in self.emissions(call, func):
                if isinstance(item, _Bound):
                    inner = self.derived.get(item.func, set())
                    expressions = [
                        a for n, a in _bind_arguments(item.func, item.call) if n in inner
                    ]
                elif item.form == "status_line":
                    continue
                else:
                    expressions = [item.message, item.fix]
                for expr in expressions:
                    stripped = _strip_str(expr)
                    if isinstance(stripped, ast.Name) and stripped.id in func.params:
                        names.add(stripped.id)
        return names - {"self", "cls"}

    # -- sites ----------------------------------------------------------------------

    def sites(self) -> list[EmissionSite]:
        found: list[EmissionSite] = []
        for func in self.funcs:
            if func not in self.derived:
                found += self._direct_sites(func)
        for func in self.derived:
            # A forwarding helper nothing calls by name keeps its own sites, so none is lost;
            # the reporter's and accumulator's own methods are the sinks themselves.
            name = getattr(func.node, "name", "")
            if func not in self.expanded and name not in _BASE_ATTRIBUTES:
                found += self._direct_sites(func)
        return found

    def _direct_sites(self, func: _Func) -> list[EmissionSite]:
        found: list[EmissionSite] = []
        for call in func.calls:
            for item in self.emissions(call, func):
                for emit, scope, env in self._expand(item, func, {}, 0):
                    found.append(self._site(emit, scope, env, func, call))
        return found

    def _expand(
        self, item: _Emit | _Bound, func: _Func, env: _Env, depth: int
    ) -> Iterator[tuple[_Emit, _Func, _Env]]:
        if isinstance(item, _Emit):
            yield item, func, env
            return
        if depth >= _MAX_DEPTH:
            return
        self.expanded.add(item.func)
        inner = self._bind_call(item.func, item.call, func, env)
        for call in item.func.calls:
            for nested in self.emissions(call, item.func):
                yield from self._expand(nested, item.func, inner, depth + 1)

    def _bind_call(self, callee: _Func, call: ast.Call, scope: _Func, env: _Env) -> _Env:
        bound: dict[str, list[str] | None] = {}
        for name, argument in _bind_arguments(callee, call):
            bound[name] = self.resolve(argument, scope, env)
        for name, default in callee.defaults.items():
            if name not in bound:
                bound[name] = self.resolve(default, callee, {})
        return bound

    def _site(
        self, emit: _Emit, scope: _Func, env: _Env, caller: _Func, call: ast.Call
    ) -> EmissionSite:
        messages = self.resolve(emit.message, scope, env) if emit.message is not None else None
        fixes = [""] if emit.fix is None else self.resolve(emit.fix, scope, env)
        if fixes is not None and fixes != [""] and all(names_a_remedy(text) for text in fixes):
            verdict: Verdict = "pass"
        elif messages is not None and all(names_a_remedy(text) for text in messages):
            verdict = "pass"
        elif fixes is None or messages is None:
            verdict = "unresolved"
        else:
            verdict = "violation"
        identity = self._check_id(emit.check_id, scope, env)
        head = (messages[0].split(_HOLE)[0].strip()[:40] if messages else "") or (
            ast.unparse(emit.message)[:40] if emit.message is not None else ""
        )
        key = f'{emit.severity} {identity or "-"} {caller.module.rel}::{caller.qualname} "{head}"'
        return EmissionSite(key, caller.module.rel, call.lineno, verdict, emit.form)

    def _check_id(self, check_id: ast.expr | str | None, scope: _Func, env: _Env) -> str | None:
        if isinstance(check_id, str) or check_id is None:
            return check_id
        texts = self.resolve(check_id, scope, env)
        return texts[0] if texts and len(set(texts)) == 1 and _HOLE not in texts[0] else None

    # -- text resolution --------------------------------------------------------------

    def resolve(
        self, expr: ast.expr | str | None, scope: _Func, env: _Env, depth: int = 0
    ) -> list[str] | None:
        """Every text *expr* can produce, or None when the scan cannot see the value."""
        if isinstance(expr, str):
            return [expr]
        if expr is None or depth > _MAX_DEPTH:
            return None
        texts = self._resolve_node(expr, scope, env, depth)
        return texts if texts is None or len(texts) <= _MAX_TEXTS else None

    def _resolve_node(  # noqa: PLR0911 - one return per expression kind
        self, expr: ast.expr, scope: _Func, env: _Env, depth: int
    ) -> list[str] | None:
        text = _const_str(expr)
        if text is not None:
            return [text]
        if isinstance(expr, ast.JoinedStr):
            return self._join_values(expr.values, scope, env, depth)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return self._join_values([expr.left, expr.right], scope, env, depth)
        if isinstance(expr, ast.IfExp):
            return self._union([expr.body, expr.orelse], scope, env, depth)
        if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
            return self._union(expr.values, scope, env, depth)
        if isinstance(expr, ast.Name):
            return self._resolve_name(expr, scope, env, depth)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            return self._resolve_constant(expr, scope, depth)
        if isinstance(expr, ast.Call):
            return self._resolve_call(expr, scope, env, depth)
        return None

    def _union(
        self, values: list[ast.expr], scope: _Func, env: _Env, depth: int
    ) -> list[str] | None:
        found: list[str] = []
        for value in values:
            texts = self.resolve(value, scope, env, depth + 1)
            if texts is None:
                return None
            found += texts
        return found

    def _join_values(
        self, parts: list[ast.expr], scope: _Func, env: _Env, depth: int
    ) -> list[str] | None:
        """Concatenate parts; a part that cannot be resolved becomes an opaque hole."""
        found = [""]
        for part in parts:
            value = part.value if isinstance(part, ast.FormattedValue) else part
            texts = self.resolve(value, scope, env, depth + 1) or [_HOLE]
            found = [head + tail for head in found for tail in texts]
            if len(found) > _MAX_TEXTS:
                return None
        return found

    def _resolve_name(
        self, expr: ast.Name, scope: _Func, env: _Env, depth: int
    ) -> list[str] | None:
        if expr.id in env:
            return env[expr.id]
        current: _Func | None = scope
        while current is not None:
            if expr.id in current.params:
                return None
            if expr.id in current.assigns:
                return self._union(current.assigns[expr.id], current, env, depth)
            current = current.parent
        imported = scope.module.imports.get(expr.id)
        target = self.modules.get(imported[0]) if imported and imported[1] else None
        if imported is not None and imported[1] and target is not None:
            values = target.top.assigns.get(imported[1])
            return self._union(values, target.top, {}, depth) if values else None
        return None

    def _resolve_constant(self, expr: ast.Attribute, scope: _Func, depth: int) -> list[str] | None:
        assert isinstance(expr.value, ast.Name)
        imported = scope.module.imports.get(expr.value.id)
        target = self._module_of(*imported) if imported else None
        values = target.top.assigns.get(expr.attr) if target else None
        return self._union(values, target.top, {}, depth) if values and target else None

    def _resolve_call(
        self, expr: ast.Call, scope: _Func, env: _Env, depth: int
    ) -> list[str] | None:
        if _is_name(expr.func, "str") and expr.args:
            return self.resolve(expr.args[0], scope, env, depth + 1)
        if _is_name(expr.func, "warning"):
            return self.resolve(_argument(expr, 1, "message"), scope, env, depth + 1)
        if isinstance(expr.func, ast.Attribute) and expr.func.attr == "format":
            template = self.resolve(expr.func.value, scope, env, depth + 1)
            return [_FORMAT_FIELD.sub(_HOLE, text) for text in template] if template else None
        callee = self.callee(expr.func, scope)
        if callee is None or not callee.returns:
            return None
        inner = self._bind_call(callee, expr, scope, env)
        return self._union(callee.returns, callee, inner, depth)

    # -- untraced sink values -----------------------------------------------------------

    def untraced(self) -> list[str]:
        """Places a sink value goes where the scan cannot follow it."""
        found: list[str] = []
        for module in self.modules.values():
            parents = {
                child: parent
                for parent in ast.walk(module.tree)
                for child in ast.iter_child_nodes(parent)
            }
            for node in ast.walk(module.tree):
                if not self._is_sink_occurrence(node, parents, module):
                    continue
                if self._escapes(node, parents, self._scope_of(node, parents)):
                    found.append(f"{module.rel}:{getattr(node, 'lineno', 0)}")
        return sorted(set(found))

    def _scope_of(self, node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> _Func:
        current = parents.get(node)
        while current is not None:
            if current in self.node_funcs:
                return self.node_funcs[current]
            current = parents.get(current)
        raise AssertionError("every node sits under its module")

    def _is_sink_occurrence(
        self, node: ast.AST, parents: Mapping[ast.AST, ast.AST], module: _Module
    ) -> bool:
        if not isinstance(node, (ast.Attribute, ast.Name, ast.Lambda)):
            return False
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            return False
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and parent.func is node:
            return False
        if isinstance(parent, ast.Attribute) and isinstance(node, ast.Name):
            return False
        return bool(self.sink_of(node, self._scope_of(node, parents)))

    def _escapes(self, node: ast.AST, parents: Mapping[ast.AST, ast.AST], scope: _Func) -> bool:
        child, parent = node, parents.get(node)
        while isinstance(parent, (ast.IfExp, ast.BoolOp)):
            child, parent = parent, parents.get(parent)
        if isinstance(parent, ast.keyword):
            parent = parents.get(parent)
        if isinstance(parent, ast.Call):
            return not self._followed(child, parent, scope)
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            return not all(isinstance(target, ast.Name) for target in targets)
        return isinstance(
            parent,
            (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Starred, ast.Subscript, ast.Attribute),
        )

    def _followed(self, argument: ast.AST, call: ast.Call, scope: _Func) -> bool:
        if _is_name(call.func, _WARNING_SINK):
            return True
        callee = self.callee(call.func, scope)
        if callee is None:
            return False
        return any(item is argument for _, item in _bind_arguments(callee, call))


def _base_severity(expr: ast.Attribute) -> str | None:
    receiver = expr.value
    if isinstance(receiver, ast.Name) and receiver.id in _NOT_SINK_RECEIVERS:
        return None
    return _BASE_ATTRIBUTES.get(expr.attr)


def _bind_arguments(callee: _Func, call: ast.Call) -> list[tuple[str, ast.expr]]:
    """Pair each argument of *call* with the parameter of *callee* that receives it."""
    names = callee.params
    if names and names[0] in {"self", "cls"} and callee.owner_class is not None:
        names = names[1:]
    pairs: list[tuple[str, ast.expr]] = []
    for name, argument in zip(names, call.args, strict=False):
        if isinstance(argument, ast.Starred):
            break
        pairs.append((name, argument))
    for keyword in call.keywords:
        if keyword.arg in callee.params:
            pairs.append((keyword.arg, keyword.value))
    return pairs


def scan_emission_sites(root: Path, files: Iterable[Path]) -> list[EmissionSite]:
    """Every FAIL and WARN emission in *files* (``.py`` files under the package *root*)."""
    return _Analysis(root, files).sites()


def untraced_sink_values(root: Path, files: Iterable[Path]) -> list[str]:
    """``path:line`` of each sink value passed where the scan cannot follow it."""
    return _Analysis(root, files).untraced()


def guard_problems(
    sites: Iterable[EmissionSite],
    *,
    baseline: Iterable[str],
    no_remedy: Mapping[str, str],
    unresolved: Mapping[str, str],
    non_doctor_printers: Mapping[str, str] | None = None,
) -> list[str]:
    """Compare the scan with the reviewed lists; each problem is one line."""
    printers = non_doctor_printers or {}
    sites = list(sites)
    doctor = [s for s in sites if not (s.form == "status_line" and s.path in printers)]
    problems = _printer_problems(sites, printers)
    violating: dict[str, list[EmissionSite]] = {}
    for site in doctor:
        if site.verdict == "violation":
            violating.setdefault(site.key, []).append(site)
    allowed = Counter(baseline)
    for key, found in sorted(violating.items()):
        if key not in no_remedy and len(found) > allowed[key]:
            where = ", ".join(f"{s.path}:{s.line}" for s in found)
            problems.append(f"new FAIL/WARN without a remedy: {key} ({where})")
    for key in sorted(allowed):
        if allowed[key] > len(violating.get(key, ())):
            problems.append(f"now names a remedy; remove it from the baseline: {key}")
    problems += [f"stale no-remedy entry: {k}" for k in sorted(no_remedy) if k not in violating]
    unresolved_keys = {s.key for s in doctor if s.verdict == "unresolved"}
    problems += [
        f"unresolved site not listed: {k}" for k in sorted(unresolved_keys - set(unresolved))
    ]
    problems += [f"stale unresolved entry: {k}" for k in sorted(set(unresolved) - unresolved_keys)]
    for label, reasons in (
        ("no-remedy", no_remedy),
        ("unresolved", unresolved),
        ("printer", printers),
    ):
        for key, reason in reasons.items():
            if not reason.strip() or "\n" in reason.strip():
                problems.append(f"{label} entry needs a one-line reason: {key}")
    return problems


def _printer_problems(sites: Iterable[EmissionSite], printers: Mapping[str, str]) -> list[str]:
    seen = {s.path for s in sites if s.form == "status_line"}
    return [f"stale non-Doctor printer entry: {path}" for path in sorted(set(printers) - seen)]
