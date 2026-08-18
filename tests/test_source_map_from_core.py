"""Tests for the .core → RTL/TB source-map primitives (ADR 0026 follow-through).

Covers ``fusesoc_registry.classified_sources`` and ``source_dirs_from_core`` —
the subprocess-free partition of design sources by the ``tags:[tb]`` marker that
replaced the retired ``[sources.*]`` config as the single source of truth — plus
the ``_diff_classify._classify_files`` drift fix that keys off exact declared
files rather than conventional ``tb/`` directory prefixes.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booley.dev_support.development_state import CATEGORY_RTL, CATEGORY_TB
from booley.fusesoc.fusesoc_registry import classified_sources, source_dirs_from_core
from booley.mcp.diff_classify import _classify_files


def _write_core(path: Path, body: str) -> None:
    """Write a dedented ``.core`` YAML body to *path* (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


# A flat single-file repo (ADR 0026): the .core sits at the project root and its
# fileset paths are root-relative, so a root file classifies by verbatim path.
_FLAT_CORE = """\
    CAPI=2:
    name: ::picorv32
    filesets:
      rtl:
        files:
          - picorv32.v: {file_type: verilogSource}
      tb:
        files:
          - testbench.v: {file_type: verilogSource}
        tags: [tb]
    targets:
      sim: {filesets: [rtl, tb], toplevel: testbench}
"""

# A structured repo: sources live under rtl/ and tb/ subdirs.
_STRUCTURED_CORE = """\
    CAPI=2:
    name: ::dut
    filesets:
      rtl:
        files:
          - rtl/dut.sv: {file_type: systemVerilogSource}
      tb:
        files:
          - tb/tb_dut.sv: {file_type: systemVerilogSource}
        tags: [tb]
    targets:
      sim: {filesets: [rtl, tb], toplevel: tb_dut}
"""


# ---------------------------------------------------------------------------
# classified_sources — project-wide RTL/TB file partition
# ---------------------------------------------------------------------------


class TestClassifiedSources:
    def test_flat_repo_root_files(self, tmp_path: Path):
        _write_core(tmp_path / "picorv32.core", _FLAT_CORE)
        cs = classified_sources(tmp_path)
        assert cs.rtl_source_files == ("picorv32.v",)
        assert cs.tb_files == ("testbench.v",)

    def test_structured_repo_subdir_files(self, tmp_path: Path):
        _write_core(tmp_path / "dut.core", _STRUCTURED_CORE)
        cs = classified_sources(tmp_path)
        assert cs.rtl_source_files == ("rtl/dut.sv",)
        assert cs.tb_files == ("tb/tb_dut.sv",)

    def test_fileset_level_tb_tag_partitions_whole_fileset(self, tmp_path: Path):
        # A fileset-level ``tags: [tb]`` marks every file in it as TB.
        _write_core(
            tmp_path / "d.core",
            """\
            CAPI=2:
            name: ::d
            filesets:
              rtl: {files: [rtl/a.sv, rtl/b.sv]}
              tb:
                files: [tb/x.sv, tb/y.sv]
                tags: [tb]
            targets:
              sim: {filesets: [rtl, tb], toplevel: x}
        """,
        )
        cs = classified_sources(tmp_path)
        assert cs.rtl_source_files == ("rtl/a.sv", "rtl/b.sv")
        assert cs.tb_files == ("tb/x.sv", "tb/y.sv")

    def test_per_file_tb_tag_partitions_single_file(self, tmp_path: Path):
        # A per-file ``tags: [tb]`` marks just that file, even inside an
        # otherwise-untagged mixed fileset.
        _write_core(
            tmp_path / "d.core",
            """\
            CAPI=2:
            name: ::d
            filesets:
              src:
                files:
                  - rtl/dut.sv: {file_type: systemVerilogSource}
                  - tb/checker.sv: {tags: [tb]}
            targets:
              sim: {filesets: [src], toplevel: checker}
        """,
        )
        cs = classified_sources(tmp_path)
        assert cs.rtl_source_files == ("rtl/dut.sv",)
        assert cs.tb_files == ("tb/checker.sv",)

    def test_include_header_lands_in_rtl(self, tmp_path: Path):
        # A non-TB ``is_include_file`` header still invalidates RTL builds, so
        # classified_sources keeps it on the RTL side (unlike target_source_files).
        _write_core(
            tmp_path / "d.core",
            """\
            CAPI=2:
            name: ::d
            filesets:
              rtl:
                files:
                  - rtl/dut.sv: {file_type: systemVerilogSource}
                  - rtl/defs.svh: {is_include_file: true}
              tb:
                files: [tb/tb_dut.sv]
                tags: [tb]
            targets:
              sim: {filesets: [rtl, tb], toplevel: tb_dut}
        """,
        )
        cs = classified_sources(tmp_path)
        assert cs.rtl_source_files == ("rtl/defs.svh", "rtl/dut.sv")
        assert cs.tb_files == ("tb/tb_dut.sv",)

    def test_subdir_core_paths_normalized_to_project_root(self, tmp_path: Path):
        # A .core in a subdir declares fileset paths relative to its own dir;
        # classified_sources re-expresses them project-root-relative.
        _write_core(
            tmp_path / "ip" / "block.core",
            """\
            CAPI=2:
            name: ::block
            filesets:
              rtl: {files: [rtl/block.sv]}
              tb:
                files: [tb/tb_block.sv]
                tags: [tb]
            targets:
              sim: {filesets: [rtl, tb], toplevel: tb_block}
        """,
        )
        cs = classified_sources(tmp_path)
        assert cs.rtl_source_files == ("ip/rtl/block.sv",)
        assert cs.tb_files == ("ip/tb/tb_block.sv",)

    def test_no_core_returns_empty(self, tmp_path: Path):
        cs = classified_sources(tmp_path)
        assert cs.rtl_source_files == ()
        assert cs.tb_files == ()


# ---------------------------------------------------------------------------
# source_dirs_from_core — directory-granularity view
# ---------------------------------------------------------------------------


class TestSourceDirsFromCore:
    def test_root_file_yields_verbatim_path(self, tmp_path: Path):
        # ADR 0026 flat repo: a root-level file appears as its own verbatim path
        # (mirroring what [sources.*].source_dirs used to list), not ".".
        _write_core(tmp_path / "picorv32.core", _FLAT_CORE)
        rtl_dirs, tb_dirs, tb_incl = source_dirs_from_core(tmp_path)
        assert rtl_dirs == ["picorv32.v"]
        assert tb_dirs == ["testbench.v"]
        assert tb_incl == []

    def test_subdir_file_yields_parent_dir(self, tmp_path: Path):
        _write_core(tmp_path / "dut.core", _STRUCTURED_CORE)
        rtl_dirs, tb_dirs, tb_incl = source_dirs_from_core(tmp_path)
        assert rtl_dirs == ["rtl"]
        assert tb_dirs == ["tb"]
        assert tb_incl == []

    def test_core_source_symlink_normalizes_to_tracked_repo_path(self, tmp_path: Path):
        """A project-dir core may expose repository sources through a symlink."""
        (tmp_path / "src" / "rtl").mkdir(parents=True)
        (tmp_path / "src" / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
        core_dir = tmp_path / ".booley_project" / "cores"
        core_dir.mkdir(parents=True)
        (core_dir / "src").symlink_to(tmp_path / "src", target_is_directory=True)
        _write_core(
            core_dir / "dut.core",
            """\
            CAPI=2:
            name: ::dut
            filesets:
              rtl: {files: [src/rtl/dut.sv]}
            targets:
              lint: {filesets: [rtl], toplevel: dut}
        """,
        )

        rtl_dirs, _tb_dirs, _tb_incl = source_dirs_from_core(tmp_path)

        assert rtl_dirs == ["src/rtl"]

    def test_tb_include_dirs_from_is_include_tb_files(self, tmp_path: Path):
        # An is_include_file entry inside a tb-tagged fileset surfaces both as a
        # tb_dir and as a tb_include_dir.
        _write_core(
            tmp_path / "d.core",
            """\
            CAPI=2:
            name: ::d
            filesets:
              rtl: {files: [rtl/dut.sv]}
              tb:
                files:
                  - verif/tb_top.sv: {file_type: systemVerilogSource}
                  - checks/asserts.svh: {is_include_file: true}
                tags: [tb]
            targets:
              sim: {filesets: [rtl, tb], toplevel: tb_top}
        """,
        )
        rtl_dirs, tb_dirs, tb_incl = source_dirs_from_core(tmp_path)
        assert rtl_dirs == ["rtl"]
        assert tb_dirs == ["checks", "verif"]  # sorted
        assert tb_incl == ["checks"]

    def test_no_core_raises(self, tmp_path: Path):
        # ADR 0039: never guess the partition — a silently wrong one fed the
        # Specialist Source Isolation boundary. The old hardcoded
        # (["rtl", "fw"], ["tb"]) fallback is gone.
        import pytest

        from booley.fusesoc.fusesoc_registry import FuseSocError

        with pytest.raises(FuseSocError, match=r"no \.core"):
            source_dirs_from_core(tmp_path)


# ---------------------------------------------------------------------------
# _classify_files — the drift fix (exact declared files, not tb/ prefixes)
# ---------------------------------------------------------------------------


class TestClassifyFilesDriftFix:
    def test_tb_tagged_file_outside_tb_dir_classifies_as_tb(self, tmp_path: Path):
        # The drift fix: a tags:[tb] file living OUTSIDE any conventional tb/
        # directory is still TB — the prefix heuristic would have missed it.
        _write_core(
            tmp_path / "d.core",
            """\
            CAPI=2:
            name: ::d
            filesets:
              rtl: {files: [rtl/dut.sv]}
              tb:
                files: [sim/checker.sv]
                tags: [tb]
            targets:
              sim: {filesets: [rtl, tb], toplevel: checker}
        """,
        )
        assert _classify_files(["sim/checker.sv"], tmp_path) == {CATEGORY_TB}
        assert _classify_files(["rtl/dut.sv"], tmp_path) == {CATEGORY_RTL}

    def test_unregistered_source_classifies_as_neither(self, tmp_path: Path):
        # A path declared by no fileset is unclassified (empty set) — surfaced
        # separately by the reviewer, never silently bucketed as RTL or TB.
        _write_core(
            tmp_path / "d.core",
            """\
            CAPI=2:
            name: ::d
            filesets:
              rtl: {files: [rtl/dut.sv]}
              tb:
                files: [tb/tb_dut.sv]
                tags: [tb]
            targets:
              sim: {filesets: [rtl, tb], toplevel: tb_dut}
        """,
        )
        assert _classify_files(["misc/other.sv"], tmp_path) == set()
