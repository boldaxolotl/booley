"""Shared FuseSoC core fixtures for adapter and Simulation tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

from booley.fusesoc.fusesoc_registry import read_core
from booley.targets.domain import FuseSocError

CORE_TEXT = textwrap.dedent(
    """\
    CAPI=2:
    name: ::demo_core:0
    description: spike core

    filesets:
      rtl:
        files:
          - rtl/counter_pkg.sv: {file_type: systemVerilogSource}
          - rtl/counter.sv: {file_type: systemVerilogSource}
      tb:
        files:
          - tb/tb_counter.sv: {file_type: systemVerilogSource}
        tags: [tb]

    targets:
      default:
        filesets: [rtl]
      sim:
        default_tool: verilator
        flow: sim
        flow_options:
          tool: verilator
        filesets: [rtl, tb]
        toplevel: tb_counter
    """
)


def touch_declared_sources(core: Path) -> None:
    """Create empty files for every literal fileset path *core* declares."""
    try:
        doc = read_core(core)
    except FuseSocError:
        return
    filesets = doc.get("filesets")
    if not isinstance(filesets, dict):
        return
    for fileset in filesets.values():
        files = fileset.get("files") if isinstance(fileset, dict) else None
        if not isinstance(files, list):
            continue
        for entry in files:
            if isinstance(entry, str):
                name = entry
            elif isinstance(entry, dict) and len(entry) == 1:
                name = str(next(iter(entry)))
            else:
                continue
            if name.endswith("booley_vcd_dump.sv"):
                continue
            path = core.parent / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()


def write_core(directory: Path, text: str = CORE_TEXT, *, create_sources: bool = True) -> Path:
    """Write a core fixture and create its declared source files by default."""
    directory.mkdir(parents=True, exist_ok=True)
    core = directory / "design.core"
    core.write_text(text, encoding="utf-8")
    if create_sources:
        touch_declared_sources(core)
    return core
