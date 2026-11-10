"""The one supervised simulator runner shared by the native adapters."""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.run_log import begin_run_log, run_log_is_current
from booley.flows.sim.backends import shared
from booley.flows.sim.backends import supervision as sup
from booley.flows.sim.backends.supervision import SimulatorLaunch, run_supervised
from booley.flows.sim.run_guard import SIM_TIME_STALL_MARKER
from booley.flows.sim.trace_session import TraceSession
from booley.runtime.platform_paths import kill_process_tree

FATAL_LINE = "%Warning: tb.sv:9: $readmemh: Cannot open memory.hex"
SLEEP = "import time; time.sleep(60)"
FATAL_THEN_SLEEP = f"import time; print({FATAL_LINE!r}, flush=True); time.sleep(60)"
# Generous wall-clock bound for runs the runner must end in about a second.
KILLED_WITHIN_S = 20


def _launch(tmp_path: Path, script: str, **overrides: Any) -> SimulatorLaunch:
    """A launch of a tiny Python child in an empty run directory."""
    run_cwd = tmp_path / "run"
    run_cwd.mkdir(exist_ok=True)
    fields: dict[str, Any] = {
        "label": "fake",
        "command": [sys.executable, "-u", "-c", script],
        "run_cwd": run_cwd,
        "env": os.environ.copy(),
        "timeout_s": 30,
    }
    return SimulatorLaunch(**{**fields, **overrides})


def _guard(message: str | None = None, *, finish: Any = None) -> SimpleNamespace:
    """Disk or stall guard whose verdict is fixed: tripped exactly when a message is given."""
    return SimpleNamespace(
        tripped=message is not None,
        message=message or "",
        start=lambda: None,
        stop=lambda: None,
        observe=lambda _line: None,
        finish=finish or (lambda: None),
    )


def _guards(monkeypatch, *, disk: str | None = None, stall: str | None = None, finish: Any = None):
    monkeypatch.setattr(sup, "DiskBudgetGuard", lambda *_a, **_k: _guard(disk, finish=finish))
    monkeypatch.setattr(sup, "SimTimeStallGuard", lambda *_a, **_k: _guard(stall))


class _FakeTrace:
    """Trace session seam: records monitor and incident calls."""

    def __init__(self, tmp_path: Path, stall_message: str | None = None):
        self.stall_killed, self.stall_message = stall_message is not None, stall_message
        self.incident_path = tmp_path / "trace_incident.txt"
        self.monitored: list[tuple[object, object]] = []
        self.incidents: list[tuple[str, object, object]] = []

    def stop_monitor(self) -> None:
        pass

    def start_monitor(self, conversion: object, proc: object) -> None:
        self.monitored.append((conversion, proc))

    def write_incident(self, reason: str, *, sim_proc: Any = None, bwave_proc: Any = None):
        self.incidents.append((reason, sim_proc, bwave_proc))
        return self.incident_path


# --- precedence: $readmemh fatal > disk budget > sim-time stall > trace stall > timeout ---


def test_readmemh_fatal_beats_disk_budget(tmp_path: Path, monkeypatch):
    _guards(monkeypatch, disk="DISK")

    lines, _, termination = run_supervised(_launch(tmp_path, FATAL_THEN_SLEEP))

    assert termination.kind == "fatal_init"
    assert "missing $readmemh" in "".join(lines)


def test_disk_budget_beats_sim_time_stall(tmp_path: Path, monkeypatch):
    _guards(monkeypatch, disk="DISK", stall="STALL")

    _, _, termination = run_supervised(_launch(tmp_path, "print('x')"))

    assert (termination.kind, termination.detail) == ("disk_budget", "DISK")


def test_sim_time_stall_beats_trace_stall(tmp_path: Path, monkeypatch):
    _guards(monkeypatch, stall=SIM_TIME_STALL_MARKER)
    trace = _FakeTrace(tmp_path, stall_message="TRACE")

    _, _, termination = run_supervised(_launch(tmp_path, "print('x')", trace=trace))

    assert termination.kind == "sim_time_stall"


def test_trace_stall_beats_timeout(tmp_path: Path, instant_timer):
    trace = _FakeTrace(tmp_path, stall_message="trace pipeline stalled")

    _, _, termination = run_supervised(_launch(tmp_path, SLEEP, trace=trace))

    assert (termination.kind, termination.failure_kind) == ("trace_stall", "infrastructure")


def test_deadline_line_leaves_a_dying_child_to_the_abort_that_started_it(
    tmp_path: Path, monkeypatch, never_firing_timer
):
    """A line past the deadline must not re-kill or write a timeout incident after a guard kill."""
    _guards(monkeypatch, disk="DISK")
    trace = _FakeTrace(tmp_path)
    script = "import time; print('x', flush=True); time.sleep(1)"

    _, _, termination = run_supervised(_launch(tmp_path, script, timeout_s=0, trace=trace))

    assert termination.kind == "disk_budget"
    assert trace.incidents == []


def test_silent_simulator_is_killed_by_the_timer(tmp_path: Path):
    """F-13/F-21: a sim that prints nothing is still killed at its budget."""
    start = time.monotonic()
    lines, _, termination = run_supervised(_launch(tmp_path, SLEEP, timeout_s=1))

    assert time.monotonic() - start < KILLED_WITHIN_S
    assert (termination.kind, termination.failure_kind) == ("timeout", "timeout")
    assert "printed NO output at all" in "".join(lines)


def test_lines_buffered_behind_the_deadline_kill_are_still_read(
    tmp_path: Path, never_firing_timer
):
    script = "import sys, time; sys.stdout.write('a\\nb\\n'); sys.stdout.flush(); time.sleep(1)"

    lines, _, termination = run_supervised(_launch(tmp_path, script, timeout_s=0))

    assert termination.kind == "timeout"
    assert list(lines)[:2] == ["a\n", "b\n"]


def test_child_that_closes_stdout_is_killed_at_the_deadline(tmp_path: Path):
    """EOF on stdout must not end supervision while the child is still alive."""
    script = (
        "import os, time; null = os.open(os.devnull, os.O_WRONLY); "
        "os.dup2(null, 1); os.dup2(null, 2); time.sleep(60)"
    )
    start = time.monotonic()
    _, _, termination = run_supervised(_launch(tmp_path, script, timeout_s=1))

    assert time.monotonic() - start < KILLED_WITHIN_S
    assert termination.kind == "timeout"


def test_disk_baseline_is_taken_before_the_spawn(tmp_path: Path, monkeypatch):
    """A baseline taken after the Popen would absorb what the sim dumps meanwhile."""
    order: list[str] = []
    real_snapshot, real_popen = sup.snapshot_dir_baseline, subprocess.Popen

    def snapshot(*args: Any):
        order.append("baseline")
        return real_snapshot(*args)

    def popen(*args: Any, **kwargs: Any):
        order.append("spawn")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(sup, "snapshot_dir_baseline", snapshot)
    monkeypatch.setattr(subprocess, "Popen", popen)

    run_supervised(_launch(tmp_path, "pass", max_rundir_bytes=1 << 20))

    assert order == ["baseline", "spawn"]


@pytest.mark.skipif(sys.platform == "win32", reason="the pid probe below is POSIX-only")
def test_kill_reaches_the_whole_process_group(tmp_path: Path):
    """A grandchild the simulator spawned dies with it (F-13)."""
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    lines, _, _ = run_supervised(_launch(tmp_path, script, timeout_s=1))

    grandchild = int(lines[0])
    deadline = time.monotonic() + KILLED_WITHIN_S
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"grandchild {grandchild} survived the group kill")


def test_readmemh_fatal_kills_the_child_and_names_the_trace_incident(tmp_path: Path):
    """SETUP-23: the warning ends the run fast, with a trace incident when tracing."""
    trace, conversion = _FakeTrace(tmp_path), object()

    start = time.monotonic()
    lines, proc, termination = run_supervised(
        _launch(tmp_path, FATAL_THEN_SLEEP, trace=trace, conversion=conversion)
    )

    assert time.monotonic() - start < KILLED_WITHIN_S
    assert termination.kind == "fatal_init"
    assert trace.monitored == [(conversion, proc)]
    assert trace.incidents == [(termination.detail, proc, conversion)]
    assert f"TRACE_INCIDENT: {trace.incident_path}" in "".join(lines)


def test_kill_does_not_claim_a_process_that_exits_during_incident_io(tmp_path: Path, monkeypatch):
    killed: list[object] = []
    monkeypatch.setattr(sup, "kill_process_tree", killed.append)

    class ExitingTrace(_FakeTrace):
        def write_incident(self, reason: str, *, sim_proc: Any = None, bwave_proc: Any = None):
            sim_proc.kill()
            sim_proc.wait()
            return super().write_incident(reason, sim_proc=sim_proc, bwave_proc=bwave_proc)

    _, _, termination = run_supervised(
        _launch(tmp_path, FATAL_THEN_SLEEP, trace=ExitingTrace(tmp_path))
    )

    assert termination.kind == "completed"
    assert killed == []


def test_disk_runaway_kills_a_silent_simulator(tmp_path: Path, monkeypatch):
    """SETUP-25: the watchdog polls while the stdout loop is blocked."""
    monkeypatch.setattr(
        sup, "DiskBudgetGuard", functools.partial(sup.DiskBudgetGuard, interval=0.02)
    )
    script = "import time; open('big.bin', 'wb').write(b'0' * 200_000); time.sleep(60)"

    start = time.monotonic()
    lines, _, termination = run_supervised(_launch(tmp_path, script, max_rundir_bytes=1024))

    assert time.monotonic() - start < KILLED_WITHIN_S
    assert termination.kind == "disk_budget"
    assert "max_rundir_bytes" in "".join(lines)


@pytest.mark.parametrize("returncode", [0, 1])
def test_final_disk_budget_after_natural_exit(tmp_path: Path, returncode: int):
    script = f"import sys; open('output.bin', 'wb').write(b'x' * 9); sys.exit({returncode})"

    lines, proc, termination = run_supervised(_launch(tmp_path, script, max_rundir_bytes=8))

    assert (termination.kind, termination.failure_kind) == ("disk_budget", "infrastructure")
    assert proc.returncode == returncode
    assert "simulation aborted:" in "".join(lines)


@pytest.mark.parametrize("prior", ["fatal_init", "timeout", "sim_time_stall", "trace_stall"])
def test_prior_abort_skips_the_final_disk_scan(tmp_path: Path, monkeypatch, request, prior: str):
    def no_final_scan() -> None:
        pytest.fail("an earlier abort must skip the final disk scan")

    stall = SIM_TIME_STALL_MARKER if prior == "sim_time_stall" else None
    _guards(monkeypatch, stall=stall, finish=no_final_scan)
    if prior == "timeout":
        request.getfixturevalue("instant_timer")
    trace = _FakeTrace(tmp_path, stall_message="stalled" if prior == "trace_stall" else None)
    script = {"fatal_init": FATAL_THEN_SLEEP, "timeout": SLEEP}.get(prior, "print('PASS')")

    _, _, termination = run_supervised(_launch(tmp_path, script, trace=trace))

    assert termination.kind == prior


def test_run_log_is_refreshed_while_running(tmp_path: Path, monkeypatch):
    """F-18: run.log carries a live tail mid-run, not just a placeholder."""
    monkeypatch.setattr(shared, "RUN_LOG_PROGRESS_INTERVAL_S", 0.0)
    work = tmp_path / "work"
    work.mkdir()
    begin_run_log(work, flow="sim", target="sim_x", run="run-live")

    run_supervised(_launch(tmp_path, "print('cycle 1'); print('cycle 2')", work_dir=work))

    content = (work / "run.log").read_text(encoding="utf-8")
    assert "cycle 2" in content and "output line(s)" in content
    assert run_log_is_current(work, "run-live") is False  # the adapter's verdict has not landed


class _IdleConversion:
    """A bwave stream that never exits and never grows: only a stop can end the monitor."""

    pid = 0

    def poll(self) -> None:
        return None

    def progress_size(self) -> int:
        return 0

    def stderr_tail(self, _limit: int = 4000) -> str:
        return ""


class _FirstPollAlive:
    """Simulator handle whose first ``poll()`` says alive, so the monitor always enters its wait."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self._polled = False

    def poll(self) -> int | None:
        if not self._polled:
            self._polled = True
            return None
        return self.proc.poll()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.proc, name)


@pytest.mark.parametrize("ending", ["completed", "timeout", "on_line_raises"])
def test_no_stall_monitor_outlives_the_run(tmp_path: Path, monkeypatch, request, ending: str):
    """AC1: however the run ends, its stall-monitor thread is gone when run_supervised returns."""
    monitors: list[tuple[threading.Thread, subprocess.Popen[str]]] = []
    real_monitor = TraceSession._monitor_stalls

    def recording_monitor(self, bwave_proc, sim_proc, stall_timeout, _poll_interval, *rest):
        monitors.append((threading.current_thread(), sim_proc))
        # A minute-long poll: an unstopped monitor is certainly still asleep when we assert.
        real_monitor(self, bwave_proc, _FirstPollAlive(sim_proc), stall_timeout, 60.0, *rest)

    monkeypatch.setattr(TraceSession, "_monitor_stalls", recording_monitor)
    session = TraceSession(tmp_path / "work")
    launch = {"trace": session, "conversion": _IdleConversion()}
    on_line_calls: list[str] = []

    def raising_on_line(line: str) -> None:
        on_line_calls.append(line)
        raise RuntimeError("on_line boom")

    def assert_monitor_joined() -> None:
        # Checked before the simulator is reaped below: that delay would let a merely
        # woken (not joined) monitor finish and hide a missing join.
        assert len(monitors) == 1
        assert not monitors[0][0].is_alive()

    try:
        if ending == "completed":
            run_supervised(_launch(tmp_path, "print('x')", **launch))
            assert_monitor_joined()
        elif ending == "timeout":
            request.getfixturevalue("instant_timer")
            _, _, termination = run_supervised(_launch(tmp_path, SLEEP, **launch))
            assert termination.kind == "timeout"
            assert_monitor_joined()
        else:
            script = "import time; print('x', flush=True); time.sleep(60)"
            with pytest.raises(RuntimeError, match="on_line boom"):
                run_supervised(_launch(tmp_path, script, on_line=raising_on_line, **launch))
            assert on_line_calls
            assert_monitor_joined()
    finally:
        for _, sim_proc in monitors:
            # Reaping the simulator after an on_line failure belongs to #3183.
            if sim_proc.poll() is None:
                kill_process_tree(sim_proc)
            sim_proc.wait(timeout=KILLED_WITHIN_S)
