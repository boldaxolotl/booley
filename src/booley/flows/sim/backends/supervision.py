"""One supervised simulator process shared by the Verilator, Icarus and Cocotb adapters."""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from booley.bwave.waveform_store import StreamingConversion
from booley.flows.sim.backends.shared import (
    ChildCpuSnapshot,
    RunLogProgress,
    RunTermination,
    append_child_cpu_marker,
    child_cpu_snapshot,
    format_idle_note,
)
from booley.flows.sim.run_guard import (
    DiskBudgetGuard,
    SimTimeStallGuard,
    child_death_kwargs,
    classify_readmemh_termination,
    parse_readmemh_fatal,
    snapshot_dir_baseline,
    supervise_child,
)
from booley.flows.sim.trace_session import TraceSession
from booley.runtime.platform_paths import kill_process_tree, popen_new_group_kwargs


@dataclass(frozen=True)
class SimulatorLaunch:
    """One simulator process and the guard inputs its adapter can supply."""

    label: str
    command: list[str]
    run_cwd: Path
    env: dict[str, str]
    timeout_s: int
    work_dir: Path | None = None
    max_rundir_bytes: int = 0
    runtime_inputs: tuple[str, ...] = ()
    sim_time_grace_s: float = 0.0  # > 0 arms the frozen-clock guard (Cocotb prints sim time)
    trace: TraceSession | None = None  # kill incidents; with ``conversion`` also a stall monitor
    conversion: StreamingConversion | None = None
    on_line: Callable[[str], None] | None = None


class _Supervisor:
    """Guards, deadline and abort evidence for one running simulator."""

    def __init__(
        self,
        launch: SimulatorLaunch,
        proc: subprocess.Popen[str],
        disk_baseline: tuple[int, float],
    ) -> None:
        self.launch = launch
        self.proc = proc
        self.lines: deque[str] = deque(maxlen=5_000)
        self.streamed = 0  # simulator lines only, never the appended CPU marker
        self.timed_out = False
        self.fatal: RunTermination | None = None
        self.incident: Path | None = None
        started = time.monotonic()
        self.deadline = started + launch.timeout_s
        self.progress = RunLogProgress(launch.work_dir, started)
        self.guard = DiskBudgetGuard(
            launch.run_cwd, launch.max_rundir_bytes, proc, baseline=disk_baseline
        )
        self.stall = SimTimeStallGuard(proc, launch.sim_time_grace_s)
        # A silent simulator never reaches the per-line deadline check (fpu F-21).
        self.timer = threading.Timer(launch.timeout_s, self._kill_at_deadline)
        self.timer.daemon = True
        self.timer.start()
        self.guard.start()
        self.stall.start()

    def _kill_at_deadline(self) -> None:
        if self.proc.poll() is None:
            self.timed_out = True
            kill_process_tree(self.proc)

    def _timeout_detail(self) -> str:
        idle = format_idle_note(self.progress.idle_s, self.streamed)
        return f"{self.launch.label} simulation timed out ({self.launch.timeout_s}s){idle}"

    def _kill(self, reason: str) -> bool:
        """Kill the tree for *reason*; False when the child exited on its own first."""
        trace = self.launch.trace
        incident = None
        if self.proc.poll() is None and trace is not None:
            incident = trace.write_incident(
                reason, sim_proc=self.proc, bwave_proc=self.launch.conversion
            )
        if self.proc.poll() is not None:  # also after the incident write
            return False
        kill_process_tree(self.proc)
        self.incident = incident
        return True

    def stop_after(self, line: str) -> bool:
        """Kill on a fatal ``$readmemh`` (True ends the read) or a passed deadline (reads on)."""
        launch = self.launch
        missing = parse_readmemh_fatal(line)
        if missing and self.proc.poll() is None:
            fatal = classify_readmemh_termination(missing, launch.run_cwd, launch.runtime_inputs)
            if self._kill(fatal.detail):
                self.fatal = fatal
                return True
        elif time.monotonic() > self.deadline and self.proc.poll() is None:
            # Another abort already owns the dying child: no second kill or incident.
            if self._abort() is None and self._kill(self._timeout_detail()):
                self.timed_out = True
        return False

    def drain(self) -> None:
        """Stream output until EOF or a fatal kill, then reap the child."""
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            print(line, end="", flush=True)
            self.lines.append(line)
            self.streamed += 1
            self.progress.observe(self.lines)
            if self.launch.on_line is not None:
                self.launch.on_line(line)
            self.stall.observe(line)
            if self.stop_after(line):
                break
        # Still bounded: the armed timer kills a child that outlives its stdout.
        self.proc.wait()

    def close(self, cpu_started: ChildCpuSnapshot | None) -> None:
        """Release streams, timer and guards; record the CPU marker last."""
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.timer.cancel()
        self.guard.stop()
        self.stall.stop()
        self.progress.final_flush(self.lines)
        append_child_cpu_marker(self.lines, cpu_started)

    def _abort(self) -> RunTermination | None:
        """The one abort cause, most specific first."""
        trace = self.launch.trace
        if self.fatal is not None:
            return self.fatal
        if self.guard.tripped:
            return RunTermination("disk_budget", self.guard.message, "infrastructure")
        if self.stall.tripped:
            return RunTermination("sim_time_stall", self.stall.message, "infrastructure")
        if trace is not None and trace.stall_killed:
            return RunTermination("trace_stall", str(trace.stall_message), "infrastructure")
        if self.timed_out:
            return RunTermination("timeout", self._timeout_detail(), "timeout")
        return None

    def resolve(self) -> RunTermination:
        """Print and record why the run stopped; final disk accounting if it did not."""
        termination = self._abort()
        if termination is None:
            self.guard.finish()
            termination = self._abort()
        if termination is None:
            return RunTermination()
        message = termination.detail
        if termination.kind != "disk_budget":
            message = f"ERROR: {message}"
        if self.incident is not None:
            message += f"\nTRACE_INCIDENT: {self.incident}"
        print(message)
        self.lines.append(message + "\n")
        return termination


def run_supervised(
    launch: SimulatorLaunch,
) -> tuple[deque[str], subprocess.Popen[str], RunTermination]:
    """Spawn *launch*, stream and guard it, and return why it stopped."""
    from booley.harness.colors import dim
    from booley.presentation.heartbeat import render_heartbeat
    from booley.runtime.heartbeat import Heartbeat

    heartbeat = Heartbeat(
        f"{launch.label} sim", render=partial(render_heartbeat, style=dim), interval=60
    )
    heartbeat.start()
    try:
        return _run(launch)
    finally:
        heartbeat.stop()


def _run(launch: SimulatorLaunch) -> tuple[deque[str], subprocess.Popen[str], RunTermination]:
    cpu_started = child_cpu_snapshot()
    # BEFORE the spawn: the baseline walk takes seconds on multi-GB trees, and
    # anything dumped during it would land in the baseline free of charge (F-23).
    disk_baseline = snapshot_dir_baseline(launch.run_cwd, launch.max_rundir_bytes)
    proc = subprocess.Popen(
        launch.command,
        cwd=str(launch.run_cwd),
        env=launch.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_new_group_kwargs(),
        **child_death_kwargs(),
    )
    supervise_child(proc)
    if launch.trace is not None and launch.conversion is not None:
        launch.trace.start_monitor(launch.conversion, proc)
    supervisor = _Supervisor(launch, proc, disk_baseline)
    try:
        supervisor.drain()
    finally:
        supervisor.close(cpu_started)
    return supervisor.lines, proc, supervisor.resolve()
