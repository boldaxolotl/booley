"""Unit tests for the edalize-era Icarus run-half (booley.flows.sim.backends.icarus).

These cover the pure run-half logic that does not need a real ``vvp``: image
discovery from the ``.scr`` sibling, the vvp command shape (``-M`` + absolute
image, no ``-fst`` → VCD), the work-document mapping, and the missing-image early return.
The real vvp run + VCD→bwave postprocess is exercised end-to-end in the Sandbox
(it needs ``fusesoc``/``iverilog`` and per-project Icarus ``.core`` Targets).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.time_budget import hang_guard_s

from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import AdapterTransportIdentity, read_adapter_result
from booley.flows.sim.backends import icarus as ir
from booley.flows.sim.backends.shared import find_icarus_image
from booley.flows.sim.backends.supervision import SimulatorLaunch
from booley.flows.sim.trace_session import TraceArtifact, TraceInspection


@pytest.mark.parametrize(("files", "image"), [(["sim_cfg.scr"], "sim_cfg"), ([], None)])
def test_find_image_strips_scr_extension(tmp_path: Path, files, image):
    """Edalize writes <name>.scr next to the vvp image named <name> (no ext)."""
    for name in files:
        (tmp_path / name).write_text("")
    assert find_icarus_image(tmp_path) == image


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
@pytest.mark.parametrize(
    ("plusargs", "tail"),
    [
        (None, []),
        # bare plusargs gain a leading +, already-prefixed ones are left alone
        (["test_id=1", "+already"], ["+test_id=1", "+already"]),
    ],
)
def test_vvp_command_uses_M_and_absolute_image_no_fst(  # noqa: N802 — `M` names the -M flag
    tmp_path: Path, tool_stub, fake_launch, plusargs, tail
):
    vvp = tool_stub("vvp", "true")
    (tmp_path / "sim_cfg.scr").write_text("")
    ir.run_icarus_image(build_dir=tmp_path, plusargs=plusargs)
    # VPI modules resolve from any cwd: -M<abs build dir> + absolute image path; no -fst.
    expected = [str(vvp), "-n", f"-M{tmp_path}", str(tmp_path / "sim_cfg"), *tail]
    assert fake_launch.launches[0].command == expected


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
def test_traced_document_with_trace_args_starts(tmp_path: Path, monkeypatch, tool_stub):
    """Project ``trace_args`` ride in the document; Icarus ignores them and still runs."""
    from booley.flows.sim import run_guard
    from booley.flows.sim.adapter_contract import encode_simulation_work

    monkeypatch.setattr(run_guard, "install_parent_death_guard", lambda: None)
    build = tmp_path / "build"
    build.mkdir()
    (build / "sim_cfg.scr").write_bytes(b"")
    (build / "sim_cfg").write_bytes(b"")
    marker = tmp_path / "vvp-ran"
    tool_stub("vvp", f"echo \"$@\">'{marker}'\necho 'ALL TESTS PASSED.'")
    work = PreparedSimulationWork(
        adapter="icarus",
        build_dir=str(build),
        run_cwd="",
        timeout_s=hang_guard_s(),
        work_dir=str(tmp_path / "work"),
        plusargs=("test_id=2",),
        trace=True,
        trace_args=("--trace={file}",),
        pass_sentinels=("ALL TESTS PASSED.",),
    )

    assert ir.main([encode_simulation_work(work)]) == 0
    assert "+test_id=2" in marker.read_text(encoding="utf-8")


def test_document_forwards_the_booley_directory(tmp_path: Path, monkeypatch):
    """Without the Booley Directory the run silently stops streaming declared traces."""
    from booley.flows.sim import run_guard
    from booley.flows.sim.adapter_contract import encode_simulation_work

    seen: dict[str, object] = {}

    def record(**kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(run_guard, "install_parent_death_guard", lambda: None)
    monkeypatch.setattr(ir, "run_icarus_image", record)
    work = PreparedSimulationWork(
        adapter="icarus",
        build_dir=str(tmp_path),
        run_cwd="",
        timeout_s=hang_guard_s(),
        work_dir=str(tmp_path / "work"),
        trace=True,
        trace_files=("fpu.vcd",),
        booley_dir=str(tmp_path / ".booley"),
    )

    assert ir.main([encode_simulation_work(work)]) == 0
    assert seen["booley_dir"] == tmp_path / ".booley"


def test_run_icarus_image_missing_image_returns_error(tmp_path: Path):
    """No .scr yields a clear error (no crash, no vvp); the run-half still creates its work dir."""
    work_dir = tmp_path / "sim" / "config_a"  # nested, does not exist
    out = ir.run_icarus_image(build_dir=tmp_path, work_dir=work_dir).output
    assert "no vvp image" in out
    assert work_dir.is_dir()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="run-half execs a POSIX vvp image (#!/bin/sh stub); real sims run in-container",
)
def test_run_icarus_image_writes_run_log_end_to_end(tmp_path: Path, tool_stub):
    """The full run path persists the raw output to <work_dir>/run.log."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "sim_cfg.scr").write_text("")  # image discovery sibling
    (build_dir / "sim_cfg").write_text("")  # the (fake) vvp image
    tool_stub("vvp", "echo 'TB chatter'\necho '[SIM_RESULT] PASSED'")

    work_dir = tmp_path / "work"
    ir.run_icarus_image(build_dir=build_dir, work_dir=work_dir, timeout=hang_guard_s())

    log = (work_dir / "run.log").read_text(encoding="utf-8")
    assert "TB chatter" in log
    assert "[SIM_RESULT] PASSED" in log
    assert (work_dir / "result.json").exists()  # run.log sits beside result.json


@pytest.mark.skipif(
    sys.platform == "win32", reason="run-half execs a POSIX vvp stub; real sims run in-container"
)
def test_bad_build_fixture_keeps_runtime_asset_and_grades_design_failure(
    tmp_path: Path, tool_stub
) -> None:
    """Build isolation and runtime assets are independent self-test boundaries."""
    runtime = tmp_path / "runtime-assets"
    runtime.mkdir()
    (runtime / "firmware.hex").write_text("runtime input\n")
    tool_stub(
        "vvp",
        "if [ ! -f firmware.hex ]; then\n"
        "  echo 'Cannot open firmware.hex for reading.'\n"
        "  exit 2\n"
        "fi\n"
        'for image in "$@"; do :; done\n'
        'case "$image" in\n'
        "  */bad) echo '[SIM_RESULT] FAILED' ;;\n"
        "  *) echo '[SIM_RESULT] PASSED' ;;\n"
        "esac",
    )

    results = {}
    for kind in ("good", "bad"):
        build = tmp_path / f"{kind}-build"
        build.mkdir()
        (build / f"{kind}.scr").write_text("")
        (build / kind).write_text("")
        work = tmp_path / f"{kind}-result"
        output = ir.run_icarus_image(build_dir=build, run_cwd=runtime, work_dir=work).output
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
    sys.platform == "win32", reason="run-half execs a POSIX vvp stub; real sims run in-container"
)
def test_disk_kill_is_published_as_aborted_infrastructure(
    tmp_path: Path, monkeypatch, tool_stub
) -> None:
    """A guard-owned kill remains abort evidence even when the child exits zero."""
    result = _run_disk_kill_adapter(tmp_path, monkeypatch, tool_stub)

    assert result.failure_kind == "infrastructure"
    assert result.inconclusive is False
    assert result.termination == "disk_budget"
    assert result.simulator_returncode == 0
    assert "run directory" in result.detail


def _run_disk_kill_adapter(tmp_path: Path, monkeypatch, tool_stub):
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
    tool_stub(
        "vvp",
        "trap 'exit 0' TERM\n"
        "dd if=/dev/zero of=guard-growth.bin bs=4096 count=64 2>/dev/null\n"
        "while :; do sleep 1; done",
    )
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
        timeout=hang_guard_s(),
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

    def stop_monitor(self):
        return None

    def start_conversion_budget(self, _timeout_s):
        return None

    def report_failed_conversions(self, *, declared_trace_used):
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
    tool_stub,
    *,
    trace_files: list[str],
    stale_dirs: tuple[str, ...],
    vvp_body: str,
    stale_name: str = "tb_dump.fst",
    converts_to_store: bool = False,
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
    tool_stub("vvp", f"{vvp_body}\necho '[SIM_RESULT] PASSED'")
    session = _FakeTraceSession(
        work_dir, published=work_dir / "trace.fst" if converts_to_store else None
    )
    monkeypatch.setattr(ir, "TraceSession", lambda *_args, **_kwargs: session)
    return ir.run_icarus_image(
        build_dir=build_dir,
        run_cwd=run_cwd,
        work_dir=work_dir,
        vcd=True,
        timeout=hang_guard_s(),
        trace_files=trace_files,
    ).output


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
    tmp_path: Path, monkeypatch, tool_stub, stale_dirs, vvp_body
):
    """A declared trace left by an earlier run must not earn TRACE_OK."""
    trace_files = ["tb_dump.fst"]
    output = _run_traced_icarus(
        tmp_path,
        monkeypatch,
        tool_stub,
        trace_files=trace_files,
        stale_dirs=stale_dirs,
        vvp_body=vvp_body,
    )

    assert "TRACE_INCIDENT" in output
    assert f"[flows.sim].trace_files {trace_files}" in output
    assert "TRACE_OK" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
def test_icarus_never_converts_unchanged_declared_vcd(tmp_path: Path, monkeypatch, tool_stub):
    """Converting a stale declared VCD would publish a fresh-looking store."""
    output = _run_traced_icarus(
        tmp_path,
        monkeypatch,
        tool_stub,
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
def test_icarus_adopts_declared_trace_rewritten_by_run(tmp_path: Path, monkeypatch, tool_stub):
    """A declared trace the run rewrites is this run's trace."""
    output = _run_traced_icarus(
        tmp_path,
        monkeypatch,
        tool_stub,
        trace_files=["tb_dump.fst"],
        stale_dirs=("run",),
        vvp_body="printf 'fresh longer content' > tb_dump.fst",
    )

    assert f"TRACE_OK: {(tmp_path / 'run').resolve() / 'tb_dump.fst'}" in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX vvp stub")
def test_launch_passes_guard_inputs_to_the_runner(tmp_path: Path, tool_stub, fake_launch):
    vvp = tool_stub("vvp", "true")
    (tmp_path / "sim.scr").write_text("")

    ir.run_icarus_image(
        build_dir=tmp_path, timeout=17, max_rundir_bytes=4096, runtime_inputs=("firmware.hex",)
    )

    (launch,) = fake_launch.launches
    command = [str(vvp), "-n", f"-M{tmp_path}", str(tmp_path / "sim")]
    assert launch == SimulatorLaunch(
        "iverilog", command, tmp_path, os.environ.copy(), 17, tmp_path, 4096, ("firmware.hex",)
    )
