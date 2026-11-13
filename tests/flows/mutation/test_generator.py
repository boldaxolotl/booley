"""Contract tests for the deterministic Mutant generator (ADR 0088, issues #2933, #2934, #3626).

Fixtures are small Verilog (``.v``) and SystemVerilog (``.sv``) files beside this
module; each test copies the ones it needs into ``tmp_path``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import booley
from booley.core.boundary import BoundaryError
from booley.flows.mutation.variants import MutationVariantPlan
from tests.subprocess_budget import run_bounded_child

if TYPE_CHECKING:
    from booley.flows.mutation.generator import Mutant, MutantSelection

# The module under test is imported inside each test body, never at collection,
# so a missing or broken module fails the test itself.
_GENERATOR = "booley.flows.mutation.generator"
_OPERATORS = "booley.flows.mutation.operators"


def generate_mutants(*args: Any, **kwargs: Any) -> MutantSelection:
    return importlib.import_module(_GENERATOR).generate_mutants(*args, **kwargs)


def _error(name: str) -> type[Exception]:
    return getattr(importlib.import_module(_OPERATORS), name)


_FIXTURES = Path(__file__).parent / "fixtures"
_EVERY_APPLICABLE_FIXTURE = (
    "relational.v",
    "relational_sv.sv",
    "arith_logic.sv",
    "conditions.sv",
    "exclusions.sv",
    "repeat_on_line.v",
    "ifdef.sv",
    "latin1.v",
    "constants.v",
    "constants.sv",
    "bit_select.v",
    "bit_select.sv",
    "deletion.v",
    "deletion.sv",
    "polarity.v",
    "polarity.sv",
)
_ARITHMETIC = ("arithmetic-logical",)
_RELATIONAL = ("relational",)
_CONSTANT = "constant"
_BIT_SELECT = "bit-select"
_OPERATORS_1_TO_3 = ("relational", "arithmetic-logical", "condition")
_ALL = 10_000  # more than any fixture holds: selects every candidate


def _stage(tmp_path: Path, *names: str) -> list[str]:
    """Copy fixtures into ``tmp_path`` byte-for-byte and return their relative names."""
    for name in names:
        (tmp_path / name).write_bytes((_FIXTURES / name).read_bytes())
    return list(names)


def _write(tmp_path: Path, name: str, text: str) -> str:
    """Write a source file with LF endings on every platform and return its name."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return name


def _sites(selection: MutantSelection, operators: tuple[str, ...]) -> list[tuple[int, str, str]]:
    """The (line, original, mutated) of every Mutant of ``operators``, in source order."""
    chosen = [m for m in selection.mutants if m.operator in operators]
    ordered = sorted(chosen, key=lambda m: (m.file, m.start, m.mutated_code))
    return [(m.line, m.original_code, m.mutated_code) for m in ordered]


def _edited_lines(
    root: Path, name: str, selection: MutantSelection, operator: str
) -> list[tuple[int, str]]:
    """The (line, whole line after applying the Mutant) of every Mutant of ``operator``.

    Unlike ``_sites`` this does not depend on how far the applier's anchor widens
    a site, so a select or a placeholder is read in its line.
    """
    pristine = (root / name).read_bytes()
    edited: list[tuple[int, str]] = []
    for mutant in selection.mutants:
        if mutant.operator == operator:
            text = pristine[: mutant.start] + mutant.mutated_code.encode() + pristine[mutant.end :]
            edited.append((mutant.line, text.decode().splitlines()[mutant.line - 1]))
    return sorted(edited)


def _generate(tmp_path: Path, *names: str, **kwargs: object) -> MutantSelection:
    return generate_mutants(tmp_path, _stage(tmp_path, *names), _ALL, 1, **kwargs)  # type: ignore[arg-type]


# AC1: pyslang is an optional dependency, only needed when Mutants are generated.


def test_importing_generator_does_not_import_pyslang() -> None:
    code = (
        "import sys, booley.flows.mutation.generator; "
        "sys.exit(1 if 'pyslang' in sys.modules else 0)"
    )
    package_root = str(Path(booley.__file__).parents[1])  # the source tree under test
    result = run_bounded_child(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": package_root},
    )
    assert result.returncode == 0, result.stderr


def test_missing_pyslang_error_names_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pyslang", None)  # `import pyslang` now fails

    with pytest.raises(_error("MissingPyslangError"), match=r"pyslang.*Sandbox image.*hdl-parser"):
        _generate(tmp_path, "relational.v")


def test_other_import_failures_are_not_reported_as_a_missing_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "broken" / "pyslang"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("import nanobind_backend_is_absent\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(broken.parent))
    monkeypatch.delitem(sys.modules, "pyslang", raising=False)

    with pytest.raises(ModuleNotFoundError, match="nanobind_backend_is_absent"):
        _generate(tmp_path, "relational.v")


# AC2 and AC7: operator sites in small Verilog and SystemVerilog fixtures.


def test_relational_operator_sites(tmp_path: Path) -> None:
    assert _sites(_generate(tmp_path, "relational.v"), _RELATIONAL) == [
        (2, "<", "<="),
        (3, "<=", "<"),
        (4, ">", ">="),
        (5, ">=", ">"),
        (6, "==", "!="),
        (7, "!=", "=="),
        (8, "===", "!=="),
        (9, "!==", "==="),
    ]


def test_wildcard_equality_sites(tmp_path: Path) -> None:
    assert _sites(_generate(tmp_path, "relational_sv.sv"), _RELATIONAL) == [
        (2, "==?", "!=?"),
        (3, "!=?", "==?"),
    ]


def test_arithmetic_logical_operator_sites(tmp_path: Path) -> None:
    """Each operator is swapped; `/` yields nothing, and `-a` loses its minus (issue #3626).

    `&`, `|`, `&&` and `||` also lose one operand each; their lines show the drops.
    """
    selection = _generate(tmp_path, "arith_logic.sv")

    assert _edited_lines(tmp_path, "arith_logic.sv", selection, "arithmetic-logical") == [
        (2, "  assign s = a - b;"),
        (3, "  assign d = a + b;"),
        (4, "  assign m = a + b;"),
        (5, "  assign n = ((a) | ~(a)) & b;"),
        (5, "  assign n = a & ((b) | ~(b));"),
        (5, "  assign n = a | b;"),
        (6, "  assign o = ((a) & ~(a)) | b;"),
        (6, "  assign o = a & b;"),
        (6, "  assign o = a | ((b) & ~(b));"),
        (7, "  assign x = a & b;"),
        (8, "  assign y = a >> 1;"),
        (9, "  assign z = a << 1;"),
        (10, "  assign u = 1'b1 && q;"),
        (10, "  assign u = p && 1'b1;"),
        (10, "  assign u = p || q;"),
        (11, "  assign v = 1'b0 || q;"),
        (11, "  assign v = p && q;"),
        (11, "  assign v = p || 1'b0;"),
        (12, "  assign xn = a ^ b;"),
        (13, "  assign nx = a ^ b;"),
        (14, "  assign sl = a >>> 1;"),
        (15, "  assign sr = a <<< 1;"),
        (17, "  assign m = (a);"),
    ]


def test_condition_operator_sites(tmp_path: Path) -> None:
    """``if`` and ``?:`` get three variants; loops only force false; ``repeat`` none."""
    selection = _generate(tmp_path, "conditions.sv")
    conditions = [m for m in selection.mutants if m.operator == "condition"]

    assert sorted(
        (m.line, m.original_code.strip(), m.mutated_code.strip()) for m in conditions
    ) == [
        (3, "a", "!(a)"),
        (3, "a", "1'b0"),
        (3, "a", "1'b1"),
        (6, "assign z = b ? a : 1'b0;", "assign z = !(b) ? a : 1'b0;"),
        (6, "assign z = b ? a : 1'b0;", "assign z = 1'b0 ? a : 1'b0;"),
        (6, "assign z = b ? a : 1'b0;", "assign z = 1'b1 ? a : 1'b0;"),
        (8, "a", "1'b0"),  # while
        (9, "b", "1'b0"),  # do-while
        (10, "i < 2", "1'b0"),  # for stop expression; its initializer and step are skipped
    ]


def test_excluded_contexts_yield_no_mutants(tmp_path: Path) -> None:
    """Each excluded construct sits inside design code, so removing any one rule shows up."""
    selection = _generate(tmp_path, "exclusions.sv")

    # Kept: the two equality tests, and `+` next to an attribute.  Everything else
    # in the fixture is excluded: comment, string, parameter defaults, nonblocking
    # `<=`, part-select bound, block-local dimension, delays, event expression,
    # `for` initializer and step, `$display` argument, assertions (immediate,
    # assume, cover, and concurrent), macro-ended condition, `initial`, generate-for
    # bound, replication count, size-cast width (the reductions around the last two
    # are kept), and class methods at file level and inside a package.
    assert _sites(selection, _OPERATORS_1_TO_3) == [
        (12, "==", "!="),
        (13, "==", "!="),
        (16, "+", "-"),
        # The reductions of the last two lines are design code (issue #3626).
        (36, "^{(1 + 1){a}}", "(|({(1 + 1){a}}))"),
        (37, "^(2 + 1)'(a)", "(|((2 + 1)'(a)))"),
    ]
    # Operators 4-6 find only the plain literals (the `IDLE macro adds none) and the
    # assignments in design code; `a` has a parameterized range, so no select is moved.
    assert _edited_lines(tmp_path, "exclusions.sv", selection, "constant") == [
        (11, "    y = 1'b1;"),
        (12, "    z = (a[0] == 1'b0);"),
        (13, "    if (state == `IDLE) y = 1'b0;"),
    ]
    assert _edited_lines(tmp_path, "exclusions.sv", selection, "bit-select") == []
    assert _edited_lines(tmp_path, "exclusions.sv", selection, "deletion") == [
        (11, "    begin end"),
        (12, "    begin end"),
        (13, "    if (state == `IDLE) begin end"),
        (14, "    begin end"),
        (16, "  /* deleted */"),
        (21, "  /* deleted */"),
        (22, "  always @(a == b) begin end"),
        (36, "  /* deleted */"),
        (37, "  /* deleted */"),
    ]


# Operators 4-6 (issue #2934): constant, bit-select, deletion.

_CONSTANT_SITES = {
    "constants.v": [
        (7, "00", "01"),  # reset value: only +1 / flip, never negative
        (8, "1", "0"),  # unsized 1 never grows to 2
        (11, "FF", "FE"),  # 255 + 1 does not fit in 8 bits; hex case kept
        (12, "7", "6"),  # unsized 7 never grows to 8
        (13, "A", "9"),
        (13, "A", "B"),
        (14, "01_01", "0100"),
        (14, "01_01", "0110"),
        (16, "2", "1"),
        (16, "2", "3"),
        (18, "0f", "0e"),
        (18, "0f", "10"),
        (19, "9", "10"),
        (19, "9", "8"),
    ],
    "constants.sv": [
        (6, "'0", "'1"),
        (7, "1", "0"),
        (7, "1", "2"),
        (10, "'1", "'0"),
        (11, "1_001", "1000"),
        (11, "1_001", "1010"),
        (14, "2", "1"),
        (14, "2", "3"),
        (14, "3", "2"),
        (14, "3", "4"),
        (16, "0F", "0E"),
        (16, "0F", "10"),
        (17, "9", "10"),
        (17, "9", "8"),
        (21, "4", "3"),
        (21, "4", "5"),
    ],
}


@pytest.mark.parametrize("name", ["constants.v", "constants.sv"])
def test_constant_operator_sites(tmp_path: Path, name: str) -> None:
    """Value digits change; sizes and bases stay.  Excluded literals sit in the fixtures.

    Not mutated: parameter and localparam defaults, ranges, replication counts, size
    casts, `x`/`z` digits, `'x`, a whole `if` condition, `case` labels, and every
    literal under a `for`, `while` or `do` loop.
    """
    assert _sites(_generate(tmp_path, name), ("constant",)) == _CONSTANT_SITES[name]


_BIT_SELECT_SITES = {
    "bit_select.v": [
        (2, "  assign o = i[1];"),
        (8, "  assign ya = a[2];"),
        (8, "  assign ya = a[4];"),
        (9, "  assign yb = a[1];"),
        (10, "  assign yc = a[6];"),
        (11, "  assign yd = b[1];"),
        (12, "  assign ye = b[6];"),
        (13, "  assign yf = c[2];"),
        (14, "  assign yg = c[7];"),
        (15, "  assign yh = mem[0][0];"),
        (15, "  assign yh = mem[0][2];"),
        (15, "  assign yh = mem[1][1];"),
        (21, "  assign sa = a[4:1];"),
        (22, "  assign sb = a[6:3];"),
        (24, "  assign sd = b[1:4];"),
        (24, "  assign sd = b[3:6];"),
        (27, "    r[0] = a[0];"),
        (27, "    r[0] = a[2];"),
        (28, "    {r[2], r[3]} = a[4:3];"),
        (28, "    {r[2], r[3]} = a[6:5];"),
        (28, "    {r[3], r[2]} = a[5:4];"),  # the concatenation swap (issue #3626)
    ],
    "bit_select.sv": [
        (3, "    return x[1];"),
        (3, "    return x[3];"),
        (7, "  return x[1];"),
        (14, "  assign ya = a[2];"),
        (14, "  assign ya = a[4];"),
        (15, "  assign yb = u[0][0];"),
        (15, "  assign yb = u[1][1];"),
        (15, "  assign yb = u[2][0];"),
        (16, "  assign yc = md[0][7];"),
        (16, "  assign yc = md[1][6];"),
        (16, "  assign yc = md[2][7];"),
        (20, "  assign yg = md[1][6:3];"),
        (20, "  assign yg = md[2][5:2];"),
        (20, "  assign yg = md[2][7:4];"),
        (20, "  assign yg = md[3][6:3];"),
        (23, "    r[2] = a[3];"),
        (23, "    r[2] = a[5];"),
    ],
}


@pytest.mark.parametrize("name", ["bit_select.v", "bit_select.sv"])
def test_bit_select_operator_sites(tmp_path: Path, name: str) -> None:
    """Literal read selects move by one inside the declared range; a slice keeps its width.

    Not moved: selects written by an assignment or a concatenation target, selects in
    instance port connections (the swap of a concatenation there is not a move), a
    full-width slice, `+:` selects, non-literal or nested indices, and selects of a
    parameterized, typedef'd or inherited-parameterized range, a parameter, or a queue.
    """
    selection = _generate(tmp_path, name)

    assert _edited_lines(tmp_path, name, selection, "bit-select") == _BIT_SELECT_SITES[name]


def test_bit_select_mutants_stay_within_declared_range(tmp_path: Path) -> None:
    """Every index of a descending, ascending, offset and unpacked range moves only inward."""
    declared = {"d": (0, 3), "e": (0, 3), "f": (2, 5), "u": (2, 5)}
    lines = ["module r(input [3:0] d, input [0:3] e, input [5:2] f, input u [2:5], output y);"]
    lines += [
        f"  assign y = {var}[{i}];"
        for var, (low, high) in declared.items()
        for i in range(low, high + 1)
    ]
    name = _write(tmp_path, "r.sv", "\n".join([*lines, "endmodule", ""]))

    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    moved = _edited_lines(tmp_path, name, selection, "bit-select")
    # Four variables x (two edge indices with one move each, two inner indices with two).
    assert len(moved) == 24
    for _, text in moved:
        var, index = re.findall(r"(\w)\[(\d+)\];", text)[0]
        assert declared[var][0] <= int(index) <= declared[var][1], text


_DELETION_SITES = {
    "deletion.v": [
        (2, "t = a & b", "t"),
        (3, "u = a[0]", "u"),
        (3, "v = b[0]", "v"),
        (5, "assign y = a[0];", "/* deleted */"),
        (8, "assign #1 w = b;", "/* deleted */"),
        (10, "q = a;", "begin end"),
        (11, "r = a;", "begin end"),
        (11, "r = b;", "begin end"),
        (13, "s <= a[3];", "begin end"),
        (16, "4'd0: q <= b;", "/* deleted */"),
        (16, "q <= b;", "begin end"),
        (17, "4'd1: r <= b;", "/* deleted */"),
        (17, "r <= b;", "begin end"),
        (18, "q <= a;", "begin end"),
        (21, "s <= 1'b1;", "begin end"),
        (23, "q <= a;", "begin end"),
        (33, "assign gx = a[0];", "begin end"),
        (33, "assign gx = a[1];", "begin end"),
        (34, "assign gw[g] = a[g];", "begin end"),
        (36, "assign gy = a[2];", "begin end"),
        (37, "assign gy = a[3];", "begin end"),
        (40, "assign gz = a[0];", "/* deleted */"),
    ],
    "deletion.sv": [
        (2, "t = a & b", "t"),
        (3, "assign y = a[0];", "/* deleted */"),
        (6, "q = a;", "begin end"),
        (8, "r += 4'd1;", "begin end"),
        (9, "lbl: s = a[1];", "begin end"),
        (12, "(* keep *) q <= a;", "begin end"),
        (14, "4'd0: r <= a;", "/* deleted */"),
        (14, "r <= a;", "begin end"),
        (15, "4'd1: begin r <= b; s <= a[0]; end", "/* deleted */"),
        (15, "r <= b;", "begin end"),
        (15, "s <= a[0];", "begin end"),
        (19, "w[i] <= b[i];", "begin end"),
        (23, "z = x;", "begin end"),
    ],
}


@pytest.mark.parametrize("name", ["deletion.v", "deletion.sv"])
def test_deletion_operator_sites(tmp_path: Path, name: str) -> None:
    """One assignment, net initializer or non-default `case` item goes; `begin end` and a
    comment stand in so the surrounding construct keeps a body.

    Not deleted: a multi-assignment `assign`, `default` and a single `case` item,
    declaration initializers, `++`, procedural `assign`, and anything in a loop body
    (`repeat`, `foreach` and generate `for` bodies are bounded and stay).
    """
    assert _sites(_generate(tmp_path, name), ("deletion",)) == _DELETION_SITES[name]


def test_awkward_contexts_neither_crash_nor_move_unsafe_sites(tmp_path: Path) -> None:
    """Review regressions: a function's own return variable, a macro in a net initializer,
    compound `case` labels, written selects (`++`, task output argument) and delays.
    """
    source = "\n".join(
        [
            "`define MASK 1'b1",
            "module awk(input [3:0] a, input [1:0] u, input req, output reg [3:0] q, output reg c);",
            "  wire en = `MASK & req;",
            "  function [3:0] rev; input [3:0] x; begin rev[0] = x[3]; end endfunction",
            "  task tk(output [1:0] o); o = 2'd1; endtask",
            "  reg [3:0] r;",
            "  always @* begin tk(r[1:0]); r[1]++; c = #5 ~c; q = rev(a); end",
            "  always @* case (u)",
            "    {1'b0, 1'b1}: q = 4'd2;",
            "    (2'd2): q = 4'd3;",
            "    default: q = 4'd4;",
            "  endcase",
            "endmodule",
            "",
        ]
    )
    name = _write(tmp_path, "awk.v", source)

    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    # Only `x[3]` (a read) moves; the select of `rev` itself has no declared range to use.
    bit_selects = [line for _, line in _edited_lines(tmp_path, name, selection, "bit-select")]
    assert bit_selects == [
        "  function [3:0] rev; input [3:0] x; begin rev[0] = x[2]; end endfunction"
    ]
    # Labels keep their constants; only the statements after them change.
    for line, text in _edited_lines(tmp_path, name, selection, "constant"):
        if line in (9, 10):
            assert text.startswith(("    {1'b0, 1'b1}: q = 4'd", "    (2'd2): q = 4'd")), text
    # The net initializer goes whole despite the macro; `c = #5 ~c` keeps its wait.
    deletions = _edited_lines(tmp_path, name, selection, "deletion")
    assert (3, "  wire en;") in deletions
    assert all("c = #5 ~c;" in text for line, text in deletions if line == 7)
    # Every Mutant of every operator still parses.
    pristine = source.encode()
    for mutant in selection.mutants:
        edited = pristine[: mutant.start] + mutant.mutated_code.encode() + pristine[mutant.end :]
        assert _error_count(edited.decode()) == 0, mutant


def _error_count(text: str) -> int:
    import pyslang

    tree = pyslang.syntax.SyntaxTree.fromText(text)
    return sum(1 for diagnostic in tree.diagnostics if diagnostic.isError())


def _elaboration_error_count(text: str) -> int:
    """Errors of the elaborated text: an unresolved name or an illegal operand counts too."""
    import pyslang

    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(pyslang.syntax.SyntaxTree.fromText(text))
    return sum(1 for diagnostic in compilation.getAllDiagnostics() if diagnostic.isError())


# Text that only a mutation of generator version 3 can produce, per new fixture.
_NEW_KIND_MARKERS = {
    "polarity.v": (
        "((b) != 0)",
        "(|(a))",
        "((a) | ~(a))",
        "((a) & ~(a))",
        "$unsigned",
        "q, p",
    ),
    "polarity.sv": ("pk::DONE", "unsigned", "((r) != 0)", "b, a"),
}


def test_every_mutant_reparses(tmp_path: Path) -> None:
    """Every Mutant of every fixture parses; the new fixtures also elaborate as cleanly.

    Elaboration catches what a parse cannot: an unresolved enum member, or a reduction of
    a `real`.  The new fixtures also have to produce each new kind of Mutant.
    """
    for name in _EVERY_APPLICABLE_FIXTURE:
        root = tmp_path / name
        root.mkdir()
        mutants = _generate(root, name).mutants
        assert any(m.operator == "deletion" for m in mutants) or not name.startswith("deletion")
        pristine = (root / name).read_bytes()
        baseline = (
            _elaboration_error_count(pristine.decode()) if name in _NEW_KIND_MARKERS else None
        )

        for mutant in mutants:
            edited = (
                pristine[: mutant.start] + mutant.mutated_code.encode() + pristine[mutant.end :]
            )
            text = edited.decode("utf-8", "replace")
            assert _error_count(text) == 0, (name, mutant)
            if name.endswith(".v"):  # pyslang parses SystemVerilog: check the text by hand
                assert not re.search(r"'\(|::|\bunsigned'", mutant.mutated_code), (name, mutant)
            if baseline is not None:
                assert _elaboration_error_count(text) <= baseline, (name, mutant)
        for marker in _NEW_KIND_MARKERS.get(name, ()):
            assert any(marker in m.mutated_code for m in mutants), (name, marker)


def test_unary_after_binary_operator_reparses(tmp_path: Path) -> None:
    """A replacement is parenthesized, so it never glues to the operator before it (`a||b`)."""
    name = _write(
        tmp_path,
        "u.v",
        "module u(input [3:0] a, b, output y1, y2, y3, y4);\n"
        "  assign y1 = a|!b;\n"
        "  assign y2 = a&~&b;\n"
        "  assign y3 = a|&b;\n"
        "  assign y4 = a&-b;\n"
        "endmodule\n",
    )
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    edited = _edited_lines(tmp_path, name, selection, "arithmetic-logical")

    for expected in (
        (2, "  assign y1 = a|((b) != 0);"),
        (3, "  assign y2 = a&(&(b));"),
        (4, "  assign y3 = a|(|(b));"),
        (5, "  assign y4 = a&(b);"),
    ):
        assert expected in edited
    for mutant in selection.mutants:
        text = (tmp_path / name).read_bytes()
        text = text[: mutant.start] + mutant.mutated_code.encode() + text[mutant.end :]
        assert _error_count(text.decode()) == 0, mutant


# Polarity, missing-term, enum, concatenation and signedness mutations (generator version 3).

_POLARITY_ARITHMETIC = {
    "polarity.v": [
        (9, "  assign y1 = (a);"),
        (10, "  assign y2 = (b);"),
        (11, "  assign y3 = ((a) & ~(a)) | !b;"),
        (11, "  assign y3 = a & !b;"),
        (11, "  assign y3 = a | ((!b) & ~(!b));"),
        (11, "  assign y3 = a | ((b) != 0);"),
        (12, "  assign o1 = (|(a));"),
        (13, "  assign o2 = (&(a));"),
        (14, "  assign o3 = (|(a));"),
        (15, "  assign o4 = (&(a));"),
        (16, "  assign o5 = (|(a));"),
        (17, "  assign o6 = (^(a));"),
        (18, "  assign o7 = 1'b1 && q;"),
        (18, "  assign o7 = p && 1'b1;"),
        (18, "  assign o7 = p || q;"),
        (19, "  assign o8 = 1'b0 || q;"),
        (19, "  assign o8 = p && q;"),
        (19, "  assign o8 = p || 1'b0;"),
        (20, "  assign t1 = ((a) | ~(a)) & b;"),
        (20, "  assign t1 = a & ((b) | ~(b));"),
        (20, "  assign t1 = a | b;"),
        (21, "  assign t2 = ((a) & ~(a)) | b;"),
        (21, "  assign t2 = a & b;"),
        (21, "  assign t2 = a | ((b) & ~(b));"),
        (22, "  assign t3 = f(a) & ((b) | ~(b));"),  # `f(a)` calls a function: never dropped
        (22, "  assign t3 = f(a) | b;"),
        (24, "  assign w1 = sa <<< 2;"),
        (24, "  assign w1 = sa >> 2;"),  # signed operand: `>>` differs from `>>>`
        (25, "  assign w2 = ua <<< 2;"),  # unsigned operand: no `>>` Mutant
        (26, "  assign w3 = sa << 2;"),
        (26, "  assign w3 = sa >>> 2;"),
        (27, "  assign w4 = $signed(a + b) >> 1;"),  # `$signed` arguments are reachable
        (27, "  assign w4 = $signed(a - b) << 1;"),
        (27, "  assign w4 = $signed(a - b) >>> 1;"),  # `$signed` makes the operand signed
        (27, "  assign w4 = $unsigned(a - b) >> 1;"),
        (28, "  assign w5 = $signed(((a) | ~(a)) & b);"),
        (28, "  assign w5 = $signed(a & ((b) | ~(b)));"),
        (28, "  assign w5 = $signed(a | b);"),
        (28, "  assign w5 = $unsigned(a & b);"),
        (41, "  assign y2 = (\\e ) ;"),  # an escaped identifier keeps its closing space
        (42, "  assign y3 = ((\\e ) | ~(\\e )) & b;"),
        (42, "  assign y3 = \\e & ((b) | ~(b));"),
        (42, "  assign y3 = \\e | b;"),
        # Connections: the expression is mutated, the bare names `.x(a)` are not.
        (
            44,
            "  sub #(.P(3)) u1(.en(((p) | ~(p)) & q), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[0], n1[1]}));",
        ),
        (
            44,
            "  sub #(.P(3)) u1(.en(p & ((q) | ~(q))), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[0], n1[1]}));",
        ),
        (44, "  sub #(.P(3)) u1(.en(p | q), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[0], n1[1]}));"),
    ],
    "polarity.sv": [
        (36, "    y6 = ((r) != 0);"),  # `!r` of a real: a comparison, not a reduction
        (37, "    y1 = unsigned'(a);"),  # `unsigned'(b)` is not a mutation
        (39, "    big = sv <<< 1;"),
        (39, "    big = sv >> 1;"),
        (40, "    big = a <<< 1;"),  # unsigned `a >>> 1` has no `>>` Mutant
    ],
}


@pytest.mark.parametrize("name", ["polarity.v", "polarity.sv"])
def test_arithmetic_logical_new_sites(tmp_path: Path, name: str) -> None:
    """Unary and reduction operators, operand drops, shift signedness and sign casts.

    Not mutated: `if (!rst)` (the condition operator has it), `$display(~a)`, an
    operand that calls a function, a macro, a `case` label, an unsigned shift, a
    `do`, `for` or `forever` body, the arguments of a gate primitive, of a `bind` and
    of a checker instance.
    """
    selection = _generate(tmp_path, name)

    assert (
        _edited_lines(tmp_path, name, selection, "arithmetic-logical")
        == _POLARITY_ARITHMETIC[name]
    )


def test_operand_drop_skips_impure_operands(tmp_path: Path) -> None:
    name = _write(
        tmp_path,
        "impure.sv",
        "module impure(input logic p, output logic y, z);\n"
        "  int k;\n"
        "  always_comb y = (k++ > 0) && p;\n"
        "  always_comb z = (k = 1) || p;\n"
        "endmodule\n",
    )
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    drops = [
        text
        for _, text in _edited_lines(tmp_path, name, selection, "arithmetic-logical")
        if "1'b" in text
    ]

    # Only `p` is replaced: the increment and the assignment would not run.
    assert drops == [
        "  always_comb y = (k++ > 0) && 1'b1;",
        "  always_comb z = (k = 1) || 1'b0;",
    ]


def test_shift_signedness_follows_operand_sign(tmp_path: Path) -> None:
    name = _write(
        tmp_path,
        "sh.sv",
        "module sh(input logic signed [7:0] s, input logic [7:0] u, output logic [7:0] a, b, c, d);\n"
        "  assign a = s >>> 1;\n"
        "  assign b = u >>> 1;\n"
        "  assign c = s >> 1;\n"
        "  assign d = u >> 1;\n"
        "endmodule\n",
    )
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    flips = [
        (m.line, m.original_code, m.mutated_code)
        for m in selection.mutants
        if (m.original_code, m.mutated_code) in {(">>>", ">>"), (">>", ">>>")}
    ]

    assert sorted(flips) == [(2, ">>>", ">>"), (4, ">>", ">>>")]


def test_enum_member_sites(tmp_path: Path) -> None:
    """The previous and next member, wrapping; two members give one; unresolved gives none.

    No site: a `case` label, a one-member enum, a bare-name connection, a variable
    initializer, a `localparam` default, and the values inside an enum declaration.
    """
    selection = _generate(tmp_path, "polarity.sv")

    references = [site for site in _sites(selection, (_CONSTANT,)) if site[1].isidentifier()]
    assert references == [
        (28, "IDLE", "pk::DONE"),  # imported as `import pk::IDLE`: the Mutant names the package
        (28, "IDLE", "pk::RUN"),
        (29, "RUN", "DONE"),  # `pk::RUN`: only the member changes
        (29, "RUN", "IDLE"),
        (30, "ON", "OFF"),
        (51, "IDLE", "pk::DONE"),
        (51, "IDLE", "pk::RUN"),
    ]
    other = _write(
        tmp_path,
        "enums.sv",
        "typedef enum logic {LO, HI} lv_t;\n"
        "module two(input logic a, output logic y, z);\n"
        "  lv_t s;\n"
        "  assign y = (s == HI);\n"
        "  assign z = (s != LO);\n"
        "endmodule\n"
        "module unresolved(input logic a, output logic y);\n"
        "  import missing_pkg::*;\n"
        "  assign y = (a == UNKNOWN);\n"
        "endmodule\n",
    )
    constants = _sites(generate_mutants(tmp_path, [other], _ALL, 1), (_CONSTANT,))
    assert constants == [(4, "HI", "LO"), (5, "LO", "HI")]  # a file-level enum: no package


@pytest.mark.parametrize(
    ("name", "declaration_lines", "expected"),
    [
        ("polarity.v", (7,), [(7, "  reg [7:0] cnt = 8'd2;"), (7, "  reg [7:0] cnt = 8'd4;")]),
        (
            "polarity.sv",
            (3, 8, 13, 24, 25),
            [(8, "  logic [3:0] v = 4'd1;"), (8, "  logic [3:0] v = 4'd3;")],  # interface level
        ),
    ],
    ids=["polarity.v", "polarity.sv"],
)
def test_variable_initializer_constant_sites(
    tmp_path: Path, name: str, declaration_lines: tuple[int, ...], expected: list[tuple[int, str]]
) -> None:
    """A module- or interface-level initializer is a site; its width and type are not.

    No site: a package-level variable (line 3), an enum-valued initializer (13), a
    `localparam` default (24), the values in an inline `enum` (25), and the dimensions
    of `reg [7:0]`.
    """
    selection = _generate(tmp_path, name)

    found = [
        (line, text)
        for line, text in _edited_lines(tmp_path, name, selection, _CONSTANT)
        if line in declaration_lines
    ]
    assert found == expected


_CONCATENATION_SITES = {
    "polarity.v": [
        (36, "  assign y1 = {a[2:1], b[1:0]};"),  # a bit-select move: the concatenation stays
        (36, "  assign y1 = {a[3:2], b[2:1]};"),
        (36, "  assign y1 = {b[1:0], a[3:2]};"),
        (38, "  assign w2 = {p, rst, q};"),  # three operands: two neighbouring pairs
        (38, "  assign w2 = {q, p, rst};"),
        (39, "  assign w3 = {2{q, p}};"),  # inside a replication, not its count
        (40, "  assign {o2, o1} = 2'b10;"),  # the left side of an assignment
        (43, "  assign w4 = {b , \\e };"),
        (44, "  sub #(.P(3)) u1(.en(p & q), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[1], n1[0]}));"),
    ],
    "polarity.sv": [
        (42, "    st8 = {b, a};"),  # `{a, a}`-like twins and streaming `{<<{a, b}}` give none
        (43, "    {y2, y3} = {p, q};"),
        (43, "    {y3, y2} = {q, p};"),
    ],
}


@pytest.mark.parametrize("name", ["polarity.v", "polarity.sv"])
def test_concatenation_order_sites(tmp_path: Path, name: str) -> None:
    """Neighbouring operands trade places; twins, streams and assignment patterns give none."""
    selection = _generate(tmp_path, name)

    assert _edited_lines(tmp_path, name, selection, _BIT_SELECT) == _CONCATENATION_SITES[name]


def test_port_connection_sites(tmp_path: Path) -> None:
    """A connection expression is design code; names, overrides, gates, `bind` and checkers are not.

    No bit-select move happens inside a connection either: `.s(b[2:1])` stays, and the
    two selects of `.o({n1[0], n1[1]})` only trade places.
    """
    (tmp_path / "v").mkdir()
    (tmp_path / "sv").mkdir()
    verilog = _generate(tmp_path / "v", "polarity.v")
    system_verilog = _generate(tmp_path / "sv", "polarity.sv")

    instance = [
        (line, text)
        for operator in ("arithmetic-logical", _CONSTANT, _BIT_SELECT)
        for line, text in _edited_lines(tmp_path / "v", "polarity.v", verilog, operator)
        if line >= 44
    ]
    assert sorted(instance) == [
        (
            44,
            "  sub #(.P(3)) u1(.en(((p) | ~(p)) & q), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[0], n1[1]}));",
        ),
        (
            44,
            "  sub #(.P(3)) u1(.en(p & ((q) | ~(q))), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[0], n1[1]}));",
        ),
        (44, "  sub #(.P(3)) u1(.en(p & q), .x(a), .y(4'd2), .s(b[2:1]), .o({n1[0], n1[1]}));"),
        (44, "  sub #(.P(3)) u1(.en(p & q), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[1], n1[0]}));"),
        (44, "  sub #(.P(3)) u1(.en(p & q), .x(a), .y(4'd4), .s(b[2:1]), .o({n1[0], n1[1]}));"),
        (44, "  sub #(.P(3)) u1(.en(p | q), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[0], n1[1]}));"),
    ]
    # The gate primitive on line 45 has no Mutant; `.x(a)` and the override `.P(3)` none.
    assert not [m for m in verilog.mutants if m.line == 45]
    # Line 51 has an expression connection; 52 a bare enum name, 53 a checker instance,
    # and 55 a `bind` directive.
    assert {m.line for m in system_verilog.mutants if 51 <= m.line <= 55} == {51}


def test_signed_call_arguments_are_reachable(tmp_path: Path) -> None:
    name = _write(
        tmp_path,
        "sc.v",
        "module sc(input [3:0] a, b, output [3:0] y, z);\n"
        "  assign y = $signed(a[3] + b);\n"
        "  assign z = $unsigned(a) - 4'd1;\n"
        '  always @* $display("%d", a + b);\n'
        "endmodule\n",
    )
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    assert _edited_lines(tmp_path, name, selection, "arithmetic-logical") == [
        (2, "  assign y = $signed(a[3] - b);"),
        (2, "  assign y = $unsigned(a[3] + b);"),
        (3, "  assign z = $unsigned(a) + 4'd1;"),
    ]
    # Another call's arguments stay excluded, and a select inside `$signed` is not moved.
    assert _edited_lines(tmp_path, name, selection, _BIT_SELECT) == []
    assert _edited_lines(tmp_path, name, selection, _CONSTANT) == [
        (3, "  assign z = $unsigned(a) - 4'd0;"),
        (3, "  assign z = $unsigned(a) - 4'd2;"),
    ]


def _assignment_widths(text: str) -> list[int]:
    """The bit width of the right side of every assignment, before slang's implicit conversion.

    slang converts each right side to the width of its left side; looking through that
    conversion is the only way to see whether the Mutant changed the expression's width.
    """
    import pyslang

    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(pyslang.syntax.SyntaxTree.fromText(text))
    widths: list[int] = []

    def record(expression: Any) -> None:
        if expression.kind.name == "Assignment":
            right = expression.right
            while right.kind.name == "Conversion" and right.isImplicit:
                right = right.operand
            widths.append(int(right.type.bitWidth))

    compilation.getRoot().visit(record)
    return widths


def test_operand_drop_and_not_removal_keep_width(tmp_path: Path) -> None:
    """`{a & b}` stays 8 bits and `{!b}` one bit, so a concatenation is never resized.

    Dropping `a` as `{b}` would make 8 bits only by luck and `{a}` would make 4; `!b` as
    `{b}` would make 8 where one bit is needed.
    """
    source = (
        "module wd(input [3:0] a, input [7:0] b, output [7:0] y, output q, output [7:0] z);\n"
        "  assign y = {a & b};\n"
        "  assign q = {!b};\n"
        "  assign z = {a | b};\n"
        "endmodule\n"
    )
    name = _write(tmp_path, "wd.v", source)
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    mutants = [m for m in selection.mutants if m.operator == "arithmetic-logical"]
    assert len(mutants) == 7  # three on each `a & b` and `a | b`, one `!b` removal
    assert _assignment_widths(source) == [8, 1, 8]
    # The measure sees a width change: a naive drop `{a}` or `{b}` would be caught.
    assert _assignment_widths(source.replace("{a & b}", "{a}")) == [4, 1, 8]
    assert _assignment_widths(source.replace("{!b}", "{b}")) == [8, 8, 8]
    for mutant in mutants:
        edited = source.encode()[: mutant.start] + mutant.mutated_code.encode()
        edited += source.encode()[mutant.end :]
        assert _assignment_widths(edited.decode()) == [8, 1, 8], mutant


def test_version_2_mutant_ids_are_kept(tmp_path: Path) -> None:
    """Every Mutant of generator version 2 is still produced, with the same ID.

    ``v2_mutant_ids.txt`` lists, per fixture that existed then, the IDs of version 2.
    """
    version = importlib.import_module(_GENERATOR).GENERATOR_VERSION
    # Version 3 and version 4 changed the rules after v2_mutant_ids.txt was recorded (version 4
    # drops `+`/`-` swaps that are equal at width; no recorded fixture has one).
    assert version not in ("2", "3"), "the generator version is still one of v2 or v3"
    recorded: dict[str, set[str]] = {}
    for line in (_FIXTURES / "v2_mutant_ids.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, *ids = line.split()
            recorded[name] = set(ids)
    assert set(recorded) == set(_EVERY_APPLICABLE_FIXTURE) - set(_NEW_KIND_MARKERS)

    kept = grown = 0
    for name, old_ids in recorded.items():
        root = tmp_path / name
        root.mkdir()
        new_ids = {m.id for m in _generate(root, name).mutants}
        assert old_ids <= new_ids, (name, sorted(old_ids - new_ids))
        kept += len(old_ids)
        grown += len(new_ids)
    assert kept == 325  # the IDs recorded at version 2
    assert grown > kept  # some fixtures gain nothing, e.g. relational.v, but the union does


def _add_subtract_swaps(tmp_path: Path, name: str, source: str) -> list[str]:
    """The original text (`+` or `-`) of every `+`/`-` swap Mutant of ``source``."""
    _write(tmp_path, name, source)
    selection = generate_mutants(tmp_path, [name], _ALL, 1)
    return [
        m.original_code
        for m in selection.mutants
        if m.operator == "arithmetic-logical" and m.original_code in ("+", "-")
    ]


def test_add_subtract_swap_skipped_where_equal_at_width(tmp_path: Path) -> None:
    """`a + b` and `a - b` agree on one bit and where `2c` is a multiple of `2**width`."""
    one_bit = "module m(input a, input b, output y);\n  assign y = a + b;\nendmodule\n"
    wraps = "module m(input [3:0] x, output [3:0] y);\n  assign y = x + 4'd8;\nendmodule\n"
    two_bits = "module m(input [1:0] x, output [1:0] y);\n  assign y = x - 2'd2;\nendmodule\n"
    differs = "module m(input [3:0] x, output [3:0] y);\n  assign y = x + 4'd3;\nendmodule\n"
    # The context widens the sum to 8 bits, where `x + 8` and `x - 8` differ.
    widened = "module m(input [3:0] x, output [7:0] y);\n  assign y = x + 4'd8;\nendmodule\n"
    # Elaboration alone cannot size `w`'s type: no width, so the swap stays.
    unknown = "module m(input pkg::t_w w, output [3:0] y);\n  assign y = w + 4'd8;\nendmodule\n"
    unknown_value = "module m(input [3:0] x, output [3:0] y);\n  assign y = x + 4'bx;\nendmodule\n"
    real = "module m(input real r, output real y);\n  assign y = r + 2.0;\nendmodule\n"
    # A negated literal is no constant to the parser, so the swap stays.
    signed_negative = (
        "module m(input [3:0] x, output [3:0] y);\n  assign y = x + -4'sd8;\nendmodule\n"
    )

    assert _add_subtract_swaps(tmp_path, "one.sv", one_bit) == []
    assert _add_subtract_swaps(tmp_path, "wraps.sv", wraps) == []
    assert _add_subtract_swaps(tmp_path, "two.sv", two_bits) == []
    assert _add_subtract_swaps(tmp_path, "differs.sv", differs) == ["+"]
    assert _add_subtract_swaps(tmp_path, "widened.sv", widened) == ["+"]
    assert _add_subtract_swaps(tmp_path, "unknown.sv", unknown) == ["+"]
    assert _add_subtract_swaps(tmp_path, "neg.sv", signed_negative) == ["+"]
    # An unknown value or a real operand is no integer constant to reason about.
    assert _add_subtract_swaps(tmp_path, "unk.sv", unknown_value) == ["+"]
    assert _add_subtract_swaps(tmp_path, "real.sv", real) == ["+"]


def test_add_subtract_swap_kept_where_elaboration_can_differ(tmp_path: Path) -> None:
    """A unit another file may instantiate with other parameters or an interface keeps its swap."""
    body = "  assign y = x + 4'd8;\nendmodule\n"
    ports = "(input [3:0] x, output [3:0] y);\n"
    parameter = f"module m #(parameter int W = 4) {ports}{body}"
    body_parameter = f"module m {ports}  parameter int W = 4;\n{body}"
    type_parameter = f"module m #(parameter type T = logic) {ports}{body}"
    interface_port = f"module m (input [3:0] x, output [3:0] y, bus_if.mp i);\n{body}"
    localparam = f"module m {ports}  localparam int W = 4;\n{body}"
    # A parameter port list may omit the `parameter` keyword; `localparam` stays fixed.
    bare_parameter = f"module m #(int W = 4) {ports}{body}"
    bare_type_parameter = f"module m #(type T = logic) {ports}{body}"
    local_port_parameter = f"module m #(localparam int W = 4) {ports}{body}"
    bare_interface_port = f"module m (input [3:0] x, output [3:0] y, bus_if i);\n{body}"
    other_unit = (
        "module p #(parameter int W = 4) (input [3:0] a, output [3:0] b);\n"
        "  assign b = a;\nendmodule\n" + f"module m {ports}{body}"
    )

    assert _add_subtract_swaps(tmp_path, "par.sv", parameter) == ["+"]
    assert _add_subtract_swaps(tmp_path, "bodypar.sv", body_parameter) == ["+"]
    assert _add_subtract_swaps(tmp_path, "typepar.sv", type_parameter) == ["+"]
    assert _add_subtract_swaps(tmp_path, "iface.sv", interface_port) == ["+"]
    # A localparam cannot be overridden, and another unit's parameter does not matter.
    assert _add_subtract_swaps(tmp_path, "barepar.sv", bare_parameter) == ["+"]
    assert _add_subtract_swaps(tmp_path, "baretype.sv", bare_type_parameter) == ["+"]
    assert _add_subtract_swaps(tmp_path, "bareif.sv", bare_interface_port) == ["+"]
    assert _add_subtract_swaps(tmp_path, "local.sv", localparam) == []
    assert _add_subtract_swaps(tmp_path, "localport.sv", local_port_parameter) == []
    assert _add_subtract_swaps(tmp_path, "other.sv", other_unit) == []


# AC3: every Mutant applies through the existing text-replacement applier.


@dataclass
class _Proposal:
    index: int
    file: str
    line: int
    original_code: str
    mutated_code: str


def _apply_all(tmp_path: Path, files: list[str], mutants: tuple[Mutant, ...]) -> dict[int, Path]:
    proposals = [
        _Proposal(i, m.file, m.line, m.original_code, m.mutated_code)
        for i, m in enumerate(mutants, start=1)
    ]
    plan = MutationVariantPlan.resolve(proposals, tmp_path, files)
    return plan.write_variants(tmp_path / "variants")


def test_every_mutant_applies_through_variant_plan(tmp_path: Path) -> None:
    for name in _EVERY_APPLICABLE_FIXTURE:
        root = tmp_path / name
        root.mkdir()
        selection = _generate(root, name)
        assert selection.mutants, name

        written = _apply_all(root, [name], selection.mutants)

        pristine = (root / name).read_bytes()
        for index, mutant in enumerate(selection.mutants, start=1):
            expected = (
                pristine[: mutant.start]
                + mutant.mutated_code.encode("utf-8")
                + pristine[mutant.end :]
            )
            assert written[index].read_bytes() == expected, (name, mutant)
            assert expected != pristine


def test_constant_condition_yields_only_variants_that_change_it(tmp_path: Path) -> None:
    name = _write(
        tmp_path,
        "k.sv",
        "module k(input a, output y, z);\n"
        "  always_comb if (1'b1) y = a;\n"
        "  always_comb while (1'b0) z = a;\n"
        "endmodule\n",
    )

    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    # Forcing a constant true (or false) would be a no-op the applier rejects.
    assert sorted(
        (m.line, m.mutated_code) for m in selection.mutants if m.operator == "condition"
    ) == [
        (2, "!(1'b1)"),
        (2, "1'b0"),
    ]
    _apply_all(tmp_path, [name], selection.mutants)  # the applier accepts every one


def test_repeated_text_on_one_line_is_anchored_uniquely(tmp_path: Path) -> None:
    selection = _generate(tmp_path, "repeat_on_line.v")
    written = _apply_all(tmp_path, ["repeat_on_line.v"], selection.mutants)
    variants = {
        written[i].read_text(encoding="utf-8").splitlines()[1]
        for i, m in enumerate(selection.mutants, start=1)
        if m.operator == "relational"
    }

    assert variants == {
        "  assign y = (a <= b) | (a < b);",
        "  assign y = (a < b) | (a <= b);",
    }


def test_latin1_comment_keeps_offsets(tmp_path: Path) -> None:
    """A Latin-1 byte in a comment neither fails the parse nor shifts offsets.

    Line 4 repeats `a < b` and carries the byte, so those sites would need a
    widened, non-UTF-8 anchor and are skipped; the unique `|` site is kept.
    """
    selection = _generate(tmp_path, "latin1.v")
    written = _apply_all(tmp_path, ["latin1.v"], selection.mutants)

    # Line 3 also yields its two operand drops (the repeated `a` widened to the whole
    # line); line 4 only the unique `|`.
    assert _sites(selection, _ARITHMETIC) == [
        (3, "  assign y = a & b;", "  assign y = ((a) | ~(a)) & b;"),
        (3, "&", "|"),
        (3, "b", "((b) | ~(b))"),
        (4, "|", "&"),
    ]
    pristine = (tmp_path / "latin1.v").read_bytes()
    swap = next(
        i
        for i, m in enumerate(selection.mutants, start=1)
        if (m.original_code, m.mutated_code) == ("&", "|")
    )
    assert written[swap].read_bytes() == pristine.replace(b"a & b", b"a | b")


# AC4: determinism and stable IDs.

_A_SV = """// header
module m1(input logic a, b, c, d, output logic y, z);
  assign y = a < b;
  assign z = a + b;
endmodule
module m2(input logic a, b, c, d, input logic [7:0] v, output logic y, z, w, x, k);
  assign y = a == b;
  assign z = a & b;
  assign w = c ? a : b;
  assign x = v[3];
  assign k = 4'd9;
endmodule
"""
_B_V = """module other(input a, b, c, output y, z, w, v);
  assign y = a > b;
  assign z = a - b;
  assign w = c ? a : b;
  assign v = a != b;
endmodule
"""


def test_same_inputs_give_identical_output(tmp_path: Path) -> None:
    names = [_write(tmp_path, "a.sv", _A_SV), _write(tmp_path, "b.v", _B_V)]

    first = generate_mutants(tmp_path, names, 5, 7)

    assert generate_mutants(tmp_path, names, 5, 7) == first
    assert generate_mutants(tmp_path, names[::-1], 5, 7) == first


def _ids_by_operator(selection: MutantSelection, file: str | None = None) -> dict[str, list[str]]:
    picks: dict[str, list[str]] = {}
    for mutant in selection.mutants:
        if file is None or mutant.file == file:
            picks.setdefault(mutant.operator, []).append(mutant.id)
    return picks


def test_edit_in_one_file_keeps_other_files_ids_and_selection(tmp_path: Path) -> None:
    names = [_write(tmp_path, "a.sv", _A_SV), _write(tmp_path, "b.v", _B_V)]
    pool = generate_mutants(tmp_path, names, _ALL, 3)
    before = generate_mutants(tmp_path, names, 6, 3)

    # An edit inside m1 adds one relational and one deletion candidate.
    _write(tmp_path, "a.sv", _A_SV.replace("a + b;", "a + b;\n  assign z = a >= b;", 1))
    grown_pool = generate_mutants(tmp_path, names, _ALL, 3)
    after = generate_mutants(tmp_path, names, 6, 3)

    by_file = lambda s, f: {m.id for m in s.mutants if m.file == f}  # noqa: E731
    assert by_file(grown_pool, "b.v") == by_file(pool, "b.v")
    assert by_file(grown_pool, "a.sv") > by_file(pool, "a.sv")  # m2's IDs stayed, one was added
    new_ids = {m.id for m in grown_pool.mutants} - {m.id for m in pool.mutants}
    assert len(new_ids) == 2
    # Per operator the selection changes only where the new candidate outranks the last pick.
    picks_before, picks_after = _ids_by_operator(before), _ids_by_operator(after)
    for operator, ids in picks_before.items():
        assert picks_after[operator][:-1] == ids[:-1], operator
        assert picks_after[operator][-1] in {ids[-1], *new_ids}, operator


def test_header_edit_keeps_every_id(tmp_path: Path) -> None:
    names = [_write(tmp_path, "a.sv", _A_SV)]
    before = generate_mutants(tmp_path, names, _ALL, 3)

    _write(tmp_path, "a.sv", _A_SV.replace("// header", "// a longer header\n`timescale 1ns/1ps"))
    after = generate_mutants(tmp_path, names, _ALL, 3)

    assert [m.id for m in after.mutants] == [m.id for m in before.mutants]
    assert [m.start for m in after.mutants] != [m.start for m in before.mutants]


def test_ids_are_unique_across_condition_variants(tmp_path: Path) -> None:
    name = _write(
        tmp_path, "c.sv", "module c(input a, b, output y);\n  assign y = a ? b : a;\nendmodule\n"
    )

    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    conditions = [m for m in selection.mutants if m.operator == "condition"]
    assert len(conditions) == 3
    assert len({m.id for m in conditions}) == 3


def test_file_level_functions_have_stable_ids(tmp_path: Path) -> None:
    f1 = "function int f1(int a, int b);\n  return a + b;\nendfunction\n"
    f2 = "function int f2(int a, int b);\n  return a - b;\nendfunction\n"
    f0 = "function int f0(int a);\n  return a * 2;\nendfunction\n"
    name = _write(tmp_path, "f.sv", f1 + f2)
    before = {m.id for m in generate_mutants(tmp_path, [name], _ALL, 1).mutants}

    _write(tmp_path, "f.sv", f1 + f0 + f2)  # a function of another name between the two
    after = {m.id for m in generate_mutants(tmp_path, [name], _ALL, 1).mutants}

    assert len(before) == 2
    assert before < after


def test_ifdef_branch_follows_defines(tmp_path: Path) -> None:
    plain = _generate(tmp_path, "ifdef.sv")
    defined = _generate(tmp_path, "ifdef.sv", defines={"X": "1"})

    plain_line = "module m(input a, output y); assign y = a | a; endmodule"
    defined_line = "module m(input a, output y); assign y = a & a; endmodule"
    # The live branch also yields the two operand drops, widened to the whole line.
    assert _sites(plain, _ARITHMETIC) == [
        (4, plain_line, "module m(input a, output y); assign y = ((a) & ~(a)) | a; endmodule"),
        (4, plain_line, "module m(input a, output y); assign y = a | ((a) & ~(a)); endmodule"),
        (4, "|", "&"),
    ]
    assert _sites(defined, _ARITHMETIC) == [
        (2, defined_line, "module m(input a, output y); assign y = ((a) | ~(a)) & a; endmodule"),
        (2, defined_line, "module m(input a, output y); assign y = a & ((a) | ~(a)); endmodule"),
        (2, "&", "|"),
    ]


def test_include_directories_resolve_includes(tmp_path: Path) -> None:
    top = 'module top(input a, b, output y);\n`include "defs.svh"\nendmodule\n'
    _write(tmp_path, "rtl/beside.sv", top)
    _write(tmp_path, "rtl/far.sv", top)
    _write(tmp_path, "rtl/defs.svh", "assign y = a & b;\n")
    (tmp_path / "rtl/far.sv").rename(tmp_path / "far.sv")

    beside = generate_mutants(tmp_path, ["rtl/beside.sv"], _ALL, 1)
    with pytest.raises(_error("SourceParseError"), match=r"far\.sv.*defs\.svh"):
        generate_mutants(tmp_path, ["far.sv"], _ALL, 1)
    far = generate_mutants(tmp_path, ["far.sv"], _ALL, 1, include_dirs=["rtl"])

    # Text from an included file is not in the mutated file: it yields no Mutant.
    assert beside.mutants == far.mutants == ()


# AC5: round-robin selection.

_MIXED = """module mixed(input logic [3:0] a, b, c, output logic p, q, r, s, t, u, v, w);
  assign p = a < b;
  assign q = a > b;
  assign r = a + b;
  assign s = a - b;
  assign t = a & b;
  assign u = a ? b : c;
  assign v = 4'd5;
  assign w = a[1];
endmodule
"""


def test_selection_is_round_robin_across_operators(tmp_path: Path) -> None:
    name = _write(tmp_path, "mixed.sv", _MIXED)

    selection = generate_mutants(tmp_path, [name], 16, 5)

    # 2 relational, 5 arithmetic-logical (`a & b` also loses an operand: two more), 3
    # condition, 2 constant, 2 bit-select and 8 deletion candidates: an exhausted operator
    # is skipped.
    assert [m.operator for m in selection.mutants] == [
        "relational",
        "arithmetic-logical",
        "condition",
        "constant",
        "bit-select",
        "deletion",
        "relational",
        "arithmetic-logical",
        "condition",
        "constant",
        "bit-select",
        "deletion",
        "arithmetic-logical",
        "condition",
        "deletion",
        "arithmetic-logical",
    ]
    assert selection.shortfall is None
    orders = {
        tuple(m.id for m in generate_mutants(tmp_path, [name], 16, seed).mutants)
        for seed in range(6)
    }
    assert len(orders) > 1  # the seed changes which candidates come first


def test_fewer_candidates_than_count_returns_all_and_says_so(tmp_path: Path) -> None:
    _stage(tmp_path, "relational.v")
    exact = generate_mutants(tmp_path, ["relational.v"], 16, 1)  # 8 relational + 8 deletion
    asked = generate_mutants(tmp_path, ["relational.v"], 50, 1)

    assert len(asked.mutants) == 16
    assert (asked.requested, asked.available) == (50, 16)
    assert asked.shortfall == (
        "only 16 candidate Mutants exist; returning all of them (50 requested)"
    )
    assert exact.shortfall is None


# AC6: a parse failure names the file and the diagnostic.


def test_parse_failure_names_file_and_diagnostic(tmp_path: Path) -> None:
    names = _stage(tmp_path, "relational.v", "syntax_error.sv")

    with pytest.raises(_error("SourceParseError")) as raised:
        generate_mutants(tmp_path, names, _ALL, 1)

    failures: list[tuple[str, str]] = list(raised.value.failures)  # type: ignore[attr-defined]
    assert [name for name, _ in failures] == ["syntax_error.sv"]
    assert "syntax_error.sv:2:18: error: expected expression" in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_unreadable_or_non_utf8_source_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(_error("MutantGeneratorError"), match=r"missing\.sv"):
        generate_mutants(tmp_path, ["missing.sv"], 1, 1)

    (tmp_path / "bad.v").write_bytes(
        b"module bad(input a, output y);\n  assign y = a \xe9 a;\nendmodule\n"
    )
    with pytest.raises(_error("SourceParseError"), match=r"bad\.v:2:"):
        generate_mutants(tmp_path, ["bad.v"], 1, 1)


@pytest.mark.parametrize(
    ("files", "count", "seed", "include_dirs"),
    [
        (["a.v"], 0, 1, ()),
        (["a.v"], True, 1, ()),
        (["a.v"], 1, -1, ()),
        (["a.v"], 1, True, ()),
        ([], 1, 1, ()),
        (["a.v", "a.v"], 1, 1, ()),
        (["/etc/a.v"], 1, 1, ()),
        (["../a.v"], 1, 1, ()),
        (["rtl\\a.v"], 1, 1, ()),
        (["a.v"], 1, 1, ("../elsewhere",)),
    ],
)
def test_invalid_arguments_are_rejected(
    tmp_path: Path, files: list[str], count: object, seed: object, include_dirs: tuple[str, ...]
) -> None:
    with pytest.raises(BoundaryError):
        generate_mutants(tmp_path, files, count, seed, include_dirs=include_dirs)  # type: ignore[arg-type]


def test_operand_drop_skips_system_function_without_parentheses(tmp_path: Path) -> None:
    """`$random` has no parentheses to give it away, but dropping its sibling would run it twice."""
    name = _write(
        tmp_path,
        "rnd.v",
        "module rnd(input [3:0] a, output [3:0] y, z);\n"
        "  assign y = $random & a;\n"
        "  assign z = a & $signed(a);\n"
        "endmodule\n",
    )
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    edited = [text for _, text in _edited_lines(tmp_path, name, selection, "arithmetic-logical")]
    # `$random` is never copied; `a` is dropped, and `$signed(a)` is pure.
    assert sorted(edited) == sorted(
        [
            "  assign y = $random & ((a) | ~(a));",
            "  assign y = $random | a;",
            "  assign z = a | $signed(a);",
            "  assign z = a & $unsigned(a);",
            "  assign z = ((a) | ~(a)) & $signed(a);",
            "  assign z = a & (($signed(a)) | ~($signed(a)));",
        ]
    )


def test_enum_member_mutant_keeps_escaped_name(tmp_path: Path) -> None:
    """An escaped member name is written back with its backslash and a space."""
    name = _write(
        tmp_path,
        "esc.sv",
        "module esc(input logic [1:0] s, output logic y);\n"
        "  typedef enum logic [1:0] {\\a+b , C, D} st_t;\n"
        "  st_t t;\n"
        "  assign y = (t == C);\n"
        "endmodule\n",
    )
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    assert [text for _, text in _edited_lines(tmp_path, name, selection, _CONSTANT)] == [
        "  assign y = (t == D);",
        "  assign y = (t == \\a+b );",
    ]
    for mutant in selection.mutants:
        text = (tmp_path / name).read_bytes()
        text = text[: mutant.start] + mutant.mutated_code.encode() + text[mutant.end :]
        assert _elaboration_error_count(text.decode()) == _elaboration_error_count(
            (tmp_path / name).read_text(encoding="utf-8")
        )


def test_concatenation_neighbours_equal_but_for_spacing_yield_none(tmp_path: Path) -> None:
    name = _write(
        tmp_path,
        "sp.v",
        "module sp(input [3:0] a, b, output [15:0] y);\n  assign y = {a + b, a+b, a, b};\nendmodule\n",
    )
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    assert [text for _, text in _edited_lines(tmp_path, name, selection, _BIT_SELECT)] == [
        "  assign y = {a + b, a+b, b, a};",
        "  assign y = {a + b, a, a+b, b};",
    ]


def test_program_instance_and_ampersand_condition_edges(tmp_path: Path) -> None:
    """A program's connections are testbench code; a `&&&` condition keeps its `!x` removal."""
    name = _write(
        tmp_path,
        "pg.sv",
        "program tb(input logic [3:0] x);\nendprogram\n"
        "module pg(input logic [3:0] a, b, input logic c, output logic [3:0] y);\n"
        "  tb t(.x(a & b));\n"
        "  always_comb begin\n"
        "    y = a;\n"
        "    if (!c &&& c) y = b;\n"
        "  end\n"
        "endmodule\n",
    )
    selection = generate_mutants(tmp_path, [name], _ALL, 1)

    lines = {line for line, _ in _edited_lines(tmp_path, name, selection, "arithmetic-logical")}
    assert 4 not in lines
    assert 7 in lines
