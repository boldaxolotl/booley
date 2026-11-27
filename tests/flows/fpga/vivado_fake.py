"""The one fake of the FPGA Flow's Vivado command, and a Project that runs it.

``FakeVivado.install`` marks this process as a Sandbox through ``BOOLEY_CONTAINER``,
the variable ``inside_session_runtime`` reads, and replaces ``fpga_run_command``, the
one function that names the command the Flow runs, with a real child process. The
child logs its arguments, writes the run log, the routed reports and the bitstream
Vivado would leave under the work root, prints ``stdout`` and ``stderr`` and exits
with ``returncode``. The reports state the answer's metrics as Vivado's report text,
so the real parser reads them back. Everything between the Flow's entry point and
that command runs for real: FuseSoC setup, Edalize configure, the profile patches,
report parsing, seat admission and the cache.
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
import json, os, sys
from pathlib import Path

work_root, answer_file, *args = sys.argv[1:]
answer = json.loads(Path(answer_file).read_text(encoding="utf-8"))
with (Path(answer_file).parent / "vivado_calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
impl = Path(work_root) / "top.runs" / "impl_1"
impl.mkdir(parents=True, exist_ok=True)
for name, text in answer["files"].items():
    (impl / name).write_bytes(text.encode())
if answer["files"] and answer["broken_report"]:  # one required report left missing, empty or stale
    name, fault = answer["broken_report"].split(":")
    report = impl / f"top_{name}.rpt"
    if fault == "missing":
        report.unlink()
    elif fault == "empty":
        report.write_bytes(b"")
    else:
        os.utime(report, (0, 0))
sys.stdout.write(answer["stdout"])
sys.stderr.write(answer["stderr"])
sys.exit(answer["returncode"])
"""

# The metrics a routed run reports unless a test states others.
METRICS = {"lut_count": 1, "ff_count": 1, "wns_ns": 0.1, "whs_ns": 0.1}
_UTILIZATION_ROWS = {
    "lut_count": "Slice LUTs",
    "ff_count": "Slice Registers",
    "bram_count": "Block RAM Tile",
    "dsp_count": "DSPs",
    "latch_count": "Register as Latch",
}
_METRIC_KEYS = {*_UTILIZATION_ROWS, "wns_ns", "whs_ns", "comb_loop_count", "per_clock"}
_ANSWER_KEYS = {"stdout", "stderr", "returncode", "route", "metrics", "broken_report"}


def _routed_files(metrics: dict) -> dict[str, str]:
    """The run log, reports and bitstream of a routed run whose reports state *metrics*."""
    given = {key: value for key, value in metrics.items() if value is not None}
    rows = (
        f"| {label} | {given[key]} |" for key, label in _UTILIZATION_ROWS.items() if key in given
    )
    clocks = given.get("per_clock", {})
    timing = [
        *(
            f"{name}(ns) : {given[key]}"
            for name, key in (("WNS", "wns_ns"), ("WHS", "whs_ns"))
            if key in given
        ),
        f"There are {given.get('comb_loop_count', 0)} combinational loops in the design.",
        "Clock Summary",
        *(f"{name} {{0 1}} {clock['period_ns']} 0" for name, clock in clocks.items()),
        "Intra Clock Table",
        *(f"{name} {clock['wns_ns']} 0 0 0 {clock['whs_ns']}" for name, clock in clocks.items()),
    ]
    return {
        "runme.log": "route_design completed successfully\n",
        "top_utilization_placed.rpt": "\n".join(["Utilization", *rows]) + "\n",
        "top_timing_summary_routed.rpt": "\n".join(timing) + "\n",
        "top_drc_routed.rpt": "No violations found.\n",
        "top.bit": "bitstream",
    }


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
        self.directory = directory
        self.script = directory / "fake_vivado.py"
        self.script.write_bytes(_CHILD.encode())
        self.answers: dict = {}

    @classmethod
    def install(
        cls, monkeypatch: pytest.MonkeyPatch, project: Path, directory: Path | None = None
    ) -> FakeVivado:
        """Answer for Vivado in runs of *project*, keeping the fake's files in *directory*."""
        fake = cls(directory or project.parent / "vivado")
        monkeypatch.setenv("BOOLEY_DIR", str(project / ".booley"))
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(fake.directory / "runtime"))
        # The Edalize Makefile's ``make`` and Vivado are the boundary; the work root
        # the Flow passes is where they would write.
        monkeypatch.setattr(fpga_edam, "fpga_run_command", fake._command)  # boundary: runs Vivado
        return fake

    def answer(self, **answers: object) -> None:
        """Set the next runs' ``stdout`` (``{work_root}`` names the run's work root),
        ``stderr``, ``returncode``, ``route`` (default: the run exits 0), the
        ``metrics`` its reports state over ``METRICS`` (``None`` leaves one out) and a
        ``broken_report`` (``"<report>:<fault>"``, fault ``missing``, ``empty`` or
        ``stale``). A list answers one run each, in run order: baseline Targets
        first, then current, each in Target order."""
        metrics = answers.get("metrics", {})
        named = set().union(*(metrics if isinstance(metrics, list) else [metrics]))
        assert not answers.keys() - _ANSWER_KEYS and not named - _METRIC_KEYS, answers
        self.answers = {key: list(v) if isinstance(v, list) else v for key, v in answers.items()}

    def _take(self, key: str, default: object) -> object:
        value = self.answers.get(key, default)
        return value.pop(0) if isinstance(value, list) else value

    def _command(self, work_root: Path, _work_dir: Path) -> list[str]:
        returncode = self._take("returncode", 0)
        metrics = {**METRICS, **self._take("metrics", {})}
        answer = {
            "returncode": returncode,
            "files": _routed_files(metrics) if self._take("route", returncode == 0) else {},
            "broken_report": self._take("broken_report", ""),
            "stdout": self._take("stdout", "").replace(
                "{work_root}", work_root.resolve().as_posix()
            ),
            "stderr": self._take("stderr", ""),
        }
        path = self.directory / f"run{len(list(self.directory.glob('run*.json')))}.json"
        path.write_bytes(json.dumps(answer).encode())
        return [sys.executable, str(self.script), str(work_root), str(path)]

    def calls(self) -> list[list[str]]:
        log = self.directory / "vivado_calls.jsonl"
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
