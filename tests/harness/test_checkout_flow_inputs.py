"""A checkout Flow run reads every input from the exported checkout, never from Main.

The live Project is a Git repository whose Main Booley Directory is poisoned
after the commit is exported: its settings, ``tests.toml`` and cores differ from
the commit's, and the live working tree's sources are modified. The Flow runs for
real in the checkout, as a child process, against fake EDA tools. Each tool fails
when it is handed a poisoned source, so a run that read any input from Main or
from the live tree fails, and the run that reads only the checkout passes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from booley.harness import checkout_flow
from booley.runtime import booley_dir, runtime_context, sandbox_layout
from booley.runtime.commit_export import export_commit
from tests.harness.test_checkout_flow import git, linux_only
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S

TIMEOUT_S = SUBPROCESS_TIMEOUT_S

# The commit's design: one core with a Target per Flow. The HDL is never compiled
# for real; the fake tools stand in for the simulator, linter and synthesizer.
CORE = textwrap.dedent(
    """\
    CAPI=2:
    name: acme:ip:dut:1.0
    filesets:
      rtl:
        files:
          - rtl/dut.sv: {file_type: systemVerilogSource}
      tb:
        files:
          - tb/tb.sv: {file_type: systemVerilogSource}
        tags: [tb]
    targets:
      default:
        filesets: [rtl]
      sim_dut:
        flow: sim
        flow_options: {tool: icarus}
        filesets: [rtl, tb]
        toplevel: tb
      synth_dut:
        flow: generic
        flow_options: {tool: yosys, arch: xilinx, synth_mode: logical}
        filesets: [rtl]
        toplevel: dut
      lint_dut:
        flow: lint
        flow_options: {tool: verilator}
        filesets: [rtl]
        toplevel: dut
    """
)
# The same core in Main: it also compiles a poisoned file.
POISONED_CORE = CORE.replace(
    "- rtl/dut.sv: {file_type: systemVerilogSource}",
    "- rtl/dut.sv: {file_type: systemVerilogSource}\n"
    "      - rtl/poison.sv: {file_type: systemVerilogSource}",
)

# Settings of the commit (committed) and Main (poison). Each Main value would change
# the result of exactly one read if the run took it from there.
COMMITTED_SETTINGS = """\
[flows.sim]
pre_run_commands = ['echo "$BOOLEY_DIR" > {ctl}/presim']
[flows.lint]
warnings_as_errors = false
[flows.synth]
expected_latches = 1
"""
POISONED_SETTINGS = """\
[flows.sim]
pre_run_commands = ["exit 1"]
[flows.lint]
enabled = false
warnings_as_errors = true
[flows.synth]
enabled = false
expected_latches = 0
"""

# What each fake tool does after the shared preamble (see ``write_fake_tools``).
TOOL_BEHAVIOR = {
    "iverilog": 'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then o=$2; shift 2; else shift; fi; done\n'
    'touch "$o"\n',
    "vvp": "echo '[SIM_RESULT] PASSED'\n",
    "verilator": "echo '%Warning-UNUSED: rtl/dut.sv:1:1: Signal is not used: clk'\n",
    "sv2v": 'echo "module dut(input clk); endmodule" > sv2v_converted.v\n',
    "yosys": "cat > stat_dut.txt <<'STAT'\n"
    "13. Printing statistics.\n=== dut ===\n  16 36.176 cells\n  1 - $_DLATCH_P_\n  Chip area for module '\\dut': 36.176\nSTAT\n"
    "cat stat_dut.txt\n"
    "echo 'Found and reported 0 problems.' > premap_check_dut.txt\n"
    "echo 'Found and reported 0 problems.' > check_dut.txt\n"
    "echo 'module dut; endmodule' > synth_dut.v\n",
}
# Fails when the tool is handed a poisoned file, directly or through a file list
# (a ``.scr`` or ``.vc`` command file); records the Booley Directory it saw.
TOOL_PREAMBLE = """\
#!/bin/sh
scan() {{
  [ -f "$1" ] || return 0
  if grep -q POISON "$1" 2>/dev/null; then echo "POISON in $1"; exit 1; fi
}}
echo "$BOOLEY_DIR" > "{ctl}/{name}.booley_dir"
for argument; do
  scan "$argument"
  if [ -f "$argument" ]; then while read -r line; do scan "$line"; done < "$argument"; fi
done
"""


@dataclass(frozen=True)
class PoisonedWorld:
    """The checkout of the commit, the poisoned Main and the tools' control directory."""

    main: Path
    checkout: Path
    report_dir: Path
    ctl: Path

    def seen_booley_dir(self, name: str) -> Path:
        """The Booley Directory the fake tool *name* (or the Pre-Sim Command) saw."""
        text = (self.ctl / name).read_text(encoding="utf-8")
        return Path(text.strip()).resolve()


def write_fake_tools(directory: Path, ctl: Path) -> None:
    """Write the fake EDA tools and the ``python3`` shim into *directory*."""
    directory.mkdir()
    for name, behavior in TOOL_BEHAVIOR.items():
        tool = directory / name
        tool.write_text(TOOL_PREAMBLE.format(ctl=ctl, name=name) + behavior, encoding="utf-8")
        tool.chmod(0o755)
    # The scrubbed environment drops the suite's PYTHONPATH; the Sandbox image installs
    # ``booley`` instead. The shim lets the sim adapter import this checkout's source.
    source = Path(__file__).resolve().parents[2] / "src"
    shim = directory / "python3"
    shim.write_text(
        f'#!/bin/sh\nPYTHONPATH={source} exec {sys.executable} "$@"\n', encoding="utf-8"
    )
    shim.chmod(0o755)


def tree_outside_runtime(checkout: Path) -> set[str]:
    """Every path in *checkout* except the scratch under ``.booley/.runtime``."""
    runtime = checkout / ".booley" / ".runtime"
    return {
        path.relative_to(checkout).as_posix()
        for path in checkout.rglob("*")
        if path != runtime and runtime not in path.parents
    }


@pytest.fixture
def poisoned_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PoisonedWorld]:
    """Export a commit, then poison Main's Booley Directory and the live working tree."""
    for tool in ("fusesoc", "make"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH; the child cannot resolve Targets without it")
    live, ctl = tmp_path / "live", tmp_path / "ctl"
    ctl.mkdir()
    (live / ".booley").mkdir(parents=True)
    for relative, text in {
        "rtl/dut.sv": "module dut(input clk); endmodule\n",
        "tb/tb.sv": "module tb; endmodule\n",
        "dut.core": CORE,
        ".booley/booley.toml": COMMITTED_SETTINGS.format(ctl=ctl),
        ".booley/tests.toml": '[sim_dut]\ntests = ["good"]\n',
    }.items():
        (live / relative).parent.mkdir(parents=True, exist_ok=True)
        (live / relative).write_bytes(text.encode())
    git(live, "init", "-q", "-b", "main")
    git(live, "add", "-A", "-f")
    git(live, "commit", "-q", "-m", "design")
    checkout = tmp_path / "scratch" / "checkout"
    export_commit(live, git(live, "rev-parse", "HEAD"), checkout)

    # Poison Main's Booley Directory and the live working tree.
    (live / ".booley" / "booley.toml").write_bytes(POISONED_SETTINGS.encode())
    (live / ".booley" / "tests.toml").write_bytes(b'[sim_dut]\ntests = ["bad_main_test"]\n')
    (live / ".booley" / "cores").mkdir()
    (live / ".booley" / "cores" / "poison.core").write_bytes(POISONED_CORE.encode())
    (live / "dut.core").write_bytes(POISONED_CORE.encode())
    (live / "rtl" / "poison.sv").write_bytes(b"module poison; // POISON\nendmodule\n")
    (live / "rtl" / "dut.sv").write_bytes(b"module dut(input clk); // POISON\nendmodule\n")

    tools = tmp_path / "fake-bin"
    write_fake_tools(tools, ctl)
    # The Sandbox ships the Nangate library under /opt/pdk; the child imports it from here.
    liberty = tmp_path / "pdk.lib"
    liberty.write_bytes(b"library(cells) {}\n")
    monkeypatch.setattr(
        checkout_flow,
        "_BOOTSTRAP",
        "import sys;sys.path.insert(0,sys.argv.pop(1));"
        "import pathlib;import booley.flows.synth.backends.yosys.discovery as d;"
        f"d.DEFAULT_LIBERTY=pathlib.Path({str(liberty)!r});"
        "from booley.harness.checkout_flow import child_main;raise SystemExit(child_main())",
    )
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    # The verdict must not depend on the mounts of the host that runs the tests.
    monkeypatch.setattr(
        runtime_context,
        "runtime_location",
        lambda: runtime_context.RuntimeLocation.HAND_WRITTEN_SANDBOX,
    )
    monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(live.resolve()))
    for name in ("BOOLEY_DIR", "BOOLEY_INTERNAL_SELFTEST_KIND"):
        monkeypatch.delenv(name, raising=False)
    booley_dir.reset_cache()
    yield PoisonedWorld(live / ".booley", checkout, tmp_path / "reports", ctl)
    booley_dir.reset_cache()


@linux_only
@pytest.mark.parametrize(
    ("flow", "target"),
    [
        pytest.param("sim", "sim_dut", id="sim"),
        pytest.param("synth", "synth_dut", id="synth"),
        pytest.param("lint", "lint_dut", id="lint"),
    ],
)
def test_poisoned_main_flow_returns_the_commits_result(
    poisoned_world: PoisonedWorld, flow: str, target: str
) -> None:
    """Settings, tests, cores and sources of Main and the live tree never reach the run."""
    world = poisoned_world
    before = tree_outside_runtime(world.checkout)

    result = checkout_flow.run_checkout_flow(
        checkout_flow.CheckoutFlowRequest(
            checkout=world.checkout,
            main_booley_dir=world.main,
            report_dir=world.report_dir,
            flow=flow,
            target=target,
            timeout_s=TIMEOUT_S,
        )
    )

    log = result.log.read_text(encoding="utf-8", errors="replace")
    assert result.status is checkout_flow.CheckoutFlowStatus.PASSED, log
    own = (world.checkout / ".booley").resolve()
    tools = {"sim": ("iverilog", "vvp"), "synth": ("sv2v", "yosys"), "lint": ("verilator",)}[flow]
    for tool in tools:
        assert world.seen_booley_dir(f"{tool}.booley_dir") == own, tool
    (report,) = result.reports
    document = json.loads(report.read_text(encoding="utf-8"))
    if flow == "sim":
        assert world.seen_booley_dir("presim") == own  # the Pre-Sim Command, as before
        assert '"good"' in json.dumps(document) and "bad_main_test" not in json.dumps(document)
    if flow == "synth":
        assert '"expected_latches": 1' in json.dumps(document)
    # Nothing but the run's scratch appeared in the checkout, and no shared state with it.
    assert tree_outside_runtime(world.checkout) == before
