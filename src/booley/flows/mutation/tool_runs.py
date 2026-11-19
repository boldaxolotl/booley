"""The one place the Mutation Flow starts a tool.

Each FuseSoC setup, build and simulation runs as
``subprocess.run(capture_output=True, text=True)`` would, measured by the
shared Job resource monitor and recorded in the Job resource history under the
Mutation Flow's name. A kill is never blamed on memory (ADR 0087): a run that
exits nonzero or is killed is a plain failure, and nothing is requeued.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence

from booley.flows import job_resources
from booley.flows.base import SubprocessResult
from booley.flows.job_resources import JobResourceMonitor, MeasuredPopen, ResourceLabel
from booley.flows.sim.build import SetupRunner


def _communicate(
    process: MeasuredPopen, monitor: JobResourceMonitor, timeout: float | None
) -> tuple[str, str]:
    """Start *monitor*, then wait as ``subprocess.run`` does: kill on any failure."""
    try:
        monitor.start(process.pid)
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        if sys.platform == "win32":
            # Windows delivers the partial output only to a second ``communicate``.
            exc.stdout, exc.stderr = process.communicate()
        else:
            process.wait()
        raise
    except BaseException:
        process.kill()  # also on Ctrl-C: a tool must not outlive the Flow
        raise
    return stdout, stderr


def run_tool(
    cmd: Sequence[str],
    *,
    label: ResourceLabel,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    timeout: float | None = None,
    timed_out: Callable[[str], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* captured, measure it, and record one history record when it settles.

    *timed_out* tells whether the tool's own output reports that it timed out
    (a simulator adapter stops the simulator at its own deadline, before the
    hard timeout). A command that cannot start records nothing and raises.
    """
    monitor = JobResourceMonitor()
    process: MeasuredPopen | None = None
    started = time.monotonic()
    hard_timeout = False
    stdout = stderr = ""
    settled = False
    try:
        with MeasuredPopen(
            list(cmd),
            cwd=cwd,
            env={**env, **monitor.temp_env()},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
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
            job_resources.record_command(
                "mutation",
                label,
                (),
                measurement,
                SubprocessResult(
                    returncode=process.returncode,
                    timed_out=hard_timeout or reported,
                    duration_s=time.monotonic() - started,
                ),
            )
    assert process is not None  # a spawn failure raised above
    return subprocess.CompletedProcess(list(cmd), process.returncode, stdout, stderr)


def setup_runner(target: str) -> SetupRunner:
    """A FuseSoC setup runner that records *target*'s ``sim_prepare`` run."""

    def run(
        cmd: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        **_ignored: object,
    ) -> subprocess.CompletedProcess[str]:
        return run_tool(
            cmd,
            label=ResourceLabel(target, "sim_prepare"),
            cwd=cwd,
            env=os.environ if env is None else env,
        )

    return run
