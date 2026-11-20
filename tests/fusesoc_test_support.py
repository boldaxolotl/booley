"""Shared FuseSoC core fixtures for adapter and Simulation tests."""

from __future__ import annotations

import subprocess
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path

from booley.fusesoc.fusesoc_registry import read_core
from booley.targets.domain import FuseSocError
from tests.conftest import symlink_or_skip

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

# #5077: children merge a base with ``<<:`` and add their own ``flow_options`` keys.
INHERITED_CORE_TEXT = textwrap.dedent(
    """\
    CAPI=2:
    name: booley::inherit:0
    filesets:
      rtl:
        files: [rtl/top.sv]
    targets:
      synth_base: &synth_base
        flow: generic
        filesets: [rtl]
        flow_options:
          synth_mode: logical
          frontend: slang
          booley: {doctor: [synth]}
      synth_child:
        <<: *synth_base
        flow_options: {tool: yosys, arch: xilinx}
      sim_base: &sim_base
        flow: sim
        filesets: [rtl]
        flow_options: {cocotb_module: test_top, verilator_options: [--timing]}
      sim_child:
        <<: *sim_base
        flow_options: {tool: verilator}
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


def symlinked_booley_dir(project: Path, external: Path) -> Path:
    """Make ``<project>/.booley`` a directory symlink to *external* (#2157).

    Creates *external* and its ``cores/`` directory; skips the test on hosts
    that cannot create symlinks. Returns *external*.
    """
    (external / "cores").mkdir(parents=True)
    symlink_or_skip(project / ".booley", external, target_is_directory=True)
    return external


def relocated_project(project: Path, directory: str = "state") -> Path:
    """Relocate *project*'s Booley Directory to ``<project>/<directory>`` (#2917).

    Writes the root ``booley.toml`` ``[project].dir`` entry and creates the
    directory. Returns it.
    """
    booley_dir = project / directory
    booley_dir.mkdir(parents=True, exist_ok=True)
    (project / "booley.toml").write_text(f'[project]\ndir = "{directory}"\n', encoding="utf-8")
    return booley_dir


def fusesoc_setup_runner(
    *,
    edam: tuple[Path, str] | None = None,
    edams: Mapping[str, tuple[str, str]] | None = None,
    returncode: int = 0,
    stderr: str = "",
    error: OSError | None = None,
    calls: list[dict[str, object]] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Stand-in for the ``fusesoc run --setup`` subprocess.

    Records each call (``cmd`` plus keywords) in *calls*, raises *error* when
    given, else writes ``edam`` (path, text) as FuseSoC would and returns
    *returncode* with *stderr*. *edams* maps each core VLNV (the positional
    argument) to an EDAM (path relative to the command's ``--build-root``,
    text), written only for the core being set up.
    """

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append({"cmd": cmd, **kwargs})
        if error is not None:
            raise error
        if edam is not None:
            path, text = edam
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if edams is not None and cmd[-1] in edams:
            relative, text = edams[cmd[-1]]
            path = Path(cmd[cmd.index("--build-root") + 1]) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    return run
