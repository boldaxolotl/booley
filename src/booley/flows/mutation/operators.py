"""Find Mutation Operator sites in synthesizable Verilog and SystemVerilog with pyslang.

pyslang is imported only inside :func:`find_sites`: the host install does not need it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Surrogates that ``surrogateescape`` decoding makes of invalid UTF-8 bytes.
_INVALID_BYTE = re.compile("[\udc80-\udcff]")


class MutantGeneratorError(Exception):
    """The Mutant generator cannot produce Mutants from its inputs."""


class MissingPyslangError(MutantGeneratorError):
    """pyslang, the Verilog and SystemVerilog parser, is not installed."""


class SourceParseError(MutantGeneratorError):
    """Source files have parser errors; ``failures`` holds (file, rendered diagnostic)."""

    def __init__(self, failures: Sequence[tuple[str, str]]) -> None:
        self.failures = tuple(failures)
        detail = "\n".join(f"{name}:\n{diagnostic}" for name, diagnostic in self.failures)
        super().__init__(f"cannot parse {len(self.failures)} source file(s):\n{detail}")


@dataclass(frozen=True)
class Site:
    """Replace ``source[start:end]`` by ``replacement``; offsets in the ID are unit-relative."""

    operator: str
    start: int
    end: int
    replacement: str
    unit_key: str  # enclosing top-level design unit: kind, name, ordinal
    unit_start: int  # first byte of that unit, so edits elsewhere do not move IDs


RELATIONAL = "relational"
ARITHMETIC_LOGICAL = "arithmetic-logical"
CONDITION = "condition"
# Fixed order: round-robin selection visits operators in this order.
OPERATOR_NAMES = (RELATIONAL, ARITHMETIC_LOGICAL, CONDITION)

# Syntax kind (without "Expression") -> replacement token, per operator.  Keyed by
# kind, not token text: the nonblocking `<=` assignment is another kind than the
# comparison, and unary `-` or `&` is never matched.  `/`, `%` and `**` are left
# out (divide-by-zero and synthesis-cost noise).
_RELATIONAL_SWAPS = (
    "LessThan:<= LessThanEqual:< GreaterThan:>= GreaterThanEqual:> Equality:!= Inequality:== "
    "CaseEquality:!== CaseInequality:=== WildcardEquality:!=? WildcardInequality:==?"
)
_ARITHMETIC_LOGICAL_SWAPS = (
    "Add:- Subtract:+ Multiply:+ BinaryAnd:| BinaryOr:& BinaryXor:& BinaryXnor:^ LogicalAnd:|| "
    "LogicalOr:&& LogicalShiftLeft:>> LogicalShiftRight:<< ArithmeticShiftLeft:>>> "
    "ArithmeticShiftRight:<<<"
)
_BINARY = {
    f"{kind}Expression": (operator, replacement)
    for operator, swaps in (
        (RELATIONAL, _RELATIONAL_SWAPS),
        (ARITHMETIC_LOGICAL, _ARITHMETIC_LOGICAL_SWAPS),
    )
    for kind, replacement in (swap.split(":") for swap in swaps.split())
}


def _names(text: str) -> frozenset[str]:
    """The whitespace-separated syntax kind names in ``text``."""
    return frozenset(text.split())


# A site must sit under one design context and one design unit (a compilation-unit
# function or task counts); class, program and checker code is verification code.
_DESIGN_CONTEXTS = _names(
    "ContinuousAssign NetDeclaration AlwaysBlock AlwaysCombBlock AlwaysFFBlock AlwaysLatchBlock "
    "FunctionDeclaration TaskDeclaration"
)
_DESIGN_UNITS = _names(
    "ModuleDeclaration InterfaceDeclaration PackageDeclaration FunctionDeclaration TaskDeclaration"
)
# Excluded even inside a design context: attributes, parameter defaults, constant
# widths and selects (bit-select and slice mutation is a later operator), timing
# controls, and every assertion (a mutated assertion fires and would count as a
# detection).
_DENIED = _names(
    "AttributeInstance ParameterDeclaration VariableDimension RangeDimensionSpecifier "
    "ElementSelect DelayControl Delay3 CycleDelay EventControlWithExpression "
    "RepeatedEventControl ImmediateAssertStatement ImmediateAssumeStatement "
    "ImmediateCoverStatement AssertPropertyStatement AssumePropertyStatement "
    "CoverPropertyStatement CoverSequenceStatement RestrictPropertyStatement "
    "ExpectPropertyStatement"
)

# A while, do-while, or for condition may only be forced false: negating or forcing
# it true can make the loop run forever.
_CONDITION_VARIANTS = ("!({})", "1'b1", "1'b0")
_LOOP_VARIANTS = ("1'b0",)


def find_sites(
    source: bytes, rel: str, path: Path, defines: Mapping[str, str], include_dirs: Sequence[Path]
) -> list[Site]:
    """Parse one file and return every operator site in its design code.

    ``rel`` names the file in diagnostics; ``path`` lets relative ``include``
    directives resolve beside it.  Raises :class:`SourceParseError` on a parser
    error and :class:`MissingPyslangError` without pyslang.
    """
    try:
        import pyslang
    except ModuleNotFoundError as exc:
        if exc.name != "pyslang":
            raise
        raise MissingPyslangError(
            "the Mutant generator needs the 'pyslang' package, which the Booley Sandbox "
            "image installs; elsewhere install the optional dependency group 'hdl-parser'"
        ) from exc
    # One invalid byte becomes one '?': byte offsets in the parse stay the file's.
    text = _INVALID_BYTE.sub("?", source.decode("utf-8", "surrogateescape"))
    options = pyslang.parsing.PreprocessorOptions()
    options.predefines = [f"{name}={value}" for name, value in defines.items()]
    options.additionalIncludePaths = [str(include_dir) for include_dir in include_dirs]
    manager = pyslang.SourceManager()
    buffer = manager.assignText(str(path), text)
    tree = pyslang.syntax.SyntaxTree.fromBuffer(buffer, manager, pyslang.Bag([options]))
    if errors := [d for d in tree.diagnostics if d.isError()]:
        rendered = pyslang.DiagnosticEngine.reportAll(manager, errors)
        raise SourceParseError([(rel, rendered.replace(str(path), rel).rstrip())])
    collector = _SiteCollector(pyslang.syntax.SyntaxNode, tree.root, buffer.id, source)
    tree.root.visit(collector.visit)
    return collector.sites


class _SiteCollector:
    """Collect the sites of every operator while walking one syntax tree."""

    def __init__(self, node_type: type, root: Any, buffer: Any, source: bytes) -> None:
        self.sites: list[Site] = []
        self._node_type = node_type
        self._buffer = buffer  # tokens of other buffers (macros, includes) are not in this file
        self._source = source
        # Number each top-level unit by kind, name and ordinal; the list keeps the
        # wrappers alive so id() stays unique.
        self._members = list(root.members)
        self._units: dict[int, tuple[str, int]] = {}
        seen: dict[tuple[str, str], int] = {}
        for member in self._members:
            kind = member.kind.name
            name = member.header.name.rawText if hasattr(member, "header") else ""
            seen[kind, name] = ordinal = seen.get((kind, name), -1) + 1
            first = member.getFirstToken()
            # A unit that starts inside a macro has no file offset: use 0.
            self._units[id(member)] = (f"{kind}:{name}#{ordinal}", self._offset(first) or 0)

    def visit(self, node: Any) -> None:
        if not isinstance(node, self._node_type):
            return
        kind = node.kind.name
        if kind in _BINARY:
            operator, replacement = _BINARY[kind]
            self._add(node, operator, node.operatorToken, node.operatorToken, (replacement,))
        elif kind in ("ConditionalStatement", "ConditionalExpression"):
            self._add_condition(node, node.predicate, _CONDITION_VARIANTS)
        elif kind == "DoWhileStatement" or (
            kind == "LoopStatement" and node.repeatOrWhile.kind.name == "WhileKeyword"
        ):
            self._add_condition(node, node.expr, _LOOP_VARIANTS)
        elif kind == "ForLoopStatement" and node.stopExpr is not None:
            self._add_condition(node, node.stopExpr, _LOOP_VARIANTS)

    def _add_condition(self, node: Any, predicate: Any, variants: tuple[str, ...]) -> None:
        if predicate.kind.name == "ConditionalPredicate":
            # ``matches`` patterns and ``&&&`` chains are not plain Boolean conditions.
            parts = [c for c in predicate.conditions if isinstance(c, self._node_type)]
            if len(parts) != 1 or parts[0].matchesClause is not None:
                return
        self._add(node, CONDITION, predicate.getFirstToken(), predicate.getLastToken(), variants)

    def _add(
        self, node: Any, operator: str, first: Any, last: Any, variants: tuple[str, ...]
    ) -> None:
        """Record one site per variant (``{}`` stands for the original text) unless excluded.

        A site whose first or last token is not in this file (a condition ending
        in a macro, say) has no text span here and is skipped.
        """
        start, end = self._offset(first), self._offset(last, len(last.rawText))
        unit = self._design_unit(node)
        if unit is not None and start is not None and end is not None:
            text = self._source[start:end].decode("utf-8", "replace")
            key, unit_start = unit
            sites = (Site(operator, start, end, v.format(text), key, unit_start) for v in variants)
            self.sites.extend(sites)

    def _offset(self, token: Any, extra: int = 0) -> int | None:
        """The token's file offset (+ ``extra``), or None if it comes from another buffer."""
        location = token.location
        return int(location.offset) + extra if location.buffer == self._buffer else None

    def _design_unit(self, node: Any) -> tuple[str, int] | None:
        """The enclosing unit's (key, start) if ``node`` is in design code, else None."""
        in_context = False
        child, ancestor = node, node.parent
        while ancestor.parent is not None:
            kind = ancestor.kind.name
            in_context = in_context or kind in _DESIGN_CONTEXTS
            if kind in _DENIED or self._excluded_child(ancestor, child):
                return None
            child, ancestor = ancestor, ancestor.parent
        if not in_context or child.kind.name not in _DESIGN_UNITS:
            return None
        return self._units[id(child)]

    @staticmethod
    def _excluded_child(ancestor: Any, child: Any) -> bool:
        """True for a ``for`` initializer or step, or an argument of a ``$`` system call."""
        kind = ancestor.kind.name
        if kind == "ForLoopStatement":
            return bool(child is not ancestor.stopExpr and child is not ancestor.statement)
        if kind == "InvocationExpression":
            return bool(ancestor.left.kind.name == "SystemName" and child is not ancestor.left)
        return False
