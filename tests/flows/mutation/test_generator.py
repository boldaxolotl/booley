"""Contract tests for the deterministic Mutant generator (ADR 0088, issues #2933, #2934).

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
from booley.dev_support.mutation_variants import MutationVariantPlan
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
)
_ARITHMETIC = ("arithmetic-logical",)
_RELATIONAL = ("relational",)
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
    # Division and unary minus on the last lines of the fixture yield nothing.
    assert _sites(_generate(tmp_path, "arith_logic.sv"), _ARITHMETIC) == [
        (2, "+", "-"),
        (3, "-", "+"),
        (4, "*", "+"),
        (5, "&", "|"),
        (6, "|", "&"),
        (7, "^", "&"),
        (8, "<<", ">>"),
        (9, ">>", "<<"),
        (10, "&&", "||"),
        (11, "||", "&&"),
        (12, "~^", "^"),
        (13, "^~", "^"),
        (14, "<<<", ">>>"),
        (15, ">>>", "<<<"),
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
    # bound, replication count, size-cast width, and class methods at file level and
    # inside a package.
    assert _sites(selection, _OPERATORS_1_TO_3) == [
        (12, "==", "!="),
        (13, "==", "!="),
        (16, "+", "-"),
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
    instance port connections (not design code), a full-width slice, `+:` selects,
    non-literal or nested indices, and selects of a parameterized, typedef'd or
    inherited-parameterized range, a parameter, or a queue.
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


def test_deletion_mutants_reparse_cleanly(tmp_path: Path) -> None:
    """Every deletion Mutant of every fixture, generate-body and `if` forms included, parses."""
    for name in _EVERY_APPLICABLE_FIXTURE:
        root = tmp_path / name
        root.mkdir()
        deletions = [m for m in _generate(root, name).mutants if m.operator == "deletion"]
        assert deletions or not name.startswith("deletion"), name
        pristine = (root / name).read_bytes()

        for mutant in deletions:
            edited = (
                pristine[: mutant.start] + mutant.mutated_code.encode() + pristine[mutant.end :]
            )
            assert _error_count(edited.decode("utf-8", "replace")) == 0, (name, mutant)


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
        if m.original_code == "  assign y = (a < b) | (a < b);"
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

    assert _sites(selection, _ARITHMETIC) == [(3, "&", "|"), (4, "|", "&")]
    pristine = (tmp_path / "latin1.v").read_bytes()
    line3 = next(
        i
        for i, m in enumerate(selection.mutants, start=1)
        if m.line == 3 and m.operator == "arithmetic-logical"
    )
    assert written[line3].read_bytes() == pristine.replace(b"a & b", b"a | b")


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

    assert _sites(plain, _ARITHMETIC) == [(4, "|", "&")]
    assert _sites(defined, _ARITHMETIC) == [(2, "&", "|")]


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

    # 2 relational, 3 arithmetic-logical, 3 condition, 2 constant, 2 bit-select and 8
    # deletion candidates: an exhausted operator is skipped.
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
        "deletion",
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
