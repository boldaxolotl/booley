"""Find Mutation Operator sites in synthesizable Verilog and SystemVerilog with pyslang.

pyslang is imported only inside :func:`find_sites`: the host install does not need it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
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
CONSTANT = "constant"
BIT_SELECT = "bit-select"
DELETION = "deletion"
# Fixed order: round-robin selection visits operators in this order.
OPERATOR_NAMES = (RELATIONAL, ARITHMETIC_LOGICAL, CONDITION, CONSTANT, BIT_SELECT, DELETION)

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
# function or task counts); class, program, checker and covergroup code is
# verification code, even nested in a module or package (see _DENIED).
_DESIGN_CONTEXTS = _names(
    "ContinuousAssign NetDeclaration AlwaysBlock AlwaysCombBlock AlwaysFFBlock AlwaysLatchBlock "
    "FunctionDeclaration TaskDeclaration"
)
_DESIGN_UNITS = _names(
    "ModuleDeclaration InterfaceDeclaration PackageDeclaration FunctionDeclaration TaskDeclaration"
)
# Excluded even inside a design context: attributes, parameter defaults, constant
# widths and selects (only the bit-select operator mutates a select, after its own
# range check), timing controls, and every assertion (a mutated assertion fires and
# would count as a detection).
_DENIED = _names(
    "AttributeInstance ParameterDeclaration VariableDimension RangeDimensionSpecifier "
    "ElementSelect DelayControl Delay3 CycleDelay EventControlWithExpression "
    "RepeatedEventControl ImmediateAssertStatement ImmediateAssumeStatement "
    "ImmediateCoverStatement AssertPropertyStatement AssumePropertyStatement "
    "CoverPropertyStatement CoverSequenceStatement RestrictPropertyStatement "
    "ExpectPropertyStatement ClassDeclaration ProgramDeclaration CheckerDeclaration "
    "CovergroupDeclaration"
)

# A while, do-while, or for condition may only be forced false: negating or forcing
# it true can make the loop run forever.
_CONDITION_VARIANTS = ("!({})", "1'b1", "1'b0")
_LOOP_VARIANTS = ("1'b0",)

# Operator 4: literal syntax kinds, and the digits each integer base allows.
_LITERALS = _names(
    "IntegerVectorExpression IntegerLiteralExpression UnbasedUnsizedLiteralExpression"
)
_BASE_DIGITS = {"b": "[01]+", "o": "[0-7]+", "d": "[0-9]+", "h": "[0-9a-fA-F]+"}
_BASE_RADIX = {"b": 2, "o": 8, "d": 10, "h": 16}
# A literal that is a whole ``if``/``?:`` condition has condition Mutants already, and
# moving anything in a ``case`` label can overlap another item (Verilator CASEOVERLAP).
_CONSTANT_EXCLUDED_PARENTS = _names("ConditionalPattern")

# Operator 5: a select is written when this chain of selects and concatenations ends at
# the left side of an assignment, an increment or decrement, or a call argument (a task
# output); moving it could drive a variable from two places.
# (Port connections of instances are not design code, so they never get this far.)
_SELECT_CHAIN = _names(
    "IdentifierSelectName ConcatenationExpression SimpleSequenceExpr SimplePropertyExpr"
)
_VARIABLE_SYMBOLS = _names("Variable Net FormalArgument")

# Operator 6: placeholders (the applier rejects empty replacement text).  A parent that
# holds a member list takes a comment; a bare generate body needs a legal empty block.
_MEMBER_LISTS = _names("ModuleDeclaration InterfaceDeclaration GenerateBlock GenerateRegion")
_DELETED_COMMENT = "/* deleted */"
_EMPTY_BLOCK = "begin end"
# Loops that may never end once a literal or counter update changes; ``repeat`` and
# ``foreach`` are bounded, and a generate ``for`` is elaboration-time.
_LOOPS = _names("ForeverStatement DoWhileStatement ForLoopStatement")


def _constant_variants(size: str, base: str, digits: str) -> tuple[str, ...]:
    """Value digits for a literal: lowest bit flipped, plus one, minus one.

    ``size`` is empty for an unsized literal; ``base`` is the base letter.  Digits
    stay in the literal's base and case, zero-padded to the original count.  A
    variant never goes negative, never reaches ``2**size`` for a sized literal, and
    never needs more bits than the original for an unsized one.  A literal with
    ``x``, ``z`` or ``?`` digits has no variants.
    """
    clean = digits.replace("_", "")
    if not re.fullmatch(_BASE_DIGITS[base], clean):
        return ()
    value = int(clean, _BASE_RADIX[base])
    bits = int(size.replace("_", "")) if size else max(1, value.bit_length())
    code = "X" if clean.isupper() else "x" if base == "h" else base  # all-uppercase stays so
    variants: dict[str, None] = {}  # insertion-ordered set
    for new in (value ^ 1, value + 1, value - 1):
        if new != value and 0 <= new < 1 << bits:
            variants[format(new, code).zfill(len(clean))] = None
    return tuple(variants)


def _shifted_selects(
    text: str, raw_bounds: Sequence[str], limits: tuple[int, int]
) -> tuple[str, ...]:
    """The select ``text`` with all its bounds moved down or up one, kept within ``limits``.

    A slice moves as a whole, so its width stays; the text between its bounds is kept.
    """
    numbers = [int(raw.replace("_", "")) for raw in raw_bounds]
    between = text[len(raw_bounds[0]) : len(text) - len(raw_bounds[-1])]
    lower, upper = limits
    moved = ([number + step for number in numbers] for step in (-1, 1))
    return tuple(
        between.join(map(str, bounds))
        for bounds in moved
        if lower <= min(bounds) and max(bounds) <= upper
    )


def _has_identifier(pyslang: Any, nodes: Sequence[Any]) -> bool:
    """True if any of ``nodes`` names anything: a parameter, package constant, or typedef."""
    found: list[Any] = []

    def collect(item: Any) -> None:
        if (
            isinstance(item, pyslang.parsing.Token)
            and item.kind == pyslang.parsing.TokenKind.Identifier
        ):
            found.append(item)

    for node in nodes:
        node.visit(collect)
    return bool(found)


def _select_limits(pyslang: Any, select: Any) -> tuple[int, int] | None:
    """The declared (lower, upper) bounds of the select's base, if no identifier decides them.

    Only a fixed-size variable, net or formal argument qualifies, and neither its type nor
    its unpacked dimensions may name an identifier, so the range is the same in every
    instance of the module.
    """
    base = select.value
    while base.kind.name in ("ElementSelect", "RangeSelect"):
        base = base.value
    if base.kind.name != "NamedValue" or base.symbol.kind.name not in _VARIABLE_SYMBOLS:
        return None
    symbol = base.symbol
    declared = select.value.type
    # A function's return variable and an implicit net have no declarator to inspect.
    dimensions = getattr(symbol.syntax, "dimensions", None)
    if dimensions is None or not declared.hasFixedRange:
        return None
    if _has_identifier(pyslang, [symbol.declaredType.typeSyntax, *dimensions]):
        return None
    return int(declared.fixedRange.lower), int(declared.fixedRange.upper)


def _select_ranges(pyslang: Any, tree: Any, buffer: Any) -> dict[int, tuple[int, int]]:
    """Declared bounds of each literal select in this file, keyed by its first literal's offset.

    Elaborates ``tree`` (the one the sites come from) with pyslang's semantic model;
    diagnostics, such as modules defined in other files, are ignored.
    """
    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(tree)
    ranges: dict[int, tuple[int, int]] = {}

    def record(expression: Any) -> None:
        kind = expression.kind.name
        if kind == "ElementSelect":
            first = expression.selector
        elif kind == "RangeSelect" and expression.selectionKind.name == "Simple":
            first = expression.left
        else:
            return
        location = first.sourceRange.start
        limits = _select_limits(pyslang, expression) if location.buffer == buffer else None
        if limits is not None:
            ranges[int(location.offset)] = limits

    compilation.getRoot().visit(record)
    return ranges


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
    ranges = _select_ranges(pyslang, tree, buffer.id)
    collector = _SiteCollector(pyslang.syntax.SyntaxNode, tree.root, buffer.id, source, ranges)
    tree.root.visit(collector.visit)
    return collector.sites


class _SiteCollector:
    """Collect the sites of every operator while walking one syntax tree."""

    def __init__(
        self,
        node_type: type,
        root: Any,
        buffer: Any,
        source: bytes,
        ranges: Mapping[int, tuple[int, int]],
    ) -> None:
        self.sites: list[Site] = []
        self._ranges = ranges  # declared bounds of literal selects, by file offset
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
            name = self._unit_name(member)
            seen[kind, name] = ordinal = seen.get((kind, name), -1) + 1
            first = member.getFirstToken()
            # A unit that starts inside a macro has no file offset: use 0.
            self._units[id(member)] = (f"{kind}:{name}#{ordinal}", self._offset(first) or 0)

    @staticmethod
    def _unit_name(member: Any) -> str:
        """The declared name of a top-level module, interface, package, function, or task."""
        if hasattr(member, "header"):
            return str(member.header.name.rawText)
        if hasattr(member, "prototype"):
            return str(member.prototype.name.getLastToken().rawText)
        return ""

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
        elif kind in _LITERALS:
            self._add_constant(node, kind)
        elif kind == "ElementSelect":
            self._add_bit_select(node)
        elif kind in ("ContinuousAssign", "ExpressionStatement", "StandardCaseItem", "Declarator"):
            self._add_deletion(node, kind)

    def _add_condition(self, node: Any, predicate: Any, variants: tuple[str, ...]) -> None:
        if predicate.kind.name == "ConditionalPredicate":
            # ``matches`` patterns and ``&&&`` chains are not plain Boolean conditions.
            parts = [c for c in predicate.conditions if isinstance(c, self._node_type)]
            if len(parts) != 1 or parts[0].matchesClause is not None:
                return
        self._add(node, CONDITION, predicate.getFirstToken(), predicate.getLastToken(), variants)

    def _add_constant(self, node: Any, kind: str) -> None:
        """Operator 4: change a numeric literal's value digits (size and base stay)."""
        if node.parent.kind.name in _CONSTANT_EXCLUDED_PARENTS or self._in_case_label(node):
            return
        if kind == "IntegerVectorExpression":
            token = node.value
            variants = _constant_variants(
                node.size.rawText, node.base.rawText[-1].lower(), token.rawText
            )
        elif kind == "IntegerLiteralExpression":
            token = node.literal
            variants = _constant_variants("", "d", token.rawText)
        else:  # '0 and '1; 'x and 'z have no variants
            token = node.literal
            variants = {"'0": ("'1",), "'1": ("'0",)}.get(token.rawText, ())
        self._add(node, CONSTANT, token, token, variants, avoid_loops=True)

    @staticmethod
    def _in_case_label(node: Any) -> bool:
        """True if the node sits in the label list of its nearest enclosing ``case`` item."""
        child, parent = node, node.parent
        while parent is not None and parent.kind.name != "StandardCaseItem":
            child, parent = parent, parent.parent
        return parent is not None and child is not parent.clause

    def _add_bit_select(self, node: Any) -> None:
        """Operator 5: move a literal bit-select index or slice bound by one, within range."""
        selector = node.selector
        if selector.kind.name == "BitSelect":
            bounds = (selector.expr,)
        elif selector.kind.name == "SimpleRangeSelect":
            bounds = (selector.left, selector.right)
        else:
            return
        if any(
            bound.kind.name != "IntegerLiteralExpression" for bound in bounds
        ) or self._is_written(node):
            return
        first, last = selector.getFirstToken(), selector.getLastToken()
        offset = self._offset(first)
        limits = None if offset is None else self._ranges.get(offset)  # None: no range recorded
        raw_bounds = tuple(str(bound.literal.rawText) for bound in bounds)
        if limits is not None:
            self._add(
                node,
                BIT_SELECT,
                first,
                last,
                lambda text: _shifted_selects(text, raw_bounds, limits),
                avoid_loops=True,
            )

    @staticmethod
    def _is_written(node: Any) -> bool:
        """True if the select, alone or inside a concatenation, may be written."""
        child, parent = node, node.parent
        while parent.kind.name in _SELECT_CHAIN:
            child, parent = parent, parent.parent
        parent_kind = parent.kind.name
        if parent_kind.endswith("AssignmentExpression"):
            return child is parent.left
        return parent_kind.endswith(("crementExpression", "Argument"))

    def _add_deletion(self, node: Any, kind: str) -> None:
        """Operator 6: remove one assignment, one net initializer, or one ``case`` item."""
        if kind == "Declarator":
            if node.parent.kind.name == "NetDeclaration" and node.initializer is not None:
                # Cut at the ``=`` token: the parsed text of a macro may differ from the file's.
                equals = self._offset(node.initializer.equals)
                start = self._offset(node.getFirstToken())
                if equals is not None and start is not None:
                    self._add(
                        node,
                        DELETION,
                        node.getFirstToken(),
                        node.getLastToken(),
                        lambda text: (text[: equals - start].rstrip(),),
                        avoid_loops=True,
                    )
            return
        if kind == "ContinuousAssign":
            if sum(isinstance(item, self._node_type) for item in node.assignments) != 1:
                return
            placeholder = (
                _DELETED_COMMENT if node.parent.kind.name in _MEMBER_LISTS else _EMPTY_BLOCK
            )
        elif kind == "StandardCaseItem":
            if node.parent.kind.name != "CaseStatement" or len(node.parent.items) < 2:
                return
            placeholder = _DELETED_COMMENT
        elif (
            node.expr.kind.name.endswith("AssignmentExpression")  # a procedural assignment
            and node.expr.right.kind.name
            != "TimingControlExpression"  # ``c = #5 ~c``: the wait stays
        ):
            placeholder = _EMPTY_BLOCK
        else:
            return
        self._add(
            node,
            DELETION,
            node.getFirstToken(),
            node.getLastToken(),
            (placeholder,),
            avoid_loops=True,
        )

    def _add(
        self,
        node: Any,
        operator: str,
        first: Any,
        last: Any,
        variants: tuple[str, ...] | Callable[[str], tuple[str, ...]],
        *,
        avoid_loops: bool = False,
    ) -> None:
        """Record one site per variant unless excluded.

        ``variants`` are replacement templates (``{}`` stands for the original text) or a
        function from the original text to ready replacements.  A site whose first or
        last token is not in this file (a condition ending in a macro, say) has no text
        span here and is skipped.  ``avoid_loops`` skips sites under a loop that a change
        could keep from ending.
        """
        start, end = self._offset(first), self._offset(last, len(last.rawText))
        unit = self._design_unit(node, avoid_loops)
        if unit is not None and start is not None and end is not None:
            text = self._source[start:end].decode("utf-8", "replace")
            key, unit_start = unit
            if callable(variants):
                replacements = variants(text)
            else:
                replacements = tuple(variant.format(text) for variant in variants)
            for replacement in replacements:
                if replacement != text:  # the applier rejects a no-op, e.g. forcing `1'b1` true
                    self.sites.append(Site(operator, start, end, replacement, key, unit_start))

    def _offset(self, token: Any, extra: int = 0) -> int | None:
        """The token's file offset (+ ``extra``), or None if it comes from another buffer."""
        location = token.location
        return int(location.offset) + extra if location.buffer == self._buffer else None

    def _design_unit(self, node: Any, avoid_loops: bool = False) -> tuple[str, int] | None:
        """The enclosing unit's (key, start) if ``node`` is in design code, else None.

        ``node`` itself counts as a design context (a continuous assignment is one) but
        never as a denied kind: a select is the site, not an ancestor of it.
        """
        in_context = node.kind.name in _DESIGN_CONTEXTS
        child, ancestor = node, node.parent
        while ancestor.parent is not None:
            kind = ancestor.kind.name
            in_context = in_context or kind in _DESIGN_CONTEXTS
            if kind in _DENIED or self._excluded_child(ancestor, child):
                return None
            if avoid_loops and self._is_unbounded_loop(ancestor):
                return None
            child, ancestor = ancestor, ancestor.parent
        if not in_context or child.kind.name not in _DESIGN_UNITS:
            return None
        return self._units[id(child)]

    @staticmethod
    def _is_unbounded_loop(node: Any) -> bool:
        """True for ``while``, ``do``-``while``, ``forever``, and procedural ``for``."""
        kind = node.kind.name
        return kind in _LOOPS or (
            kind == "LoopStatement" and node.repeatOrWhile.kind.name == "WhileKeyword"
        )

    @staticmethod
    def _excluded_child(ancestor: Any, child: Any) -> bool:
        """True for a ``for`` initializer or step, a ``$`` call argument, or a width.

        Widths are a replication count and a size cast's size.
        """
        kind = ancestor.kind.name
        if kind == "MultipleConcatenationExpression":
            return bool(child is ancestor.expression)
        if kind == "CastExpression":
            return bool(child is ancestor.left)
        if kind == "ForLoopStatement":
            return bool(child is not ancestor.stopExpr and child is not ancestor.statement)
        if kind == "InvocationExpression":
            return bool(ancestor.left.kind.name == "SystemName" and child is not ancestor.left)
        return False
