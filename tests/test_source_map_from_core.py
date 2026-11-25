"""Tests for the .core -> RTL/TB source-map primitives (ADR 0026 follow-through).

Covers ``fusesoc_registry.classified_sources`` and ``source_dirs_from_core`` -
the subprocess-free partition of design sources by the ``tags:[tb]`` marker that
replaced the retired ``[sources.*]`` config as the single source of truth.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from booley.fusesoc.fusesoc_registry import FuseSocError, classified_sources, source_dirs_from_core


def _write_core(path: Path, body: str) -> None:
    """Write a dedented ``.core`` YAML body to *path* (creating parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


# A flat single-file repo (ADR 0026): the .core sits at the Project Root and its
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

# A fileset-level ``tags: [tb]`` marks every file in it as TB.
_FILESET_TAG_CORE = """\
    CAPI=2:
    name: ::d
    filesets:
      rtl: {files: [rtl/a.sv, rtl/b.sv]}
      tb:
        files: [tb/x.sv, tb/y.sv]
        tags: [tb]
    targets:
      sim: {filesets: [rtl, tb], toplevel: x}
"""

# A per-file ``tags: [tb]`` marks just that file, even inside an untagged mixed fileset.
_PER_FILE_TAG_CORE = """\
    CAPI=2:
    name: ::d
    filesets:
      src:
        files:
          - rtl/dut.sv: {file_type: systemVerilogSource}
          - tb/checker.sv: {tags: [tb]}
    targets:
      sim: {filesets: [src], toplevel: checker}
"""

# A non-TB ``is_include_file`` header still invalidates RTL builds, so
# classified_sources keeps it on the RTL side.
_INCLUDE_HEADER_CORE = """\
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
"""

# A .core in a subdir declares fileset paths relative to its own dir.
_SUBDIR_CORE = """\
    CAPI=2:
    name: ::block
    filesets:
      rtl: {files: [rtl/block.sv]}
      tb:
        files: [tb/tb_block.sv]
        tags: [tb]
    targets:
      sim: {filesets: [rtl, tb], toplevel: tb_block}
"""

# An is_include_file entry inside a tb-tagged fileset is both a tb_dir and a tb_include_dir.
_TB_INCLUDE_CORE = """\
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
"""


@pytest.mark.parametrize(
    ("core_path", "body", "rtl", "tb"),
    [
        ("picorv32.core", _FLAT_CORE, ("picorv32.v",), ("testbench.v",)),
        ("dut.core", _STRUCTURED_CORE, ("rtl/dut.sv",), ("tb/tb_dut.sv",)),
        ("d.core", _FILESET_TAG_CORE, ("rtl/a.sv", "rtl/b.sv"), ("tb/x.sv", "tb/y.sv")),
        ("d.core", _PER_FILE_TAG_CORE, ("rtl/dut.sv",), ("tb/checker.sv",)),
        ("d.core", _INCLUDE_HEADER_CORE, ("rtl/defs.svh", "rtl/dut.sv"), ("tb/tb_dut.sv",)),
        # classified_sources re-expresses subdir paths project-root-relative.
        ("ip/block.core", _SUBDIR_CORE, ("ip/rtl/block.sv",), ("ip/tb/tb_block.sv",)),
    ],
    ids=["flat", "structured", "fileset-tag", "per-file-tag", "include-header", "subdir-core"],
)
def test_classified_sources_partition(
    tmp_path: Path, core_path: str, body: str, rtl: tuple[str, ...], tb: tuple[str, ...]
):
    _write_core(tmp_path / core_path, body)

    cs = classified_sources(tmp_path)

    assert cs.rtl_source_files == rtl
    assert cs.tb_files == tb


def test_no_core_returns_empty(tmp_path: Path):
    cs = classified_sources(tmp_path)

    assert cs.rtl_source_files == ()
    assert cs.tb_files == ()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # ADR 0026 flat repo: a root-level file appears as its own verbatim path, not ".".
        (_FLAT_CORE, (["picorv32.v"], ["testbench.v"], [])),
        (_STRUCTURED_CORE, (["rtl"], ["tb"], [])),
        (_TB_INCLUDE_CORE, (["rtl"], ["checks", "verif"], ["checks"])),
    ],
    ids=["flat", "structured", "tb-include"],
)
def test_source_dirs_from_core(tmp_path: Path, body: str, expected: tuple[list[str], ...]):
    _write_core(tmp_path / "d.core", body)

    assert source_dirs_from_core(tmp_path) == expected


def test_core_source_symlink_normalizes_to_tracked_repo_path(tmp_path: Path):
    """A core in the Booley Directory may expose repository sources through a symlink."""
    (tmp_path / "src" / "rtl").mkdir(parents=True)
    (tmp_path / "src" / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    core_dir = tmp_path / ".booley" / "cores"
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


def test_no_core_raises(tmp_path: Path):
    # ADR 0039: never guess the partition - a silently wrong one fed the
    # Specialist Source Isolation boundary.
    with pytest.raises(
        FuseSocError, match=r"category isolation requires a usable \.core; run booley setup"
    ):
        source_dirs_from_core(tmp_path)
