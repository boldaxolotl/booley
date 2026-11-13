"""Find Mutation Operator sites in synthesizable Verilog and SystemVerilog with pyslang.

Beyond swapping, deleting and moving, the operators also cover (ADR 0088, amended on
2026-11-13): ``arithmetic-logical`` removes unary operators, swaps reductions, drops an
operand and flips shift signedness and sign casts; ``constant`` changes enum members and
module-level initializers; ``bit-select`` swaps neighbouring concatenation operands.

pyslang is imported only inside :func:`find_sites`: the host install does not need it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

# Surrogates that ``surrogateescape`` decoding makes of invalid UTF-8 bytes.
_INVALID_BYTE = re.compile("[\udc80-\udcff]")


class MutantGeneratorError(Exception):
    """The Mutant generator cannot produce Mutants from its inputs."""


PYSLANG_MISSING_MESSAGE = (
    "pyslang is not installed: the Mutant generator needs it. The Booley Sandbox "
    "image installs it; elsewhere install the optional dependency group 'hdl-parser'"
)


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
# comparison, and unary `-` or `&` is no binary kind.  `/`, `%` and `**` are left out
# (divide-by-zero and synthesis-cost noise).
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
    "CovergroupDeclaration BindDirective"
)

# Contexts beyond the design contexts that a mutation may be reached through (the
# ``reach`` argument of _SiteCollector._add).  Bit-select moves and deletions use none.
_CONNECTION = "connection"  # an expression connected to a module or interface instance
_SIGN_CALL = "sign-call"  # an argument of $signed or $unsigned
_INITIALIZER = "initializer"  # the initializer of a module-level variable
_NO_REACH: frozenset[str] = frozenset()
_REACH = frozenset({_CONNECTION, _SIGN_CALL})
_CONSTANT_REACH = _REACH | {_INITIALIZER}
_NON_DESIGN_UNITS = _names("CheckerDeclaration ProgramDeclaration")
_PORT_CONNECTIONS = _names("NamedPortConnection OrderedPortConnection")
# What wraps a connection's expression between the expression and the connection.
_CONNECTION_WRAPPERS = _names("SimpleSequenceExpr SimplePropertyExpr")
_SIGN_CALLS = frozenset({"$signed", "$unsigned"})

# A while, do-while, or for condition may only be forced false: negating or forcing
# it true can make the loop run forever.
_CONDITION_VARIANTS = ("!({})", "1'b1", "1'b0")
_LOOP_VARIANTS = ("1'b0",)

# Operator 2 (beyond the swaps): unary removal and reduction swaps by syntax kind.  Every
# replacement is parenthesized so that it cannot glue to a preceding operator (`a|!b`).
# `!x` becomes `x != 0`: a truth value that, like `!x`, is one bit wide.
_UNARY_REPLACEMENTS = {
    "UnaryBitwiseNotExpression": "({})",
    "UnaryMinusExpression": "({})",
    "UnaryLogicalNotExpression": "(({}) != 0)",
    "UnaryBitwiseAndExpression": "(|({}))",
    "UnaryBitwiseOrExpression": "(&({}))",
    "UnaryBitwiseXorExpression": "(|({}))",
    "UnaryBitwiseNandExpression": "(&({}))",
    "UnaryBitwiseNorExpression": "(|({}))",
    "UnaryBitwiseXnorExpression": "(^({}))",
}
# Dropping one operand puts the operator's identity element there: the expression keeps
# its width and type.  `(X) | ~(X)` is all ones (and `(X) & ~(X)` all zeros) in the
# context width of X, and legal Verilog-2001.
_OPERAND_DROPS = {
    "LogicalAndExpression": "1'b1",
    "LogicalOrExpression": "1'b0",
    "BinaryAndExpression": "(({0}) | ~({0}))",
    "BinaryOrExpression": "(({0}) & ~({0}))",
}
_SHIFT_FLIPS = {"ArithmeticShiftRightExpression": ">>", "LogicalShiftRightExpression": ">>>"}
_SHIFT_OPERATORS = _names("ArithmeticShiftRight LogicalShiftRight")
# Text that ends in an escaped identifier needs a space before the next token.
_ESCAPED_TAIL = re.compile(r"\\\S*$")


_SIMPLE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _declared(name: str) -> str:
    """``name`` as written in source: an escaped identifier gets its backslash and a space."""
    return name if _SIMPLE_IDENTIFIER.fullmatch(name) else f"\\{name} "


def _drop_to(template: str) -> Callable[[str], tuple[str, ...]]:
    """A variants function that fills ``template`` with the operand's text."""

    def variants(text: str) -> tuple[str, ...]:
        return (template.format(_spaced(text)),)

    return variants


def _replace_by(*replacements: str) -> Callable[[str], tuple[str, ...]]:
    """A variants function that ignores the original text."""
    return lambda _text: replacements


def _spaced(text: str) -> str:
    """``text`` followed by one space if it ends in an escaped identifier (``\\foo``)."""
    return text + " " if _ESCAPED_TAIL.search(text) else text


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
# (Bit-select moves have no reach into port connections, so they never get this far.)
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


@dataclass(frozen=True)
class _EnumRef:
    """One reference to an enum member: all members in declaration order, and which one."""

    members: tuple[str, ...]
    index: int
    package: str  # the declaring package's name, or "" when the enum is not in a package


@dataclass(frozen=True)
class _Facts:
    """What the semantic model says about one file, keyed by file offsets."""

    ranges: Mapping[int, tuple[int, int]]  # declared bounds of a literal select, by first literal
    signed_shifts: frozenset[int]  # operator offsets of right shifts of a signed operand
    enums: Mapping[tuple[int, int], _EnumRef]  # (start, end) of each enum member reference


# Syntax nodes that open a scope: an enum declared directly in a package is reachable as
# `pkg::MEMBER`, one declared in any of these is not.
_SCOPES = _names(
    "PackageDeclaration ModuleDeclaration InterfaceDeclaration ProgramDeclaration "
    "ClassDeclaration FunctionDeclaration TaskDeclaration GenerateBlock "
    "SequentialBlockStatement ParallelBlockStatement"
)


def _declaring_package(symbol: Any) -> str:
    """The name of the package that directly declares ``symbol``, or "" if none does."""
    node = symbol.syntax
    while node is not None:
        kind = node.kind.name
        if kind in _SCOPES:
            return str(node.header.name.rawText) if kind == "PackageDeclaration" else ""
        node = node.parent
    return ""


def _semantic_facts(pyslang: Any, tree: Any, buffer: Any) -> _Facts:
    """Elaborate ``tree`` (the one the sites come from) and record what syntax cannot tell.

    These are the declared bounds of each literal select, the right shifts whose left
    operand is signed in at least one instance (elsewhere ``>>`` and ``>>>`` are the same),
    and each reference to an enum member.  Diagnostics, such as modules or packages
    defined in other files, are ignored: an enum that cannot be resolved has no member
    symbol, so it yields no reference.  Only locations in ``buffer`` are kept.
    """
    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(tree)
    ranges: dict[int, tuple[int, int]] = {}
    signed_shifts: set[int] = set()
    enums: dict[tuple[int, int], _EnumRef] = {}

    def record(expression: Any) -> None:
        kind = expression.kind.name
        if kind == "BinaryOp" and expression.op.name in _SHIFT_OPERATORS:
            # A compound assignment (`x >>>= 1`) is a shift without an operator expression.
            token = getattr(expression.syntax, "operatorToken", None)
            if (
                token is not None
                and token.location.buffer == buffer
                and expression.left.type.isSigned
            ):
                signed_shifts.add(int(token.location.offset))
        elif kind == "NamedValue" and expression.symbol.kind.name == "EnumValue":
            span = expression.sourceRange
            if span.start.buffer == buffer:
                symbol = expression.symbol
                members = tuple(str(member.name) for member in symbol.type.canonicalType)
                enums[int(span.start.offset), int(span.end.offset)] = _EnumRef(
                    members, members.index(str(symbol.name)), _declaring_package(symbol)
                )
        elif kind == "ElementSelect":
            record_select(expression, expression.selector)
        elif kind == "RangeSelect" and expression.selectionKind.name == "Simple":
            record_select(expression, expression.left)

    def record_select(expression: Any, first: Any) -> None:
        location = first.sourceRange.start
        limits = _select_limits(pyslang, expression) if location.buffer == buffer else None
        if limits is not None:
            ranges[int(location.offset)] = limits

    compilation.getRoot().visit(record)
    return _Facts(ranges, frozenset(signed_shifts), enums)


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
        raise MissingPyslangError(PYSLANG_MISSING_MESSAGE) from exc
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
    facts = _semantic_facts(pyslang, tree, buffer.id)
    collector = _SiteCollector(pyslang.syntax.SyntaxNode, tree.root, buffer.id, source, facts)
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
        facts: _Facts,
    ) -> None:
        self.sites: list[Site] = []
        self._facts = facts
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
        # An instance of a checker or program parses like a module instance but is not design.
        self._checkers: set[str] = set()
        root.visit(self._note_checker)

    def _note_checker(self, node: Any) -> None:
        if isinstance(node, self._node_type) and node.kind.name in _NON_DESIGN_UNITS:
            # A program declaration names itself in its header, a checker directly.
            name = node.header.name if hasattr(node, "header") else node.name
            self._checkers.add(str(name.rawText))

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
            self._add_binary(node, kind)
        elif kind in _UNARY_REPLACEMENTS:
            self._add_unary(node, kind)
        elif kind in ("InvocationExpression", "SignedCastExpression"):
            self._add_sign_cast(node, kind)
        elif kind == "ConcatenationExpression":
            self._add_concatenation_swaps(node)
        elif kind in ("IdentifierName", "ScopedName"):
            self._add_enum_member(node)
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

    def _add_binary(self, node: Any, kind: str) -> None:
        """Operators 1 and 2: swap the operator; for operator 2, also its other mutations."""
        operator, replacement = _BINARY[kind]
        token = node.operatorToken
        self._add(node, operator, token, token, (replacement,))
        if operator == ARITHMETIC_LOGICAL:
            self._add_operand_drops(node, kind)
            self._add_shift_signedness(node, kind)

    def _add_guarded(
        self,
        node: Any,
        operator: str,
        first: Any,
        last: Any,
        variants: Callable[[str], tuple[str, ...]] | tuple[str, ...],
    ) -> None:
        """Record a mutation that, like a constant change, skips ``case`` labels and unbounded loops."""
        if not self._in_case_label(node):
            self._add(node, operator, first, last, variants, avoid_loops=True)

    def _add_unary(self, node: Any, kind: str) -> None:
        """Operator 2: remove ``~``, ``-`` or ``!``, or swap a reduction operator.

        The replacement is built from the operand's text and parenthesized, so it never
        glues to a preceding operator.  A ``!`` that is a whole ``if`` or ``?:``
        condition has condition Mutants already.
        """
        if kind == "UnaryLogicalNotExpression" and self._is_whole_condition(node):
            return
        start = self._offset(node.operatorToken)
        operand_start = self._offset(node.operand.getFirstToken())
        if start is None or operand_start is None:
            return
        lead = len(self._text(start, operand_start))
        template = _UNARY_REPLACEMENTS[kind]

        def replace(text: str) -> tuple[str, ...]:
            return (template.format(_spaced(text[lead:])),)

        self._add_guarded(
            node, ARITHMETIC_LOGICAL, node.operatorToken, node.getLastToken(), replace
        )

    def _add_operand_drops(self, node: Any, kind: str) -> None:
        """Operator 2: replace one operand of ``&&``, ``||``, ``&`` or ``|`` by its identity.

        The other operand passes through, and width and type stay.  An operand that calls
        a function or changes a variable is never dropped, as it would not run or would
        run twice.
        """
        if kind not in _OPERAND_DROPS:
            return
        template = _OPERAND_DROPS[kind]
        for operand in (node.left, node.right):
            if self._is_pure(operand):
                first, last = operand.getFirstToken(), operand.getLastToken()
                self._add_guarded(node, ARITHMETIC_LOGICAL, first, last, _drop_to(template))

    def _is_pure(self, operand: Any) -> bool:
        """True unless the operand calls a function or changes a variable."""
        impure: list[str] = []

        def check(item: Any) -> None:
            if not isinstance(item, self._node_type):
                return
            kind = item.kind.name
            # A call, or a system function written without parentheses (`$random`).
            parent = item.parent
            is_call = kind == "InvocationExpression" and self._sign_call(item) is None
            is_bare_system_name = kind == "SystemName" and not (
                parent.kind.name == "InvocationExpression" and self._sign_call(parent) is not None
            )
            if kind.endswith(("AssignmentExpression", "crementExpression")) or (
                is_call or is_bare_system_name
            ):
                impure.append(kind)

        operand.visit(check)
        return not impure

    @staticmethod
    def _sign_call(node: Any) -> str | None:
        """``$signed`` or ``$unsigned`` if ``node`` is a call of it, else None."""
        if node.left.kind.name != "SystemName":
            return None
        name = str(node.left.systemIdentifier.rawText)
        return name if name in _SIGN_CALLS else None

    def _add_shift_signedness(self, node: Any, kind: str) -> None:
        """Operator 2: ``>>>`` <-> ``>>`` where the shifted operand is signed."""
        token = node.operatorToken
        if kind in _SHIFT_FLIPS and self._offset(token) in self._facts.signed_shifts:
            self._add_guarded(node, ARITHMETIC_LOGICAL, token, token, (_SHIFT_FLIPS[kind],))

    def _add_sign_cast(self, node: Any, kind: str) -> None:
        """Operator 2: ``$signed(x)`` -> ``$unsigned(x)`` and ``signed'(x)`` -> ``unsigned'(x)``."""
        if kind == "SignedCastExpression":
            token, replacement = node.signing, "unsigned"
            is_signed = token.rawText == "signed"
        else:
            token, replacement = node.left.getFirstToken(), "$unsigned"
            is_signed = self._sign_call(node) == "$signed"
        if is_signed:
            self._add_guarded(node, ARITHMETIC_LOGICAL, token, token, (replacement,))

    def _add_concatenation_swaps(self, node: Any) -> None:
        """Operator 5: exchange each pair of neighbouring operands of a concatenation.

        Operands with identical text (ignoring whitespace) yield no Mutant.  A replication count is not a
        concatenation operand, and streaming concatenations and assignment patterns are
        other syntax kinds.
        """
        operands = [item for item in node.expressions if isinstance(item, self._node_type)]
        for left, right in pairwise(operands):
            left_span, right_span = self._span(left), self._span(right)
            if left_span is None or right_span is None:
                continue
            left_text, right_text = self._text(*left_span), self._text(*right_span)
            if "".join(left_text.split()) != "".join(right_text.split()):
                between = self._text(left_span[1], right_span[0])
                swapped = _spaced(right_text) + between + _spaced(left_text)
                first, last = left.getFirstToken(), right.getLastToken()
                self._add_guarded(node, BIT_SELECT, first, last, _replace_by(swapped))

    def _add_enum_member(self, node: Any) -> None:
        """Operator 4: a reference to an enum member becomes the previous or next member.

        Members wrap around; a two-member enum yields one Mutant.  The exclusions of
        numeric literals apply, and a member that is a whole port connection is no site.
        """
        span = self._span(node)
        reference = None if span is None else self._facts.enums.get(span)
        if (
            reference is None
            or node.parent.kind.name in _CONSTANT_EXCLUDED_PARENTS
            or self._is_bare_connection(node)
        ):
            return
        members, index = reference.members, reference.index
        neighbours = dict.fromkeys(members[(index + step) % len(members)] for step in (-1, 1))
        # A bare name of a package member may be visible through `import pkg::NAME`
        # only, so its Mutant names the package.
        bare = node.kind.name == "IdentifierName" and reference.package
        prefix = f"{reference.package}::" if bare else ""
        replacements = tuple(
            f"{prefix}{_declared(name)}" for name in neighbours if name != members[index]
        )
        last = node.getLastToken()  # of `pkg::NAME`, only the member's name changes
        self._add_guarded(node, CONSTANT, last, last, _replace_by(*replacements))

    def _span(self, node: Any) -> tuple[int, int] | None:
        """The (start, end) file offsets of ``node``, or None if its text is not in this file."""
        last = node.getLastToken()
        start, end = self._offset(node.getFirstToken()), self._offset(last, len(last.rawText))
        return None if start is None or end is None else (start, end)

    def _text(self, start: int, end: int) -> str:
        return self._source[start:end].decode("utf-8", "replace")

    def _is_whole_condition(self, node: Any) -> bool:
        """True if the expression, looking through parentheses, is a whole ``if``/``?:`` condition."""
        parent = node.parent
        while parent.kind.name == "ParenthesizedExpression":
            parent = parent.parent
        if parent.kind.name not in _CONSTANT_EXCLUDED_PARENTS:
            return False
        # Only a single plain condition has condition Mutants (see _add_condition).
        return self._is_plain_predicate(parent.parent)

    @staticmethod
    def _is_bare_connection(node: Any) -> bool:
        """True if the expression is a whole port connection of an instance."""
        parent = node.parent
        while parent.kind.name in _CONNECTION_WRAPPERS:
            parent = parent.parent
        return bool(parent.kind.name in _PORT_CONNECTIONS)

    def _is_plain_predicate(self, predicate: Any) -> bool:
        """False for a ``matches`` pattern or a ``&&&`` chain: not a plain Boolean condition."""
        if predicate.kind.name != "ConditionalPredicate":
            return True
        parts = [c for c in predicate.conditions if isinstance(c, self._node_type)]
        return len(parts) == 1 and parts[0].matchesClause is None

    def _add_condition(self, node: Any, predicate: Any, variants: tuple[str, ...]) -> None:
        if not self._is_plain_predicate(predicate):
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
        self._add(node, CONSTANT, token, token, variants, avoid_loops=True, reach=_CONSTANT_REACH)

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
        limits = None if offset is None else self._facts.ranges.get(offset)  # None: no range
        raw_bounds = tuple(str(bound.literal.rawText) for bound in bounds)
        if limits is not None:
            self._add(
                node,
                BIT_SELECT,
                first,
                last,
                lambda text: _shifted_selects(text, raw_bounds, limits),
                avoid_loops=True,
                reach=_NO_REACH,
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
                        reach=_NO_REACH,
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
            reach=_NO_REACH,
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
        reach: frozenset[str] = _REACH,
    ) -> None:
        """Record one site per variant unless excluded.

        ``variants`` are replacement templates (``{}`` stands for the original text) or a
        function from the original text to ready replacements.  A site whose first or
        last token is not in this file (a condition ending in a macro, say) has no text
        span here and is skipped.  ``avoid_loops`` skips sites under a loop that a change
        could keep from ending.  ``reach`` names the extra contexts (``_CONNECTION``,
        ``_SIGN_CALL``, ``_INITIALIZER``) the site may sit in.
        """
        start, end = self._offset(first), self._offset(last, len(last.rawText))
        unit = self._design_unit(node, avoid_loops, reach)
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

    def _design_unit(
        self, node: Any, avoid_loops: bool, reach: frozenset[str]
    ) -> tuple[str, int] | None:
        """The enclosing unit's (key, start) if ``node`` is in design code, else None.

        ``node`` itself counts as a design context (a continuous assignment is one) but
        never as a denied kind: a select is the site, not an ancestor of it.  A context
        named in ``reach`` counts as design code too.
        """
        in_context = node.kind.name in _DESIGN_CONTEXTS
        previous, child, ancestor = None, node, node.parent
        while ancestor.parent is not None:
            kind = ancestor.kind.name
            in_context = (
                in_context
                or kind in _DESIGN_CONTEXTS
                or self._is_reached_context(ancestor, child, previous, reach)
            )
            if kind in _DENIED or self._excluded_child(ancestor, child, reach):
                return None
            if avoid_loops and self._is_unbounded_loop(ancestor):
                return None
            previous, child, ancestor = child, ancestor, ancestor.parent
        if not in_context or child.kind.name not in _DESIGN_UNITS:
            return None
        return self._units[id(child)]

    def _is_reached_context(
        self, ancestor: Any, child: Any, previous: Any, reach: frozenset[str]
    ) -> bool:
        """True if ``ancestor`` is a context that ``reach`` makes design code.

        ``child`` is the walk's node just below ``ancestor``, ``previous`` the one below it.
        """
        kind = ancestor.kind.name
        if kind in _PORT_CONNECTIONS:
            # connection -> HierarchicalInstance -> instantiation
            instance = ancestor.parent.parent
            return (
                _CONNECTION in reach
                and instance.kind.name == "HierarchyInstantiation"
                and str(instance.type.rawText) not in self._checkers
            )
        # A variable's initializer, not its type (an inline enum's values, say).
        return (
            kind == "DataDeclaration"
            and _INITIALIZER in reach
            and ancestor.parent.kind.name in _MEMBER_LISTS
            and child.kind.name == "Declarator"
            and previous is child.initializer
        )

    @staticmethod
    def _is_unbounded_loop(node: Any) -> bool:
        """True for ``while``, ``do``-``while``, ``forever``, and procedural ``for``."""
        kind = node.kind.name
        return kind in _LOOPS or (
            kind == "LoopStatement" and node.repeatOrWhile.kind.name == "WhileKeyword"
        )

    def _excluded_child(self, ancestor: Any, child: Any, reach: frozenset[str]) -> bool:
        """True for a ``for`` initializer or step, a ``$`` call argument, or a width.

        Widths are a replication count and a size cast's size.  The arguments of
        ``$signed`` and ``$unsigned`` are not excluded for a ``_SIGN_CALL`` reach.
        """
        kind = ancestor.kind.name
        if kind == "MultipleConcatenationExpression":
            return bool(child is ancestor.expression)
        if kind == "CastExpression":
            return bool(child is ancestor.left)
        if kind == "ForLoopStatement":
            return bool(child is not ancestor.stopExpr and child is not ancestor.statement)
        if kind == "InvocationExpression":
            return bool(
                ancestor.left.kind.name == "SystemName"
                and child is not ancestor.left
                and not (_SIGN_CALL in reach and self._sign_call(ancestor) is not None)
            )
        return False
