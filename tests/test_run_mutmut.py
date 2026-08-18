"""Tests for run_mutmut: lightweight AST-based mutation testing runner."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# The module has module-level code that resolves PYTHON and CACHE_FILE via
# platform_paths.  We import the module itself so we can patch its globals.
import booley.dev_support.run_mutmut as rm
from booley.dev_support.run_mutmut import (
    BINOP_SWAPS,
    BOOLOP_SWAPS,
    CMPOP_SWAPS,
    Mutation,
    _desc,
    _op_name,
    collect_mutations,
)

# ---------------------------------------------------------------------------
# Mutation dataclass
# ---------------------------------------------------------------------------


class TestMutationDataclass:
    def test_construction_and_fields(self):
        m = Mutation(
            file=Path("foo.py"),
            name="foo:L1:binop:Add->Sub",
            line=1,
            kind="binop",
            description="Add->Sub",
            mutated_code="a - b",
        )
        assert m.file == Path("foo.py")
        assert m.name == "foo:L1:binop:Add->Sub"
        assert m.line == 1
        assert m.kind == "binop"
        assert m.description == "Add->Sub"
        assert m.mutated_code == "a - b"

    def test_equality(self):
        kwargs = {
            "file": Path("x.py"),
            "name": "n",
            "line": 1,
            "kind": "k",
            "description": "d",
            "mutated_code": "c",
        }
        assert Mutation(**kwargs) == Mutation(**kwargs)


# ---------------------------------------------------------------------------
# Operator swap tables
# ---------------------------------------------------------------------------


class TestSwapTables:
    def test_binop_has_add_sub(self):
        assert ast.Sub in BINOP_SWAPS[ast.Add]
        assert ast.Add in BINOP_SWAPS[ast.Sub]

    def test_binop_has_mult_div(self):
        assert ast.FloorDiv in BINOP_SWAPS[ast.Mult]
        assert ast.Mult in BINOP_SWAPS[ast.Div]

    def test_binop_bit_ops(self):
        assert ast.BitOr in BINOP_SWAPS[ast.BitAnd]
        assert ast.BitAnd in BINOP_SWAPS[ast.BitOr]

    def test_binop_shift_ops(self):
        assert ast.RShift in BINOP_SWAPS[ast.LShift]
        assert ast.LShift in BINOP_SWAPS[ast.RShift]

    def test_cmpop_eq_neq(self):
        assert ast.NotEq in CMPOP_SWAPS[ast.Eq]
        assert ast.Eq in CMPOP_SWAPS[ast.NotEq]

    def test_cmpop_lt_lte(self):
        assert ast.LtE in CMPOP_SWAPS[ast.Lt]
        assert ast.Lt in CMPOP_SWAPS[ast.LtE]

    def test_cmpop_gt_gte(self):
        assert ast.GtE in CMPOP_SWAPS[ast.Gt]
        assert ast.Gt in CMPOP_SWAPS[ast.GtE]

    def test_cmpop_is_isnot(self):
        assert ast.IsNot in CMPOP_SWAPS[ast.Is]
        assert ast.Is in CMPOP_SWAPS[ast.IsNot]

    def test_cmpop_in_notin(self):
        assert ast.NotIn in CMPOP_SWAPS[ast.In]
        assert ast.In in CMPOP_SWAPS[ast.NotIn]

    def test_boolop_and_or(self):
        assert ast.Or in BOOLOP_SWAPS[ast.And]
        assert ast.And in BOOLOP_SWAPS[ast.Or]


# ---------------------------------------------------------------------------
# _op_name / _desc
# ---------------------------------------------------------------------------


class TestOpNameAndDesc:
    def test_op_name_returns_class_name(self):
        assert _op_name(ast.Add) == "Add"
        assert _op_name(ast.NotEq) == "NotEq"

    def test_desc_arrow_format(self):
        assert _desc(ast.Add, ast.Sub) == "Add->Sub"
        assert _desc(ast.Eq, ast.NotEq) == "Eq->NotEq"


# ---------------------------------------------------------------------------
# _MutationCollector — via collect_mutations() on small temp files
# ---------------------------------------------------------------------------


def _write_src(tmp_path: Path, code: str, name: str = "sample.py") -> Path:
    """Write a small Python snippet to a temp file and return its path."""
    f = tmp_path / name
    f.write_text(code, encoding="utf-8")
    return f


class TestCollectMutationsBinOp:
    def test_add_generates_sub(self, tmp_path):
        f = _write_src(tmp_path, "x = a + b\n")
        muts = collect_mutations(f)
        names = [m.name for m in muts]
        assert any("binop" in n and "Add->Sub" in n for n in names)

    def test_sub_generates_add(self, tmp_path):
        f = _write_src(tmp_path, "x = a - b\n")
        muts = collect_mutations(f)
        assert any("Sub->Add" in m.name for m in muts)

    def test_mult_generates_floordiv(self, tmp_path):
        f = _write_src(tmp_path, "x = a * b\n")
        muts = collect_mutations(f)
        assert any("Mult->FloorDiv" in m.name for m in muts)


class TestCollectMutationsCmpOp:
    def test_eq_generates_neq(self, tmp_path):
        f = _write_src(tmp_path, "x = a == b\n")
        muts = collect_mutations(f)
        assert any("cmpop" in m.name and "Eq->NotEq" in m.name for m in muts)

    def test_lt_generates_lte(self, tmp_path):
        f = _write_src(tmp_path, "x = a < b\n")
        muts = collect_mutations(f)
        assert any("Lt->LtE" in m.name for m in muts)

    def test_gt_generates_gte(self, tmp_path):
        f = _write_src(tmp_path, "x = a > b\n")
        muts = collect_mutations(f)
        assert any("Gt->GtE" in m.name for m in muts)


class TestCollectMutationsBoolOp:
    def test_and_generates_or(self, tmp_path):
        f = _write_src(tmp_path, "x = a and b\n")
        muts = collect_mutations(f)
        assert any("boolop" in m.name and "And->Or" in m.name for m in muts)

    def test_or_generates_and(self, tmp_path):
        f = _write_src(tmp_path, "x = a or b\n")
        muts = collect_mutations(f)
        assert any("Or->And" in m.name for m in muts)


class TestCollectMutationsConstants:
    def test_int_zero_becomes_one(self, tmp_path):
        f = _write_src(tmp_path, "x = 0\n")
        muts = collect_mutations(f)
        assert any("const" in m.name and "0->1" in m.name for m in muts)

    def test_int_one_becomes_two(self, tmp_path):
        f = _write_src(tmp_path, "x = 1\n")
        muts = collect_mutations(f)
        assert any("const" in m.name and "1->2" in m.name for m in muts)

    def test_int_other_incremented(self, tmp_path):
        f = _write_src(tmp_path, "x = 42\n")
        muts = collect_mutations(f)
        assert any("const" in m.name and "42->43" in m.name for m in muts)

    def test_float_zero_becomes_one(self, tmp_path):
        f = _write_src(tmp_path, "x = 0.0\n")
        muts = collect_mutations(f)
        assert any("const" in m.name and "0.0->1.0" in m.name for m in muts)

    def test_float_nonzero_incremented(self, tmp_path):
        f = _write_src(tmp_path, "x = 3.5\n")
        muts = collect_mutations(f)
        assert any("const" in m.name and "3.5->4.5" in m.name for m in muts)

    def test_bool_true_becomes_false(self, tmp_path):
        f = _write_src(tmp_path, "x = True\n")
        muts = collect_mutations(f)
        assert any("const" in m.name and "True->False" in m.name for m in muts)

    def test_bool_false_becomes_true(self, tmp_path):
        f = _write_src(tmp_path, "x = False\n")
        muts = collect_mutations(f)
        assert any("const" in m.name and "False->True" in m.name for m in muts)

    def test_string_mutated(self, tmp_path):
        f = _write_src(tmp_path, 'x = "hello"\n')
        muts = collect_mutations(f)
        assert any("const_str" in m.name for m in muts)
        # Mutated code should contain the XX...XX wrapper
        str_muts = [m for m in muts if "const_str" in m.name]
        assert any("XX" in m.mutated_code for m in str_muts)

    def test_empty_string_not_mutated(self, tmp_path):
        f = _write_src(tmp_path, 'x = ""\n')
        muts = collect_mutations(f)
        assert not any("const_str" in m.name for m in muts)


class TestCollectMutationsIfNegate:
    def test_if_condition_negated(self, tmp_path):
        code = "if x > 0:\n    pass\n"
        f = _write_src(tmp_path, code)
        muts = collect_mutations(f)
        assert any("negate" in m.name and "negate_if" in m.name for m in muts)
        # The negated code should include 'not' somewhere
        negate_muts = [m for m in muts if "negate_if" in m.name]
        assert any("not" in m.mutated_code for m in negate_muts)


class TestCollectMutationsReturnNone:
    def test_return_value_becomes_none(self, tmp_path):
        code = "def foo():\n    return 42\n"
        f = _write_src(tmp_path, code)
        muts = collect_mutations(f)
        assert any("return" in m.name and "return_None" in m.name for m in muts)
        ret_muts = [m for m in muts if "return_None" in m.name]
        assert any("None" in m.mutated_code for m in ret_muts)

    def test_bare_return_not_mutated(self, tmp_path):
        code = "def foo():\n    return\n"
        f = _write_src(tmp_path, code)
        muts = collect_mutations(f)
        # Bare return has value=None at the AST level, so visit_Return skips it
        assert not any("return_None" in m.name for m in muts)


class TestCollectMutationsPragma:
    def test_pragma_no_mutate_skips_line(self, tmp_path):
        code = "x = a + b  # pragma: no mutate\n"
        f = _write_src(tmp_path, code)
        muts = collect_mutations(f)
        # The binop mutation on that line should be suppressed
        assert not any("binop" in m.name for m in muts)

    def test_pragma_only_affects_marked_line(self, tmp_path):
        code = "x = a + b  # pragma: no mutate\ny = c + d\n"
        f = _write_src(tmp_path, code)
        muts = collect_mutations(f)
        # Line 1 suppressed, line 2 should generate a mutation
        assert not any("L1:" in m.name and "binop" in m.name for m in muts)
        assert any("L2:" in m.name and "binop" in m.name for m in muts)


class TestCollectMutationsDedup:
    def test_same_mutation_not_repeated(self, tmp_path):
        # A simple expression that could trigger the same mutation path twice
        code = "x = a + b\n"
        f = _write_src(tmp_path, code)
        muts = collect_mutations(f)
        names = [m.name for m in muts]
        assert len(names) == len(set(names)), "Duplicate mutation names found"


class TestCollectMutationsSyntaxError:
    def test_syntax_error_returns_empty(self, tmp_path, capsys):
        f = _write_src(tmp_path, "def broken(\n")
        muts = collect_mutations(f)
        assert muts == []
        captured = capsys.readouterr()
        assert "WARN" in captured.out


class TestCollectMutationsCombined:
    """Verify collect_mutations finds multiple mutation kinds in one file."""

    def test_mixed_snippet(self, tmp_path):
        code = (
            "def process(a, b):\n"
            "    if a == b:\n"
            "        return a + b\n"
            "    x = a and b\n"
            "    y = 42\n"
            "    return y\n"
        )
        f = _write_src(tmp_path, code)
        muts = collect_mutations(f)
        kinds = {m.kind for m in muts}
        # Should find at least binop, cmpop, boolop, const, negate, return
        assert "binop" in kinds
        assert "cmpop" in kinds
        assert "boolop" in kinds
        assert "const" in kinds
        assert "negate" in kinds
        assert "return" in kinds


# ---------------------------------------------------------------------------
# find_target_files
# ---------------------------------------------------------------------------


class TestFindTargetFiles:
    def test_finds_step_files(self, tmp_path):
        # Create fake stage module files
        (tmp_path / "step_00_init.py").touch()
        (tmp_path / "step_01_setup.py").touch()
        (tmp_path / "step_05_review.py").touch()
        (tmp_path / "__init__.py").touch()  # package marker: should be excluded

        with patch.object(rm, "STEPS_DIR", tmp_path):
            files = rm.find_target_files(None)
        assert len(files) == 3
        assert all(f.name != "__init__.py" for f in files)

    def test_pattern_filters(self, tmp_path):
        (tmp_path / "step_00_init.py").touch()
        (tmp_path / "step_05_review.py").touch()

        with patch.object(rm, "STEPS_DIR", tmp_path):
            files = rm.find_target_files("step_05")
        assert len(files) == 1
        assert files[0].name == "step_05_review.py"

    def test_no_match_exits(self, tmp_path):
        (tmp_path / "step_00_init.py").touch()

        with patch.object(rm, "STEPS_DIR", tmp_path), pytest.raises(SystemExit):
            rm.find_target_files("nonexistent")

    def test_returns_sorted(self, tmp_path):
        (tmp_path / "step_03_c.py").touch()
        (tmp_path / "step_01_a.py").touch()
        (tmp_path / "step_02_b.py").touch()

        with patch.object(rm, "STEPS_DIR", tmp_path):
            files = rm.find_target_files(None)
        names = [f.name for f in files]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# load_results / save_results — JSON round-trip
# ---------------------------------------------------------------------------


class TestLoadSaveResults:
    def test_load_missing_returns_empty(self, tmp_path):
        cache = tmp_path / "results.json"
        with patch.object(rm, "CACHE_FILE", cache):
            assert rm.load_results() == {}

    def test_round_trip(self, tmp_path):
        cache = tmp_path / "results.json"
        data = {
            "step_00.py": {
                "step_00:L5:binop:Add->Sub": "killed",
                "step_00:L8:cmpop:Eq->NotEq": "survived",
            },
            "step_01.py": {
                "step_01:L3:const:0->1": "timeout",
            },
        }
        with patch.object(rm, "CACHE_FILE", cache):
            rm.save_results(data)
            loaded = rm.load_results()
        assert loaded == data

    def test_save_overwrites(self, tmp_path):
        cache = tmp_path / "results.json"
        with patch.object(rm, "CACHE_FILE", cache):
            rm.save_results({"a": {"m1": "killed"}})
            rm.save_results({"b": {"m2": "survived"}})
            loaded = rm.load_results()
        # Second save overwrites, not merges
        assert "a" not in loaded
        assert loaded == {"b": {"m2": "survived"}}

    def test_save_creates_valid_json(self, tmp_path):
        cache = tmp_path / "results.json"
        with patch.object(rm, "CACHE_FILE", cache):
            rm.save_results({"x.py": {"mut1": "killed"}})
        raw = cache.read_text()
        parsed = json.loads(raw)
        assert parsed == {"x.py": {"mut1": "killed"}}


# ---------------------------------------------------------------------------
# show_results
# ---------------------------------------------------------------------------


class TestShowResults:
    def test_empty_results(self, capsys):
        with patch.object(rm, "load_results", return_value={}):
            rm.show_results()
        out = capsys.readouterr().out
        assert "No results" in out

    def test_formats_output(self, capsys):
        data = {
            "step_00.py": {
                "m1": "killed",
                "m2": "survived",
                "m3": "timeout",
                "m4": "error",
            }
        }
        with patch.object(rm, "load_results", return_value=data):
            rm.show_results()
        out = capsys.readouterr().out
        assert "step_00.py" in out
        assert "K:1" in out
        assert "S:1" in out
        assert "T:1" in out
        assert "E:1" in out
        assert "TOTAL" in out

    def test_kill_rate_percentage(self, capsys):
        data = {
            "f.py": {
                "m1": "killed",
                "m2": "killed",
                "m3": "survived",
            }
        }
        with patch.object(rm, "load_results", return_value=data):
            rm.show_results()
        out = capsys.readouterr().out
        # 2 killed / (2 killed + 1 survived) = 66.7%
        assert "66.7%" in out

    def test_multiple_files(self, capsys):
        data = {
            "a.py": {"m1": "killed"},
            "b.py": {"m2": "survived"},
        }
        with patch.object(rm, "load_results", return_value=data):
            rm.show_results()
        out = capsys.readouterr().out
        assert "a.py" in out
        assert "b.py" in out
        assert "TOTAL" in out


# ---------------------------------------------------------------------------
# show_survivors
# ---------------------------------------------------------------------------


class TestShowSurvivors:
    def test_empty_results(self, capsys):
        with patch.object(rm, "load_results", return_value={}):
            rm.show_survivors()
        out = capsys.readouterr().out
        assert "No results" in out

    def test_no_survivors(self, capsys):
        data = {"f.py": {"m1": "killed", "m2": "killed"}}
        with patch.object(rm, "load_results", return_value=data):
            rm.show_survivors()
        out = capsys.readouterr().out
        assert "No survivors" in out

    def test_lists_survived_only(self, capsys):
        data = {
            "f.py": {
                "m_killed": "killed",
                "m_survived_1": "survived",
                "m_survived_2": "survived",
                "m_timeout": "timeout",
            }
        }
        with patch.object(rm, "load_results", return_value=data):
            rm.show_survivors()
        out = capsys.readouterr().out
        assert "m_survived_1" in out
        assert "m_survived_2" in out
        assert "m_killed" not in out
        assert "m_timeout" not in out

    def test_index_numbering(self, capsys):
        data = {"f.py": {"a": "survived", "b": "survived"}}
        with patch.object(rm, "load_results", return_value=data):
            rm.show_survivors()
        out = capsys.readouterr().out
        assert "[0]" in out
        assert "[1]" in out


# ---------------------------------------------------------------------------
# show_mutant
# ---------------------------------------------------------------------------


class TestShowMutant:
    _SAMPLE_RESULTS: ClassVar[dict] = {
        "step_05.py": {
            "step_05:L10:binop:Add->Sub": "survived",
            "step_05:L20:cmpop:Eq->NotEq": "killed",
            "step_05:L30:const:0->1": "survived",
        }
    }

    def test_by_index(self, capsys):
        with (
            patch.object(rm, "load_results", return_value=self._SAMPLE_RESULTS),
            patch.object(rm, "STEPS_DIR", Path("/nonexistent")),
        ):
            rm.show_mutant("0")
        out = capsys.readouterr().out
        assert "step_05.py" in out
        assert "step_05:L10:binop:Add->Sub" in out

    def test_by_index_second(self, capsys):
        with (
            patch.object(rm, "load_results", return_value=self._SAMPLE_RESULTS),
            patch.object(rm, "STEPS_DIR", Path("/nonexistent")),
        ):
            rm.show_mutant("1")
        out = capsys.readouterr().out
        assert "L30" in out

    def test_by_name_substring(self, capsys):
        with (
            patch.object(rm, "load_results", return_value=self._SAMPLE_RESULTS),
            patch.object(rm, "STEPS_DIR", Path("/nonexistent")),
        ):
            rm.show_mutant("const")
        out = capsys.readouterr().out
        assert "const:0->1" in out

    def test_index_out_of_range(self, capsys):
        with patch.object(rm, "load_results", return_value=self._SAMPLE_RESULTS):
            rm.show_mutant("99")
        out = capsys.readouterr().out
        assert "out of range" in out

    def test_no_match(self, capsys):
        with patch.object(rm, "load_results", return_value=self._SAMPLE_RESULTS):
            rm.show_mutant("nonexistent_xyz")
        out = capsys.readouterr().out
        assert "No match" in out

    def test_shows_parsed_fields(self, capsys):
        with (
            patch.object(rm, "load_results", return_value=self._SAMPLE_RESULTS),
            patch.object(rm, "STEPS_DIR", Path("/nonexistent")),
        ):
            rm.show_mutant("0")
        out = capsys.readouterr().out
        assert "Line:" in out
        assert "Kind:" in out
        assert "Change:" in out

    def test_shows_source_context(self, tmp_path, capsys):
        src = tmp_path / "step_05.py"
        lines = [f"line_{i}" for i in range(1, 20)]
        src.write_text("\n".join(lines), encoding="utf-8")

        with (
            patch.object(rm, "load_results", return_value=self._SAMPLE_RESULTS),
            patch.object(rm, "STEPS_DIR", tmp_path),
        ):
            rm.show_mutant("0")
        out = capsys.readouterr().out
        assert ">>>" in out
