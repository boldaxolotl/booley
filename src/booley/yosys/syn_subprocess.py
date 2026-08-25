"""Subprocess execution helpers for the Yosys synthesis flow.

Thin wrappers around :mod:`subprocess` that print/capture EDA tool output, save
per-step logs, and — for long-running steps — attach the stall-detecting
:class:`booley.yosys.synthesis_watchdog.SynthesisWatchdog`.  A non-zero exit is
surfaced with a log tail and turned into a ``sys.exit`` (``run_cmd``) or a
``subprocess.CalledProcessError`` (``run_cmd_watched``).

Leaf module: it imports the shared project-context (``SYN_DIR``) and the
watchdog, but never imports back from ``syn_core``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from booley.runtime.heartbeat import Heartbeat
from booley.yosys.syn_config import SYN_DIR
from booley.yosys.synthesis_watchdog import SynthesisWatchdog


def _print_log_tail(log_path: Path, max_lines: int = 15) -> None:
    """Echo the end of a failed step's log to stdout.

    The asic_synthesize wrapper only sees this process's stdout/stderr — the
    log file lives inside the sandbox work dir and never reaches the report.
    Without the tail, a failure surfaces as a bare "failed with code 1" and
    the actual yosys/sv2v/sta diagnostic is invisible to the agent.
    """
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if not lines:
        return
    print(f"--- last {min(max_lines, len(lines))} line(s) of {log_path.name} ---")
    for line in lines[-max_lines:]:
        print(line)


def _check_rc_and_exit(returncode: int, desc: str, log_path: Path | None = None) -> None:
    """If returncode indicates failure, print error and sys.exit."""
    if returncode == 0:
        return
    print(f"\nERROR: {desc} failed with code {returncode}")
    if log_path:
        print(f"Log saved to: {log_path}")
        _print_log_tail(log_path)
    # sys.exit truncates to 8 bits on POSIX — clamp to [1, 255]
    rc = returncode
    if not (0 < rc < 256):
        print(f"  (original rc={rc}, clamped to 1)")
        rc = 1
    sys.exit(rc)


def run_cmd(
    cmd: list[str],
    desc: str,
    work_dir: Path | None = None,
    log_file: str | None = None,
    heartbeat_interval: int = 0,
) -> subprocess.CompletedProcess:
    """Run a command, print output, optionally save to log file.
    If heartbeat_interval > 0, prints elapsed time every N seconds."""
    cwd = work_dir if work_dir else SYN_DIR
    print(f"\n{'=' * 60}")
    print(f"[{desc}]")
    print(f"{'=' * 60}")
    print(f"CWD: {cwd}")
    print(f"CMD: {' '.join(str(c) for c in cmd)}\n")

    hb = None
    if heartbeat_interval > 0:
        hb = Heartbeat(desc, interval=heartbeat_interval)
        hb.start()

    try:
        if log_file:
            log_path = work_dir / log_file if work_dir else Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as f:
                result = subprocess.run(
                    cmd,
                    cwd=cwd,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            _check_rc_and_exit(result.returncode, desc, log_path)
        else:
            result = subprocess.run(cmd, cwd=cwd, check=False)
            _check_rc_and_exit(result.returncode, desc)
    finally:
        if hb:
            hb.stop()

    return result


class WatchedResult(NamedTuple):
    """Result from run_cmd_watched(), compatible with subprocess.CompletedProcess."""

    returncode: int
    stdout: str
    watchdog_result: object  # WatchdogResult or None


def _watched_env() -> dict[str, str]:
    """Build environment for watched command (fix Windows ABC temp paths)."""
    env = os.environ.copy()
    if sys.platform == "win32":
        import tempfile

        win_tmp = tempfile.gettempdir()
        env["TMPDIR"] = win_tmp
        env["TEMP"] = win_tmp
        env["TMP"] = win_tmp
    return env


def _run_with_watchdog(
    proc: subprocess.Popen,
    cmd: list[str],
    desc: str,
    work_dir: Path,
    log_fh,
    log_path: Path | None,
    heartbeat_interval: int,
    poll_interval: int,
    timings_filename: str,
) -> WatchedResult:
    """Attach watchdog to proc, wait, and return results."""
    try:
        watchdog = SynthesisWatchdog(
            proc,
            desc,
            work_dir,
            poll_interval=poll_interval,
            heartbeat_interval=heartbeat_interval,
            log_file=log_fh,
            timings_filename=timings_filename,
        )
        watchdog.start()

        # Wait for output reader (avoids Windows pipe deadlock)
        watchdog.join_reader()
        proc.wait()

        wd_result = watchdog.stop()
        stdout_text = watchdog.get_stdout()

        if proc.returncode != 0:
            print(f"\nERROR: {desc} failed with code {proc.returncode}")
            if log_path:
                print(f"Log saved to: {log_path}")
                _print_log_tail(log_path)
            raise subprocess.CalledProcessError(
                proc.returncode,
                cmd,
                output=stdout_text,
            )

        return WatchedResult(
            returncode=proc.returncode,
            stdout=stdout_text,
            watchdog_result=wd_result,
        )
    except BaseException:
        proc.kill()
        proc.wait()
        raise


def run_cmd_watched(
    cmd: list[str],
    desc: str,
    work_dir: Path | None = None,
    log_file: str | None = None,
    heartbeat_interval: int = 60,
    poll_interval: int = 10,
    timings_filename: str = "stage_timings.json",
) -> WatchedResult:
    """Run a command with the synthesis watchdog (streaming output + metrics).

    ``timings_filename`` must be unique per watched step sharing a work_dir
    (Yosys, then OpenROAD), or the later step overwrites the earlier
    step's stage-timings JSON.

    Returns WatchedResult with returncode, captured stdout, and watchdog metrics.
    """
    cwd = work_dir if work_dir else SYN_DIR
    print(f"\n{'=' * 60}")
    print(f"[{desc}]")
    print(f"{'=' * 60}")
    print(f"CWD: {cwd}")
    print(f"CMD: {' '.join(str(c) for c in cmd)}\n")

    log_path = work_dir / log_file if (work_dir and log_file) else None
    log_stack = contextlib.ExitStack()
    log_fh = log_stack.enter_context(log_path.open("w", encoding="utf-8")) if log_path else None

    try:
        env = _watched_env()
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        return _run_with_watchdog(
            proc,
            cmd,
            desc,
            work_dir or SYN_DIR,
            log_fh,
            log_path,
            heartbeat_interval,
            poll_interval,
            timings_filename,
        )
    finally:
        log_stack.close()
