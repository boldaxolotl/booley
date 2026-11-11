"""Contract tests for the deterministic Mutant generator (ADR 0088, issue #2933).

Fixtures are small Verilog (``.v``) and SystemVerilog (``.sv``) files beside this
module; each test copies the ones it needs into ``tmp_path``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
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
)
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


def _sites(selection: MutantSelection) -> list[tuple[int, str, str]]:
    """The (line, original, mutated) of every Mutant, in source order."""
    ordered = sorted(selection.mutants, key=lambda m: (m.file, m.start, m.mutated_code))
    return [(m.line, m.original_code, m.mutated_code) for m in ordered]


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
    assert _sites(_generate(tmp_path, "relational.v")) == [
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
    assert _sites(_generate(tmp_path, "relational_sv.sv")) == [
        (2, "==?", "!=?"),
        (3, "!=?", "==?"),
    ]


def test_arithmetic_logical_operator_sites(tmp_path: Path) -> None:
    # Division and unary minus on the last lines of the fixture yield nothing.
    assert _sites(_generate(tmp_path, "arith_logic.sv")) == [
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
    # bound, and class methods at file level and inside a package.
    assert _sites(selection) == [
        (12, "==", "!="),
        (13, "==", "!="),
        (16, "+", "-"),
    ]


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
    assert sorted((m.line, m.mutated_code) for m in selection.mutants) == [
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

    assert _sites(selection) == [(3, "&", "|"), (4, "|", "&")]
    pristine = (tmp_path / "latin1.v").read_bytes()
    line3 = next(i for i, m in enumerate(selection.mutants, start=1) if m.line == 3)
    assert written[line3].read_bytes() == pristine.replace(b"a & b", b"a | b")


# AC4: determinism and stable IDs.

_A_SV = """// header
module m1(input logic a, b, c, d, output logic y, z);
  assign y = a < b;
  assign z = a + b;
endmodule
module m2(input logic a, b, c, d, output logic y, z, w);
  assign y = a == b;
  assign z = a & b;
  assign w = c ? a : b;
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

    # An edit inside m1 adds one relational candidate.
    _write(tmp_path, "a.sv", _A_SV.replace("a + b;", "a + b;\n  assign z = a >= b;", 1))
    grown_pool = generate_mutants(tmp_path, names, _ALL, 3)
    after = generate_mutants(tmp_path, names, 6, 3)

    by_file = lambda s, f: {m.id for m in s.mutants if m.file == f}  # noqa: E731
    assert by_file(grown_pool, "b.v") == by_file(pool, "b.v")
    assert by_file(grown_pool, "a.sv") > by_file(pool, "a.sv")  # m2's IDs stayed, one was added
    new_ids = {m.id for m in grown_pool.mutants} - {m.id for m in pool.mutants}
    assert len(new_ids) == 1
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

    assert _sites(plain) == [(4, "|", "&")]
    assert _sites(defined) == [(2, "&", "|")]


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

_MIXED = """module mixed(input logic a, b, c, output logic p, q, r, s, t, u);
  assign p = a < b;
  assign q = a > b;
  assign r = a + b;
  assign s = a - b;
  assign t = a & b;
  assign u = a ? b : c;
endmodule
"""


def test_selection_is_round_robin_across_operators(tmp_path: Path) -> None:
    name = _write(tmp_path, "mixed.sv", _MIXED)

    selection = generate_mutants(tmp_path, [name], 8, 5)

    # 2 relational, 3 arithmetic-logical, 3 condition: the exhausted operator is skipped.
    assert [m.operator for m in selection.mutants] == [
        "relational",
        "arithmetic-logical",
        "condition",
        "relational",
        "arithmetic-logical",
        "condition",
        "arithmetic-logical",
        "condition",
    ]
    assert selection.shortfall is None
    orders = {
        tuple(m.id for m in generate_mutants(tmp_path, [name], 8, seed).mutants)
        for seed in range(6)
    }
    assert len(orders) > 1  # the seed changes which candidates come first


def test_fewer_candidates_than_count_returns_all_and_says_so(tmp_path: Path) -> None:
    _stage(tmp_path, "relational.v")
    exact = generate_mutants(tmp_path, ["relational.v"], 8, 1)
    asked = generate_mutants(tmp_path, ["relational.v"], 50, 1)

    assert len(asked.mutants) == 8
    assert (asked.requested, asked.available) == (50, 8)
    assert asked.shortfall == (
        "only 8 candidate Mutants exist; returning all of them (50 requested)"
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
