"""Shared time budget for subprocesses that test helpers launch.

CI runs every test under ``--timeout=60`` (``.github/workflows/test.yml``) and
the Windows lanes add ``--timeout-headroom=0.8`` (48 s of useful work).  A
helper that waits on a child process must therefore give up well inside that
budget, so a hung child fails one test with a message naming the command
instead of freezing a pytest-xdist worker until the whole job is killed.

``run_bounded_child`` is the one way helpers start such a child: plain
``subprocess.run(timeout=...)`` kills only the direct child and, on Windows,
then waits without limit for pipes a surviving descendant holds (issue #3115).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping

import pytest

from booley.runtime.platform_paths import run_captured_bounded

#: Seconds a test helper may wait for a script or external tool (git, bash, ...).
#: Half of the 60 s per-test budget and under the 48 s Windows headroom limit.
SUBPROCESS_TIMEOUT_S = 30


def _captured(stream: str | None) -> str:
    """Render one captured stream of a timed-out child for a failure message."""
    if stream is None:
        return "(not collected: a descendant still held the pipe after the kill)"
    return stream or "(none captured)"


def run_bounded_child(
    argv: list[str],
    *,
    timeout: float = SUBPROCESS_TIMEOUT_S,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* capturing text output, killing its whole process tree after *timeout*.

    A timeout fails the test, naming the command, the limit, the elapsed wall
    time (kill and drain included) and the output the child wrote before the
    kill, so a child that hung silently reads differently from one that was
    still making progress. With ``check=True`` a non-zero exit raises
    ``subprocess.CalledProcessError`` like ``subprocess.run(check=True)``.
    """
    started = time.monotonic()
    try:
        result = run_captured_bounded(argv, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as expired:
        elapsed = time.monotonic() - started
        pytest.fail(
            f"{' '.join(argv)} timed out after {timeout:g} s "
            f"(elapsed {elapsed:.1f} s; process tree killed)\n"
            f"stdout:\n{_captured(expired.stdout)}\nstderr:\n{_captured(expired.stderr)}"
        )
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
    return result
