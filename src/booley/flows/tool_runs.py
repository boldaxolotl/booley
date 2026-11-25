"""The one place a Flow starts a tool outside ``FlowMechanics._execute_local``.

Each FuseSoC setup, build and simulation runs as
``subprocess.run(capture_output=True, text=True)`` would, but in its own process
group, stopped with its whole tree on a timeout or interrupt; it is measured by the
shared Job resource monitor and recorded in the Job resource history under the
calling Flow's name. A run the OOM killer took is named as such, with whether
the host or the Sandbox's own limit ran out (ADR 0087); ``CampaignClaims``
decides whether it runs again.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from booley.flows import job_resources
from booley.flows.base import SubprocessResult
from booley.flows.job_resources import JobResourceMonitor, MeasuredPopen, ResourceLabel
from booley.runtime.platform_paths import (
    communicate_sliced,
    drain_killed_tree,
    kill_process_tree,
    popen_new_group_kwargs,
)

#: Which out-of-memory kill took a process of a run's own tree: at the Sandbox's
#: own memory limit, or the host's while the Sandbox stayed below it.
OomKill = Literal["sandbox_limit", "host"]


class ToolRun(subprocess.CompletedProcess[str]):
    """A settled tool run, and the out-of-memory kill that ended it (``None``: none)."""

    def __init__(
        self, args: list[str], returncode: int, stdout: str, stderr: str, oom_kill: OomKill | None
    ) -> None:
        super().__init__(args, returncode, stdout, stderr)
        self.oom_kill: OomKill | None = oom_kill


def oom_kill_of(process: subprocess.CompletedProcess[str] | None) -> OomKill | None:
    """The out-of-memory kill that ended *process*; ``None`` for none or a run that never started."""
    return process.oom_kill if isinstance(process, ToolRun) else None


def _communicate(
    process: MeasuredPopen, monitor: JobResourceMonitor, timeout: float | None
) -> tuple[str, str]:
    """Start *monitor*, then wait; on a timeout or any failure stop the tool's whole tree.

    The tool runs in its own process group, so Ctrl-C reaches Booley, not the
    tool; the wait is sliced so the interrupt arrives promptly on Windows too.
    """
    limit = math.inf if timeout is None else timeout
    try:
        monitor.start(process.pid)
        stdout, stderr = communicate_sliced(process, limit)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = _stop(process)
        if stdout is None:  # abandoned or undecodable: keep the partial output
            stdout, stderr = exc.output, exc.stderr
        raise subprocess.TimeoutExpired(process.args, limit, stdout, stderr) from None
    except BaseException:
        _stop(process)  # also on Ctrl-C: a tool must not outlive the Flow
        raise
    assert stdout is not None and stderr is not None  # both are pipes
    return stdout, stderr


def _stop(process: MeasuredPopen) -> tuple[str | None, str | None]:
    """Kill *process*'s tree and drain its output; ``None`` when that output is lost."""
    kill_process_tree(process)
    try:
        return drain_killed_tree(process)
    except UnicodeDecodeError:  # killed in the middle of a character
        return None, None


def run_tool(
    cmd: Sequence[str],
    *,
    flow: str,
    label: ResourceLabel,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    timeout: float | None = None,
    timed_out: Callable[[str], bool] | None = None,
) -> ToolRun:
    """Run *cmd* captured, measure it, and record one history record when it settles.

    *timed_out* tells whether the tool's own output reports that it timed out
    (a simulator adapter stops the simulator at its own deadline, before the
    hard timeout); a timed-out run is never an out-of-memory kill. A command
    that cannot start records nothing and raises.
    """
    monitor = JobResourceMonitor()
    process: MeasuredPopen | None = None
    started = time.monotonic()
    hard_timeout = settled = False
    stdout = stderr = ""
    result: SubprocessResult | None = None
    try:
        process = MeasuredPopen(
            list(cmd),
            cwd=cwd,
            env={**env, **monitor.temp_env()},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_new_group_kwargs(),
        )
        try:
            stdout, stderr = _communicate(process, monitor, timeout)
            settled = True
        except subprocess.TimeoutExpired:
            hard_timeout = settled = True
            raise
    finally:
        measurement = monitor.finish(None if process is None else process.reaped_peak_bytes)
        if monitor.tmp_dir is not None:
            shutil.rmtree(monitor.tmp_dir, ignore_errors=True)
        if settled:
            assert process is not None
            reported = timed_out is not None and timed_out(stdout + stderr)
            result = SubprocessResult(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=hard_timeout or reported,
                duration_s=time.monotonic() - started,
            )
            _blame_oom_kill(measurement, result)
            job_resources.record_command(flow, label, (), measurement, result)
    assert result is not None  # a spawn failure raised above; a hard timeout re-raised
    return ToolRun(list(cmd), result.returncode, stdout, stderr, _oom_kill(result))


def _blame_oom_kill(measurement: job_resources.JobMeasurement, result: SubprocessResult) -> None:
    """Mark *result* when the OOM killer took a process of its own tree, and which kind it was.

    Booley cancels a campaign's tool only by raising, so a settled run was never
    Booley's own kill.
    """
    result.oom_killed = job_resources.own_oom_kill(measurement, result, cancelled=False)
    result.host_oom_kill = result.oom_killed and job_resources.host_oom_kill(measurement)


def _oom_kill(result: SubprocessResult) -> OomKill | None:
    """Which out-of-memory kill *result* was marked with; ``None`` for none."""
    if not result.oom_killed:
        return None
    return "host" if result.host_oom_kill else "sandbox_limit"


def setup_runner(
    flow: str, target: str, phase: str
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A FuseSoC setup runner that records *target*'s *phase* run under *flow*."""

    def run(
        cmd: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        **_ignored: object,
    ) -> subprocess.CompletedProcess[str]:
        return run_tool(
            cmd,
            flow=flow,
            label=ResourceLabel(target, phase),
            cwd=cwd,
            env=os.environ if env is None else env,
        )

    return run
