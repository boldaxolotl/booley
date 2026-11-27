"""A Flow body that removes a run root during a long tool step (#5372)."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from booley.runtime.endpoint_execution import EndpointOutcome
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.pid import is_pid_alive
from tests.time_budget import hang_guard_s

NEEDS_PROC = pytest.mark.skipif(
    not Path("/proc/self").exists(), reason="the watchdog finds tool processes through /proc"
)
# The tool dies on SIGTERM; its child ignores it and outlives it, holding the pipes.
_TOOL = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]])\n"
    "time.sleep(float(sys.argv[3]))\n"
)
_ORPHAN = (
    "import os, pathlib, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "pathlib.Path(sys.argv[1] + '.tmp').write_text(str(os.getpid()))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "time.sleep(float(sys.argv[2]))\n"
)


def watchdog_alive() -> bool:
    return any(thread.name == "booley-run-root-watchdog" for thread in threading.enumerate())


def wait_until(done, message: str) -> None:
    deadline = time.monotonic() + hang_guard_s()
    while not done():
        assert time.monotonic() < deadline, message
        time.sleep(0.01)


def in_process_child(monkeypatch: pytest.MonkeyPatch, flow, argv: list[str]):
    """An endpoint child for ``mcp_runner_fake``: run *flow* here, as the child would."""

    async def child(_cmd, *, env, **_kwargs):
        for name in ("BOOLEY_RUN_ID", "BOOLEY_RUNTIME_DIR"):
            monkeypatch.setenv(name, env[name])
        out, err = io.StringIO(), io.StringIO()
        # A child process has Python's own SIGINT handler, not this event loop's.
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = flow.main(argv)
        finally:
            signal.signal(signal.SIGINT, previous)
        return code, out.getvalue(), err.getvalue(), False

    return child


class RemovalRun:
    """Start a tool, remove *root*, then block on the tool's pipes as a Flow does."""

    def __init__(self, root: Path, scratch: Path, *, recreate: bool = False) -> None:
        self._root = root
        self._pid_file = scratch / "orphan.pid"
        self._recreate = recreate
        self._tool: subprocess.Popen[bytes] | None = None
        self._pids: list[int] = []
        self._removed_at = 0.0
        self._returned = False

    def __call__(self) -> EndpointOutcome:
        script = [_TOOL, _ORPHAN, str(self._pid_file), str(hang_guard_s())]
        self._tool = subprocess.Popen(
            [sys.executable, "-c", *script],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        wait_until(self._pid_file.is_file, "the tool never started its child")
        self._pids = [self._tool.pid, int(self._pid_file.read_text())]
        safe_rmtree(self._root)
        self._removed_at = time.monotonic()
        try:
            self._tool.communicate()
            self._returned = True
        finally:
            if self._recreate:
                self._root.mkdir()
        return EndpointOutcome()

    def assert_stopped(self) -> None:
        assert not self._returned, "the tool step ran to completion"
        assert time.monotonic() - self._removed_at < hang_guard_s() / 2
        wait_until(lambda: not any(map(is_pid_alive, self._pids)), "a tool process is left")

    def close(self) -> None:
        """Leave nothing running, whatever the run under test did."""
        for pid in self._pids:
            if is_pid_alive(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        if self._tool is not None:
            self._tool.communicate()
