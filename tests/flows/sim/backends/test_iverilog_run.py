"""Unit tests for the edalize-era Icarus run-half (booley.flows.sim.backends.icarus).

These cover the pure run-half logic that does not need a real ``vvp``: image
discovery from the ``.scr`` sibling, the vvp command shape (``-M`` + absolute
image, no ``-fst`` → VCD), arg round-trip, and the missing-image early return.
The real vvp run + VCD→bwave postprocess is exercised end-to-end in the Sandbox
(it needs ``fusesoc``/``iverilog`` and per-project Icarus ``.core`` Targets).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from booley.flows.sim.adapter_transport import (
    AdapterTransportIdentity,
    read_adapter_result,
)
from booley.flows.sim.backends import icarus as ir
from booley.flows.sim.backends import shared
from booley.flows.sim.backends.shared import find_icarus_image
from booley.flows.sim.trace_session import TraceArtifact, TraceInspection


def test_find_image_strips_scr_extension(tmp_path: Path):
    """Edalize writes <name>.scr next to the vvp image named <name> (no ext)."""
    (tmp_path / "sim_cfg.scr").write_text("")
    assert find_icarus_image(tmp_path) == "sim_cfg"


def test_find_image_none_when_no_scr(tmp_path: Path):
    assert find_icarus_image(tmp_path) is None


def test_build_vvp_cmd_uses_M_and_absolute_image_no_fst(tmp_path: Path):  # noqa: N802 — `M` names the -M flag under test
    cmd = ir._build_vvp_cmd("vvp", tmp_path, "sim_cfg", ["test_id=1", "+already"])
    assert cmd[0] == "vvp"
    assert "-n" in cmd
    # VPI modules resolve from any cwd: -M<abs build dir> + absolute image path.
    assert f"-M{tmp_path}" in cmd
    assert str(tmp_path / "sim_cfg") in cmd
    # bare plusargs gain a leading +, already-prefixed ones are left alone
    assert "+test_id=1" in cmd
    assert "+already" in cmd
    # -fst is omitted so the dump is a VCD the postprocess understands
    assert "-fst" not in cmd


def test_build_vvp_cmd_no_plusargs(tmp_path: Path):
    cmd = ir._build_vvp_cmd("vvp", tmp_path, "sim_cfg", None)
    assert cmd == ["vvp", "-n", f"-M{tmp_path}", str(tmp_path / "sim_cfg")]


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_parse_args_rejects_non_positive_timeout(bad: str):
    # A non-positive timeout would fire threading.Timer immediately; reject at CLI.
    with pytest.raises(SystemExit):
        ir._parse_args(["--build-dir", "b", "--timeout", bad])


def test_run_icarus_image_missing_image_returns_error(tmp_path: Path):
    """A build dir with no .scr yields a clear error string (no crash, no vvp)."""
    out = ir.run_icarus_image(build_dir=tmp_path)
    assert "no vvp image" in out


def test_run_icarus_image_creates_missing_work_dir(tmp_path: Path):
    """The run-half owns its output dir: a caller-derived work dir that does not
    exist yet is created before any run (parity with the Verilator run-half)."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    work_dir = tmp_path / "sim" / "config_a"  # nested, does not exist
    # No image present → returns early after the mkdir, before any vvp run.
    ir.run_icarus_image(build_dir=build_dir, work_dir=work_dir)
    assert work_dir.is_dir()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="run-half execs a POSIX vvp image (#!/bin/sh stub); real sims run in-container",
)
def test_run_icarus_image_writes_run_log_end_to_end(tmp_path: Path, monkeypatch, capsys):
    """The full run path persists the raw output to <work_dir>/run.log."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "sim_cfg.scr").write_text("")  # image discovery sibling
    (build_dir / "sim_cfg").write_text("")  # the (fake) vvp image
    fake_vvp = tmp_path / "fake_vvp"
    fake_vvp.write_text("#!/bin/sh\necho 'TB chatter'\necho '[SIM_RESULT] PASSED'\n")
    fake_vvp.chmod(0o755)
    monkeypatch.setattr(ir, "_find_vvp", lambda: str(fake_vvp))

    work_dir = tmp_path / "work"
    ir.run_icarus_image(build_dir=build_dir, work_dir=work_dir, timeout=30)

    log = (work_dir / "run.log").read_text(encoding="utf-8")
    assert "TB chatter" in log
    assert "[SIM_RESULT] PASSED" in log
    assert (work_dir / "result.json").exists()  # run.log sits beside result.json


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="run-half execs a POSIX vvp stub; real sims run in-container",
)
def test_bad_build_fixture_keeps_runtime_asset_and_grades_design_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Build isolation and runtime assets are independent self-test boundaries."""
    runtime = tmp_path / "runtime-assets"
    runtime.mkdir()
    (runtime / "firmware.hex").write_text("runtime input\n")
    runner = tmp_path / "fake_vvp"
    runner.write_text(
        "#!/bin/sh\n"
        "if [ ! -f firmware.hex ]; then\n"
        "  echo 'Cannot open firmware.hex for reading.'\n"
        "  exit 2\n"
        "fi\n"
        'for image in "$@"; do :; done\n'
        'case "$image" in\n'
        "  */bad) echo '[SIM_RESULT] FAILED' ;;\n"
        "  *) echo '[SIM_RESULT] PASSED' ;;\n"
        "esac\n"
    )
    runner.chmod(0o755)
    monkeypatch.setattr(ir, "_find_vvp", lambda: str(runner))

    results = {}
    for kind in ("good", "bad"):
        build = tmp_path / f"{kind}-build"
        build.mkdir()
        (build / f"{kind}.scr").write_text("")
        (build / kind).write_text("")
        work = tmp_path / f"{kind}-result"
        output = ir.run_icarus_image(build_dir=build, run_cwd=runtime, work_dir=work)
        assert "missing $readmemh" not in output
        results[kind] = json.loads((work / "result.json").read_text(encoding="utf-8"))

    assert results["good"]["passed"] is True
    assert results["bad"] == {
        "passed": False,
        "sva_errors": 0,
        "returncode": 0,
        "first_error": "[SIM_RESULT] FAILED",
    }


# ---------------------------------------------------------------------------
# Per-run safety guards: missing $readmemh (SETUP-23) + disk budget (SETUP-25)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="run-half execs a POSIX vvp stub; real sims run in-container",
)
def test_disk_kill_is_published_as_aborted_infrastructure(tmp_path: Path, monkeypatch) -> None:
    """A guard-owned kill remains abort evidence even when the child exits zero."""
    result = _run_disk_kill_adapter(tmp_path, monkeypatch)

    assert result.failure_kind == "infrastructure"
    assert result.inconclusive is False
    assert result.termination == "disk_budget"
    assert result.simulator_returncode == 0
    assert "run directory" in result.detail


def _run_disk_kill_adapter(tmp_path: Path, monkeypatch):
    import functools

    from booley.flows.sim.backends import supervision

    monkeypatch.setattr(
        supervision,
        "DiskBudgetGuard",
        functools.partial(supervision.DiskBudgetGuard, interval=0.02),
    )
    build = tmp_path / "build"
    run = tmp_path / "run"
    work = tmp_path / "work"
    build.mkdir()
    run.mkdir()
    (build / "sim.scr").write_text("")
    (build / "sim").write_text("")
    fake_vvp = tmp_path / "fake-vvp"
    fake_vvp.write_text(
        "#!/bin/sh\n"
        "trap 'exit 0' TERM\n"
        "dd if=/dev/zero of=guard-growth.bin bs=4096 count=64 2>/dev/null\n"
        "while :; do sleep 1; done\n"
    )
    fake_vvp.chmod(0o755)
    monkeypatch.setattr(ir, "_find_vvp", lambda: str(fake_vvp))
    identity = AdapterTransportIdentity(
        adapter="icarus",
        attempt_token="attempt-1",
        target_identity="acme:lib:guard:1#sim_guard",
        selected_tests=("guard",),
        result_path=tmp_path / "adapter-result.json",
    )

    ir.run_icarus_image(
        build_dir=build,
        run_cwd=run,
        work_dir=work,
        timeout=30,
        max_rundir_bytes=1024,
        transport=identity,
    )

    return read_adapter_result(identity)


class _FakeTraceSession:
    """Trace seam stub: Booley-owned discovery sees only the store a VCD
    conversion publishes, so the outcome depends on the declared-file filter."""

    owned_outputs: frozenset[Path] = frozenset()

    def __init__(self, work_dir: Path, published: Path | None = None):
        self._work_dir = work_dir
        # Where a successful VCD conversion publishes its waveform store.
        self._published = published

    def reset_for_run(self, _paths):
        return None

    def postprocess(self, path):
        if self._published is not None and path.exists():
            self._published.write_text("converted", encoding="utf-8")
            path.unlink()  # a successful conversion consumes the source VCD

    def find(self):
        return self._published if self._published and self._published.exists() else None

    def inspect(self, path):
        if path.suffix == ".vcd":  # a raw VCD is never queryable
            return TraceInspection(None, "retained trace is raw .vcd")
        size = path.stat().st_size if path.exists() else 0
        return TraceInspection(TraceArtifact(path, size, "tb", 1, 1))

    def write_incident(self, reason, *, sim_proc, bwave_proc=None):
        incident = self._work_dir / "trace_incident.txt"
        incident.write_text(reason, encoding="utf-8")
        return incident


def _run_traced_icarus(
    tmp_path: Path,
    monkeypatch,
    *,
    trace_files: list[str],
    stale_dirs: tuple[str, ...],
    vvp_body: str,
    stale_name: str = "tb_dump.fst",
    converts_to_store: bool = False,
    timeout: int = 30,
) -> str:
    """Run the full traced Icarus path with stale declared files pre-created."""
    build_dir = tmp_path / "build"
    run_cwd = tmp_path / "run"
    work_dir = tmp_path / "work"
    for directory in (build_dir, run_cwd, work_dir):
        directory.mkdir(exist_ok=True)
    (build_dir / "sim_cfg.scr").write_text("")
    (build_dir / "sim_cfg").write_text("")
    for name in stale_dirs:
        (tmp_path / name / stale_name).write_text("stale", encoding="utf-8")
    fake_vvp = tmp_path / "fake_vvp"
    fake_vvp.write_text(f"#!/bin/sh\n{vvp_body}\necho '[SIM_RESULT] PASSED'\n")
    fake_vvp.chmod(0o755)
    monkeypatch.setattr(ir, "_find_vvp", lambda: str(fake_vvp))
    session = _FakeTraceSession(
        work_dir, published=work_dir / "trace.fst" if converts_to_store else None
    )
    monkeypatch.setattr(ir, "TraceSession", lambda *_args, **_kwargs: session)
    return ir.run_icarus_image(
        build_dir=build_dir,
        run_cwd=run_cwd,
        work_dir=work_dir,
        vcd=True,
        timeout=timeout,
        trace_files=trace_files,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
@pytest.mark.parametrize(
    ("stale_dirs", "vvp_body"),
    [
        pytest.param(("run",), "true", id="run_cwd"),
        pytest.param(("work",), "true", id="work_dir"),
        # The run removes the earlier root's copy; the later root's stale copy
        # must not take its place.
        pytest.param(("run", "work"), "rm tb_dump.fst", id="shadowed"),
    ],
)
def test_icarus_never_adopts_unchanged_declared_trace(
    tmp_path: Path, monkeypatch, stale_dirs, vvp_body
):
    """A declared trace left by an earlier run must not earn TRACE_OK."""
    trace_files = ["tb_dump.fst"]
    output = _run_traced_icarus(
        tmp_path,
        monkeypatch,
        trace_files=trace_files,
        stale_dirs=stale_dirs,
        vvp_body=vvp_body,
    )

    assert "TRACE_INCIDENT" in output
    assert f"[flows.sim].trace_files {trace_files}" in output
    assert "TRACE_OK" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
def test_icarus_never_converts_unchanged_declared_vcd(tmp_path: Path, monkeypatch):
    """Converting a stale declared VCD would publish a fresh-looking store."""
    output = _run_traced_icarus(
        tmp_path,
        monkeypatch,
        trace_files=["tb_dump.vcd"],
        stale_dirs=("run",),
        vvp_body="true",
        stale_name="tb_dump.vcd",
        converts_to_store=True,
    )

    assert "TRACE_INCIDENT" in output
    assert "TRACE_OK" not in output
    assert (tmp_path / "run" / "tb_dump.vcd").read_text(encoding="utf-8") == "stale"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
def test_icarus_adopts_declared_trace_rewritten_by_run(tmp_path: Path, monkeypatch):
    """A declared trace the run rewrites is this run's trace."""
    output = _run_traced_icarus(
        tmp_path,
        monkeypatch,
        trace_files=["tb_dump.fst"],
        stale_dirs=("run",),
        vvp_body="printf 'fresh longer content' > tb_dump.fst",
    )

    assert f"TRACE_OK: {(tmp_path / 'run').resolve() / 'tb_dump.fst'}" in output


def _icarus_run(tmp_path: Path, command: list[str]) -> ir._IcarusRun:
    return ir._IcarusRun(tmp_path, tmp_path, tmp_path, Path("sim"), command, None)


def test_execute_passes_guard_inputs_to_the_runner(tmp_path: Path, monkeypatch):
    from booley.flows.sim.backends.supervision import SimulatorLaunch

    launches: list[SimulatorLaunch] = []
    monkeypatch.setattr(ir, "run_supervised", launches.append)
    run = _icarus_run(tmp_path, ["vvp", "-n", "image"])

    ir._execute_icarus_run(run, 17, 4096, ("firmware.hex",))

    (launch,) = launches
    assert launch == SimulatorLaunch(
        "iverilog", run.command, tmp_path, os.environ.copy(), 17, tmp_path, 4096, ("firmware.hex",)
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
def test_printing_icarus_run_is_stopped_at_the_deadline(
    tmp_path: Path, monkeypatch, never_firing_timer
):
    """Only the per-line deadline check can end this run: the timer never fires."""
    chatty = "i=0; while [ $i -lt 800 ]; do echo tick $i; sleep 0.01; i=$((i+1)); done"

    start = time.monotonic()
    output = _run_traced_icarus(
        tmp_path, monkeypatch, trace_files=[], stale_dirs=(), vvp_body=chatty, timeout=1
    )

    assert time.monotonic() - start < 6
    assert "ERROR: iverilog simulation timed out (1s)" in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
def test_icarus_oversize_declared_vcd_never_falls_back(tmp_path: Path, monkeypatch):
    """A declared VCD over the conversion cap is an incident, never TRACE_FALLBACK."""
    monkeypatch.setattr(shared, "MAX_ADOPTED_VCD_BYTES", 16)

    output = _run_traced_icarus(
        tmp_path,
        monkeypatch,
        trace_files=["tb_dump.vcd"],
        stale_dirs=(),
        vvp_body="head -c 64 /dev/zero > tb_dump.vcd",
    )

    assert "TRACE_INCIDENT" in output
    assert "TRACE_FALLBACK" not in output
    assert "TRACE_OK" not in output
