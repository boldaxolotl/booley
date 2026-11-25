"""Shared time budget for subprocesses that test helpers launch.

A helper that waits on a child process must give up well inside the per-test
limit, so a hung child fails one test with a message naming the command instead
of freezing a pytest-xdist worker until the whole job is killed. The budget is
the scaled hang guard of ``tests/time_budget.py``; the policy, and why the
per-test limit stays at least twice the guard, are in
``.github/ci/README.md#test-time-budgets``.

Shared helpers start such a child through ``run_bounded_child``: plain
``subprocess.run(timeout=...)`` kills only the direct child and, on Windows,
then waits without limit for pipes a surviving descendant holds (issue #3115).
A nested pytest session starts through ``run_nested_pytest``, which also makes a
stalled test dump every thread's stack before the hang guard fires.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping

import pytest

from booley.runtime.platform_paths import run_captured_bounded
from tests.time_budget import hang_guard_s

#: Seconds a test helper may wait for a script or external tool (git, bash, ...).
#: The scaled hang guard: 30 s, times ``BOOLEY_TEST_TIME_SCALE``.
SUBPROCESS_TIMEOUT_S = hang_guard_s()

#: Seconds one test of a nested pytest session may run before the session writes
#: every thread's stack to stderr and goes on: half the hang guard, so a hang guard
#: failure shows where the session stopped.
STALL_DUMP_S = SUBPROCESS_TIMEOUT_S / 2


def _captured(stream: str | bytes | None) -> str:
    """Render one captured stream of a timed-out child for a failure message.

    ``run_captured_bounded`` always raises with text, but typeshed types
    ``TimeoutExpired`` output as bytes, so decode defensively.
    """
    if stream is None:
        return "(not collected: a descendant still held the pipe after the kill)"
    text = stream if isinstance(stream, str) else stream.decode("utf-8", errors="replace")
    return text or "(none captured)"


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


def run_nested_pytest(
    arguments: list[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    timeout: float = SUBPROCESS_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m pytest`` with *arguments* through ``run_bounded_child``.

    pytest's built-in ``faulthandler_timeout`` dumps every thread's stack to stderr
    when one test's setup, call and teardown outlast ``STALL_DUMP_S``.
    """
    dump = f"faulthandler_timeout={STALL_DUMP_S:g}"
    return run_bounded_child(
        [sys.executable, "-m", "pytest", "-o", dump, *arguments],
        timeout=timeout,
        cwd=cwd,
        env=env,
    )
