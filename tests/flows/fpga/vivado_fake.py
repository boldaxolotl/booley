"""A fake of Vivado at the FPGA Flow's command boundary, and a Project that runs it.

``FakeVivado.install`` marks this process as a Sandbox through ``BOOLEY_CONTAINER``,
the variable ``inside_session_runtime`` reads, and replaces ``fpga_run_command``, the
one function that names the command the Flow runs, with a real child process. The
child logs its arguments, writes the routed reports, run log and bitstream Vivado
would leave under the work root, prints ``stdout`` and exits with ``returncode``.
Everything between the Flow's entry point and that command runs for real: FuseSoC
setup, Edalize configure, the profile patches, report parsing, and the cache.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from booley.flows.fpga.backends.vivado import edam as fpga_edam
from booley.flows.fpga.flow import FpgaImplFlow
from booley.runtime.endpoint_execution import EndpointOutcome

_CHILD = """\
import json, sys
from pathlib import Path

work_root, settings_file, *args = sys.argv[1:]
settings = json.loads(Path(settings_file).read_text(encoding="utf-8"))
with (Path(settings_file).parent / "vivado_calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
impl = Path(work_root) / "fake.runs" / "impl_1"
impl.mkdir(parents=True, exist_ok=True)
if settings["route"]:
    reports = {
        "top_utilization_placed.rpt": "| Slice LUTs | 18 |\\n| Slice Registers | 36 |\\n",
        "top_timing_summary_routed.rpt": "WNS(ns) : 0.5\\nWHS(ns) : 0.1\\n",
        "top_drc_routed.rpt": "No violations found.\\n",
        "runme.log": "route_design completed successfully\\n",
    }
    for name, text in reports.items():
        (impl / name).write_bytes(text.encode())
    (impl / "top.bit").write_bytes(b"bitstream")
sys.stdout.write(settings["stdout"])
sys.exit(settings["returncode"])
"""

# A Target FuseSoC resolves for real: one source, one constraint file, an FPGA part.
_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files:
      - rtl/top.sv: {file_type: systemVerilogSource}
      - timing.xdc: {file_type: xdc}
parameters:
  WIDTH: {datatype: DATATYPE, default: DEFAULT, paramtype: vlogparam}
targets:
  fpga:
    default_tool: vivado
    flow: generic
    flow_options: {tool: vivado, part: xc7a35tcpg236-1}
    filesets: [rtl]
    parameters: [WIDTH]
    toplevel: top
"""


def write_fpga_project(root: Path, *, width: str = "int", default: str = "8") -> Path:
    """Write a Project with one ``fpga`` Target whose ``WIDTH`` has *width* datatype."""
    files = {
        "demo.core": _CORE.replace("DATATYPE", width).replace("DEFAULT", default),
        "rtl/top.sv": "module top #(parameter WIDTH = 8); endmodule\n",
        "timing.xdc": "create_clock -period 10 [get_ports clk]\n",
        ".booley/booley.toml": "[flows.fpga]\n\n[jobs.seats]\nvivado = 1\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode())
    return root


# Edalize leaves its Tcl file handle unclosed when it regenerates a project, and a
# second run of the same Target does exactly that.
EDALIZE_LEAKS_HANDLE = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)


class FakeVivado:
    """Vivado's answer to the next runs; ``calls`` lists the arguments each run received."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.script = directory / "fake_vivado.py"
        self.script.write_bytes(_CHILD.encode())
        self.settings = directory / "vivado.json"
        self.answer()

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch, project: Path) -> FakeVivado:
        """Answer for Vivado in runs of *project*."""
        directory = project.parent / "vivado"
        fake = cls(directory)
        monkeypatch.setenv("BOOLEY_DIR", str(project / ".booley"))
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(directory / "runtime"))
        # The Edalize Makefile's ``make`` and Vivado are the boundary; the work root
        # the Flow passes is where they would write.
        monkeypatch.setattr(
            fpga_edam,
            "fpga_run_command",
            lambda work_root, _work_dir: [
                sys.executable,
                str(fake.script),
                str(work_root),
                str(fake.settings),
            ],
        )
        return fake

    def answer(self, *, stdout: str = "", returncode: int = 0, route: bool = True) -> None:
        settings = {"stdout": stdout, "returncode": returncode, "route": route}
        self.settings.write_bytes(json.dumps(settings).encode())

    def calls(self) -> list[list[str]]:
        log = self.settings.parent / "vivado_calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def run_fpga(project: Path, *args: str) -> tuple[EndpointOutcome, dict]:
    """Run the FPGA Flow on the ``fpga`` Target as the CLI does; return its outcome and report."""
    reports = project / "reports"
    flow = FpgaImplFlow()
    flow.parse_args(
        ["--target", "fpga", "--project", str(project), "--report-dir", str(reports), *args]
    )
    outcome = flow.execute_prepared().outcome
    report = reports / "fpga_fpga.json"
    return outcome, json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
