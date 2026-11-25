"""The one fake of the FPGA Flow's Vivado command: a Python child process the Flow runs
through its own runner."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from booley.flows import endpoint_admission
from booley.flows.fpga.backends.vivado import edam as fpga_edam
from booley.runtime import runtime_context

# A Python stand-in for Vivado: records each call, then leaves the routed
# reports and bitstream the route witness reads (nothing when it "crashes").
SCRIPT = """\
import os
import sys
from pathlib import Path
work_root, returncode, route = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1"
with (work_root.parent / "vivado-calls").open("a") as calls:
    calls.write("x")
impl = work_root / "top.runs" / "impl_1"
impl.mkdir(parents=True, exist_ok=True)
if route:
    (impl / "runme.log").write_text("route_design completed successfully\\n")
    for name in ("utilization_placed", "timing_summary_routed", "drc_routed"):
        (impl / f"top_{name}.rpt").write_text(name)
    (impl / "top.bit").write_bytes(b"bit")
    if sys.argv[6]:  # one required report left missing, empty or stale
        name, fault = sys.argv[6].split(":")
        report = impl / f"top_{name}.rpt"
        if fault == "missing":
            report.unlink()
        elif fault == "empty":
            report.write_text("")
        else:
            os.utime(report, (0, 0))
print(sys.argv[4])
sys.stderr.write(sys.argv[5])
sys.exit(returncode)
"""


def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Run the whole FPGA Flow on real Edalize configure with a fake Vivado process.

    Returns the state a test bends: ``parsed`` reports, process ``returncode``,
    ``route``, ``output``, ``stderr`` and ``broken_report`` (``"<report>:<fault>"``,
    fault ``missing``, ``empty`` or ``stale``). ``returncode``, ``route``, ``stderr``
    and ``parsed`` may be lists, consumed one per Vivado run in run order (baseline
    Targets first, then current, each in Target order). Also holds the ``recipe``
    the Flow built.
    """
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(endpoint_admission, "required_seats", lambda _tools, _config: ())
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    script = tmp_path / "fake_vivado.py"
    script.write_bytes(SCRIPT.encode())
    state: dict = {
        "parsed": {"status": "pass", "lut_count": 1, "ff_count": 1, "wns_ns": 0.1, "whs_ns": 0.1},
        "returncode": 0,
    }
    real_build = fpga_edam.build_fpga_edam

    def build(recipe, **kwargs):
        state["recipe"] = recipe
        return real_build(recipe, **kwargs)

    def take(key, default=None):
        value = state.get(key, default)
        return value.pop(0) if isinstance(value, list) else value

    def run_command(work_root, _work_dir):
        returncode = take("returncode")
        route = take("route", returncode == 0)
        output = "routed"
        if "output" in state:
            build = Path(state["recipe"].resolved.build_root).resolve()
            output = state["output"].format(
                build=build.as_posix(), rel=Path(os.path.relpath(build, work_root)).as_posix()
            )
        return [
            sys.executable,
            str(script),
            str(work_root),
            str(returncode),
            str(int(route)),
            output,
            take("stderr", ""),
            state.get("broken_report", ""),
        ]

    monkeypatch.setattr(fpga_edam, "build_fpga_edam", build)
    monkeypatch.setattr(fpga_edam, "fpga_run_command", run_command)
    monkeypatch.setattr(fpga_edam, "parse_fpga_reports", lambda _text: dict(take("parsed")))
    return state
