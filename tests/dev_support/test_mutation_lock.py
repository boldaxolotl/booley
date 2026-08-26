"""Tests for booley.dev_support.mutation_lock — lock schema, hashing, harness injection.

The mutation_tester relies on this module to persist cold-start work and
detect when it can be reused.  Tests cover the on-disk schema, scope-hash
identity checks, the booley_mut_pkg + plusarg-reader textual injection,
and the build-cache validity helpers.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from booley.dev_support import mutation_lock as lm

# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestScopeHashing:
    def test_round_trip(self, tmp_path: Path):
        f = tmp_path / "a.sv"
        f.write_text("module a; endmodule\n", encoding="utf-8")
        hashes = lm.compute_scope_hashes(["a.sv"], tmp_path)
        assert hashes["a.sv"].startswith("sha256:")

    def test_missing_file_marker(self, tmp_path: Path):
        hashes = lm.compute_scope_hashes(["nope.sv"], tmp_path)
        assert hashes["nope.sv"] == "sha256:MISSING"

    def test_edit_changes_hash(self, tmp_path: Path):
        f = tmp_path / "a.sv"
        f.write_text("v1\n", encoding="utf-8")
        h1 = lm.compute_scope_hashes(["a.sv"], tmp_path)["a.sv"]
        f.write_text("v2\n", encoding="utf-8")
        h2 = lm.compute_scope_hashes(["a.sv"], tmp_path)["a.sv"]
        assert h1 != h2


# ---------------------------------------------------------------------------
# Lock save/load round-trip
# ---------------------------------------------------------------------------


class TestLockPersistence:
    def test_round_trip(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        meta = lm.LockMeta(
            schema_version=lm.LOCK_SCHEMA_VERSION,
            created_at=lm.now_iso(),
            scope=["rtl/a.sv"],
            scope_hashes={"rtl/a.sv": "sha256:abc"},
            count=3,
            host_file="rtl/a.sv",
            mutations=[{"index": 1, "category": "x"}],
            muxed_files=["muxed_a.sv"],
            pkg_file=lm.MUT_PKG_FILENAME,
            docker_digest="sha256:img",
        )
        lm.save_lock(meta)
        loaded = lm.load_lock()
        assert loaded is not None
        assert loaded.scope == ["rtl/a.sv"]
        assert loaded.count == 3
        assert loaded.mutations[0]["index"] == 1
        persisted = json.loads(lm.lock_json_path().read_text(encoding="utf-8"))
        assert "muxed_files" not in persisted
        assert "pkg_file" not in persisted
        assert "docker_digest" not in persisted

    def test_corrupt_lock_treated_as_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        lm.lock_dir().mkdir(parents=True, exist_ok=True)
        lm.lock_json_path().write_text("not json", encoding="utf-8")
        assert lm.load_lock() is None

    def test_missing_lock_is_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        assert lm.load_lock() is None

    def test_malformed_count_treated_as_missing(self, tmp_path: Path, monkeypatch):
        # Valid JSON object, but count is non-numeric — int() would raise
        # ValueError. The guard must treat it as a corrupt (missing) lock.
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        lm.lock_dir().mkdir(parents=True, exist_ok=True)
        lm.lock_json_path().write_text('{"count": "ten"}', encoding="utf-8")
        assert lm.load_lock() is None

    def test_malformed_scope_type_treated_as_missing(self, tmp_path: Path, monkeypatch):
        # scope is a non-iterable — list() would raise TypeError.
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        lm.lock_dir().mkdir(parents=True, exist_ok=True)
        lm.lock_json_path().write_text('{"scope": 5}', encoding="utf-8")
        assert lm.load_lock() is None


# ---------------------------------------------------------------------------
# Validity
# ---------------------------------------------------------------------------


class TestLockValidity:
    def _meta(self, scope: list[str], hashes: dict[str, str]) -> lm.LockMeta:
        return lm.LockMeta(
            schema_version=lm.LOCK_SCHEMA_VERSION,
            scope=list(scope),
            scope_hashes=hashes,
            count=1,
        )

    def test_match(self):
        h = {"a.sv": "sha256:x"}
        m = self._meta(["a.sv"], h)
        assert lm.is_lock_valid(m, ["a.sv"], h) is True

    def test_scope_mismatch(self):
        h = {"a.sv": "sha256:x"}
        m = self._meta(["a.sv"], h)
        assert lm.is_lock_valid(m, ["b.sv"], {"b.sv": "sha256:x"}) is False

    def test_hash_mismatch(self):
        m = self._meta(["a.sv"], {"a.sv": "sha256:old"})
        assert lm.is_lock_valid(m, ["a.sv"], {"a.sv": "sha256:new"}) is False

    def test_tool_version_mismatch(self):
        m = self._meta(["a.sv"], {"a.sv": "sha256:x"})
        m.schema_version = "0.0"
        assert lm.is_lock_valid(m, ["a.sv"], {"a.sv": "sha256:x"}) is False


# ---------------------------------------------------------------------------
# Wipe
# ---------------------------------------------------------------------------


class TestWipe:
    def test_wipe_idempotent(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        lm.wipe_lock()  # no error when absent
        lm.lock_dir().mkdir(parents=True, exist_ok=True)
        (lm.lock_dir() / "stray.txt").write_text("x", encoding="utf-8")
        lm.wipe_lock()
        assert not lm.lock_dir().exists()


class TestMuxedPath:
    def test_same_basename_in_different_dirs_does_not_collide(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        a = lm.muxed_path("rtl/a/mod.sv")
        b = lm.muxed_path("rtl/b/mod.sv")

        assert a != b
        assert a.parent.name == "a"
        assert b.parent.name == "b"

    def test_parent_escape_rejected(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        try:
            lm.muxed_path("../mod.sv")
        except ValueError:
            return
        raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# Build meta cache
# ---------------------------------------------------------------------------


class TestBuildCache:
    def test_valid_when_match(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        inputs = {"tb/tb.sv": "sha256:tb"}
        lm.save_build_meta({"a.sv": "sha256:x"}, "sha256:img", inputs)
        assert lm.is_build_cache_valid({"a.sv": "sha256:x"}, "sha256:img", inputs)

    def test_invalid_when_hash_differs(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        inputs = {"tb/tb.sv": "sha256:tb"}
        lm.save_build_meta({"a.sv": "sha256:x"}, "sha256:img", inputs)
        assert not lm.is_build_cache_valid({"a.sv": "sha256:y"}, "sha256:img", inputs)

    def test_invalid_when_digest_differs(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        inputs = {"tb/tb.sv": "sha256:tb"}
        lm.save_build_meta({"a.sv": "sha256:x"}, "sha256:old", inputs)
        assert not lm.is_build_cache_valid({"a.sv": "sha256:x"}, "sha256:new", inputs)

    def test_invalid_when_build_input_differs(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        lm.save_build_meta(
            {"a.sv": "sha256:x"},
            "sha256:img",
            {"tb/tb.sv": "sha256:old"},
        )
        assert not lm.is_build_cache_valid(
            {"a.sv": "sha256:x"},
            "sha256:img",
            {"tb/tb.sv": "sha256:new"},
        )

    def test_missing_is_invalid(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        assert not lm.is_build_cache_valid({}, "sha256:x")


# ---------------------------------------------------------------------------
# Mut-harness injection
# ---------------------------------------------------------------------------


_DUT_SOURCE = """\
module dut_top (
  input  logic clk,
  input  logic rst,
  output logic [3:0] q
);
  always_ff @(posedge clk) begin
    if (rst) q <= 4'b0000;
    else     q <= q + 1;
  end
endmodule
"""


class TestHarnessInjection:
    def test_classic_verilog_uses_no_systemverilog_package(self, tmp_path: Path):
        source = "module dut_top(input clk); endmodule\n"
        path = tmp_path / "dut_top.v"
        path.write_text(source, encoding="utf-8")

        package, reader = lm.inject_mut_harness(path, "dut_top")

        text = path.read_text(encoding="utf-8")
        assert not package and reader
        assert "package booley_mut_pkg" not in text
        assert "integer mut_id = 0" in text
        assert lm.remove_mut_harness(path)
        assert path.read_text(encoding="utf-8") == source

    def test_round_trip(self, tmp_path: Path):
        f = tmp_path / "dut_top.sv"
        f.write_text(_DUT_SOURCE, encoding="utf-8")
        pkg, reader = lm.inject_mut_harness(f, "dut_top")
        assert pkg and reader
        txt = f.read_text(encoding="utf-8")
        assert "package booley_mut_pkg" in txt
        assert "$value$plusargs" in txt

        # Idempotent re-call.
        pkg2, reader2 = lm.inject_mut_harness(f, "dut_top")
        assert not pkg2 and not reader2

        # Strip cleanly.
        assert lm.remove_mut_harness(f) is True
        txt = f.read_text(encoding="utf-8")
        assert "package booley_mut_pkg" not in txt
        assert "$value$plusargs" not in txt

    def test_reader_follows_module_header_package_import(self, tmp_path: Path):
        src = """\
module dut_top import dut_pkg::*; #(
  parameter int Width = 8
) (
  input logic clk
);
  logic body_signal;
endmodule
"""
        f = tmp_path / "dut_top.sv"
        f.write_text(src, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        txt = f.read_text(encoding="utf-8")
        header_end = txt.index(");") + 2
        reader = txt.index("// __BOOLEY_MUT_READER_BEGIN__")
        assert reader > header_end
        assert "module dut_top import dut_pkg::*; #(\n" in txt

    def test_missing_module_raises(self, tmp_path: Path):
        f = tmp_path / "broken.sv"
        f.write_text("// no module here\n", encoding="utf-8")
        try:
            lm.inject_mut_harness(f, "nonexistent_top")
        except lm.MutHarnessInjectionError:
            return
        raise AssertionError("expected MutHarnessInjectionError")

    def test_reader_echoes_selected_mut_id_but_not_the_baseline(self, tmp_path: Path):
        # SETUP-F-38: the echo is the runtime proof that +MUT_ID reached the
        # design; MUT_ID=0 must stay silent so a baseline run's stdout is
        # byte-identical to an unmutated one.
        f = tmp_path / "dut_top.sv"
        f.write_text(_DUT_SOURCE, encoding="utf-8")
        lm.inject_mut_harness(f, "dut_top")
        txt = f.read_text(encoding="utf-8")
        assert f'$display("{lm.MUT_ECHO_PREFIX}%0d active", mut_id)' in txt
        assert "if (mut_id != 0) $display" in txt

    def test_partial_rollback_on_missing_module(self, tmp_path: Path):
        # Build a file whose ONLY module name differs from the requested top.
        # The package prepend will succeed but the reader insertion can't —
        # the helper must undo the prepend so the file is unchanged.
        src = "module other_top();\nendmodule\n"
        f = tmp_path / "x.sv"
        f.write_text(src, encoding="utf-8")
        with contextlib.suppress(lm.MutHarnessInjectionError):
            lm.inject_mut_harness(f, "missing_top")
        assert f.read_text(encoding="utf-8") == src


class TestPackageTimescale:
    """SETUP-F-37: a timescale-less package is fatal on a timed design.

    Verilator promotes TIMESCALEMOD ("this unit has no timescale, others do")
    to an error, so the generated package inherits whatever the DUT top it is
    prepended to declares — and stays timescale-free when the design is.
    """

    def test_timeunit_and_timeprecision_are_mirrored_into_the_package(
        self,
        tmp_path: Path,
    ):
        src = "timeunit 1ns;\ntimeprecision 1ps;\n" + _DUT_SOURCE
        f = tmp_path / "dut_top.sv"
        f.write_text(src, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        txt = f.read_text(encoding="utf-8")
        pkg = txt[txt.index("package booley_mut_pkg") : txt.index("endpackage")]
        assert "timeunit 1ns;" in pkg
        assert "timeprecision 1ps;" in pkg

    def test_combined_timeunit_form_carries_the_precision(self, tmp_path: Path):
        f = tmp_path / "dut_top.sv"
        f.write_text("timeunit 1ns / 10ps;\n" + _DUT_SOURCE, encoding="utf-8")

        assert lm.generate_mut_pkg(f.read_text(encoding="utf-8")) == (
            "package booley_mut_pkg;\n"
            "  timeunit 1ns;\n"
            "  timeprecision 10ps;\n"
            "  int mut_id = 0;\n"
            "endpackage\n"
        )

    def test_timescale_directive_is_re_emitted_above_the_package(self, tmp_path: Path):
        # The file's own `timescale sits *below* the prepended block, so the
        # package would otherwise be uncovered.
        f = tmp_path / "dut_top.sv"
        f.write_text("`timescale 1ns / 1ps\n" + _DUT_SOURCE, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        txt = f.read_text(encoding="utf-8")
        assert txt.index("`timescale 1ns / 1ps") < txt.index("package booley_mut_pkg")
        assert "timeunit" not in txt[: txt.index("endpackage")]

    def test_package_suppresses_declfilename(self, tmp_path: Path):
        # The package is inlined into <dut_top>.sv, so its name can never match
        # the filename — a project linting its sim build with -Wall would fail
        # elaboration on the harness itself.
        f = tmp_path / "dut_top.sv"
        f.write_text(_DUT_SOURCE, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        txt = f.read_text(encoding="utf-8")
        assert "/* verilator lint_off DECLFILENAME */" in txt
        assert "/* verilator lint_on DECLFILENAME */" in txt
        assert lm.remove_mut_harness(f) is True
        assert f.read_text(encoding="utf-8") == _DUT_SOURCE

    def test_reader_goes_below_in_module_time_declarations(self, tmp_path: Path):
        # SV requires timeunit/timeprecision to be first in the module body;
        # an import + initial block above them is a syntax error.
        src = (
            "module dut_top (input logic clk);\n"
            "  timeunit 1ns;\n"
            "  // a comment between the two\n"
            "  timeprecision 1ps;\n"
            "  logic q;\n"
            "endmodule\n"
        )
        f = tmp_path / "dut_top.sv"
        f.write_text(src, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        txt = f.read_text(encoding="utf-8")
        assert txt.index("timeprecision 1ps;") < txt.index("__BOOLEY_MUT_READER_BEGIN__")
        assert txt.index("__BOOLEY_MUT_READER_END__") < txt.index("logic q;")
        assert lm.remove_mut_harness(f) is True
        assert f.read_text(encoding="utf-8") == src

    def test_untimed_design_gets_no_timescale(self, tmp_path: Path):
        f = tmp_path / "dut_top.sv"
        f.write_text(_DUT_SOURCE, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        txt = f.read_text(encoding="utf-8")
        assert "timeunit" not in txt
        assert "`timescale" not in txt

    def test_injection_stays_reversible_and_idempotent_with_a_timescale(
        self,
        tmp_path: Path,
    ):
        src = "`timescale 1ns / 1ps\n" + _DUT_SOURCE
        f = tmp_path / "dut_top.sv"
        f.write_text(src, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")
        assert lm.inject_mut_harness(f, "dut_top") == (False, False)
        assert lm.remove_mut_harness(f) is True
        assert f.read_text(encoding="utf-8") == src

    def test_included_timescale_header_still_covers_the_package(self, tmp_path: Path):
        # The value lives in an unresolved header, so the package gets a
        # default directive: it holds no time-consuming code, and the include
        # below restores the design's real value before the module.
        f = tmp_path / "dut_top.sv"
        f.write_text('`include "timescale.vh"\n' + _DUT_SOURCE, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        txt = f.read_text(encoding="utf-8")
        assert txt.index("`timescale 1ns / 1ps") < txt.index("package booley_mut_pkg")
        assert txt.index("package booley_mut_pkg") < txt.index('`include "timescale.vh"')

    def test_included_timescale_header_injection_is_reversible(self, tmp_path: Path):
        src = '`include "rtl/inc/timescale.svh"\n' + _DUT_SOURCE
        f = tmp_path / "dut_top.sv"
        f.write_text(src, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")
        assert lm.inject_mut_harness(f, "dut_top") == (False, False)
        assert lm.remove_mut_harness(f) is True
        assert f.read_text(encoding="utf-8") == src

    def test_unrelated_include_is_not_guessed_at(self, tmp_path: Path):
        # No evidence of a timescale anywhere: guessing one here would
        # redefine the time unit of a design that never asked for it.
        f = tmp_path / "dut_top.sv"
        f.write_text('`include "defines.vh"\n' + _DUT_SOURCE, encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        assert "`timescale" not in f.read_text(encoding="utf-8")

    def test_timescale_include_below_the_module_is_left_alone(self, tmp_path: Path):
        # Our directive would leak into the module above the include, so the
        # ordering guard declines to emit one.
        f = tmp_path / "dut_top.sv"
        f.write_text(_DUT_SOURCE + '`include "timescale.vh"\n', encoding="utf-8")

        lm.inject_mut_harness(f, "dut_top")

        assert "`timescale 1ns / 1ps" not in f.read_text(encoding="utf-8")

    def test_real_timescale_wins_over_an_included_header(self, tmp_path: Path):
        f = tmp_path / "dut_top.sv"
        f.write_text(
            '`include "timescale.vh"\n`timescale 10ps / 1ps\n' + _DUT_SOURCE,
            encoding="utf-8",
        )

        assert lm.generate_mut_pkg(f.read_text(encoding="utf-8")) == (
            "package booley_mut_pkg;\n  int mut_id = 0;\nendpackage\n"
        )
        lm.inject_mut_harness(f, "dut_top")
        txt = f.read_text(encoding="utf-8")
        assert txt.index("`timescale 10ps / 1ps") < txt.index("package booley_mut_pkg")


# ---------------------------------------------------------------------------
# DUT top resolution
# ---------------------------------------------------------------------------


class TestFindDutTopFile:
    def test_match_by_basename(self, tmp_path: Path):
        # Greenfield/fixture path: file doesn't exist on disk, falls back
        # to basename stem matching.
        p = lm.find_dut_top_file(
            "dut_top",
            ["rtl/dut_top.sv"],
            tmp_path,
        )
        assert p == tmp_path / "rtl/dut_top.sv"

    def test_no_match(self, tmp_path: Path):
        assert lm.find_dut_top_file("missing", ["rtl/other.sv"], tmp_path) is None

    def test_absolute_path_preserved(self, tmp_path: Path):
        abs_file = (tmp_path / "rtl/dut_top.sv").resolve()
        p = lm.find_dut_top_file("dut_top", [str(abs_file)], tmp_path)
        assert p == abs_file

    def test_match_by_declaration_when_basename_differs(self, tmp_path: Path):
        # The c-8x3-priority-encoder bug: file named priority_encoder.v
        # contains `module priority_encoder_8x3`.  Must locate by parsing
        # the declaration, not by filename basename.
        rtl_dir = tmp_path / "rtl"
        rtl_dir.mkdir()
        f = rtl_dir / "priority_encoder.v"
        f.write_text(
            "module priority_encoder_8x3 (input [7:0] in, output [2:0] out);\nendmodule\n",
            encoding="utf-8",
        )
        p = lm.find_dut_top_file(
            "priority_encoder_8x3",
            ["rtl/priority_encoder.v"],
            tmp_path,
        )
        assert p == f

    def test_declaration_match_beats_basename(self, tmp_path: Path):
        # When multiple candidates exist and one declares the target
        # module while another only shares its basename, prefer the
        # declaration hit.
        rtl_dir = tmp_path / "rtl"
        rtl_dir.mkdir()
        decoy = rtl_dir / "foo.v"  # basename-stem == "foo" but unrelated module
        decoy.write_text("module unrelated;\nendmodule\n", encoding="utf-8")
        real = rtl_dir / "bar.v"
        real.write_text("module foo (input a);\nendmodule\n", encoding="utf-8")
        p = lm.find_dut_top_file(
            "foo",
            ["rtl/foo.v", "rtl/bar.v"],
            tmp_path,
        )
        assert p == real

    def test_commented_out_declaration_ignored(self, tmp_path: Path):
        # A `// module foo` inside a comment must NOT count as a real
        # declaration, otherwise stale commented-out code masks the
        # real top file.
        rtl_dir = tmp_path / "rtl"
        rtl_dir.mkdir()
        decoy = rtl_dir / "decoy.v"
        decoy.write_text(
            "// module foo (input a);  -- old name, removed\nmodule decoy_top;\nendmodule\n",
            encoding="utf-8",
        )
        real = rtl_dir / "real.v"
        real.write_text("module foo (input a);\nendmodule\n", encoding="utf-8")
        p = lm.find_dut_top_file(
            "foo",
            ["rtl/decoy.v", "rtl/real.v"],
            tmp_path,
        )
        assert p == real

    def test_block_comment_declaration_ignored(self, tmp_path: Path):
        rtl_dir = tmp_path / "rtl"
        rtl_dir.mkdir()
        decoy = rtl_dir / "decoy.v"
        decoy.write_text(
            "/* module foo;\nendmodule */\nmodule decoy_top;\nendmodule\n",
            encoding="utf-8",
        )
        real = rtl_dir / "real.v"
        real.write_text("module foo (input a);\nendmodule\n", encoding="utf-8")
        p = lm.find_dut_top_file(
            "foo",
            ["rtl/decoy.v", "rtl/real.v"],
            tmp_path,
        )
        assert p == real

    def test_word_boundary_required(self, tmp_path: Path):
        # `module foobar` must NOT satisfy a search for `foo` — the regex
        # uses \b on both sides of the module name.
        rtl_dir = tmp_path / "rtl"
        rtl_dir.mkdir()
        f = rtl_dir / "foobar.v"
        f.write_text("module foobar;\nendmodule\n", encoding="utf-8")
        # No file declares `module foo`, no file is named `foo.v` —
        # expect None (not a false-positive hit on `foobar`).
        assert (
            lm.find_dut_top_file(
                "foo",
                ["rtl/foobar.v"],
                tmp_path,
            )
            is None
        )
