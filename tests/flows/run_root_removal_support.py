"""A Flow body that removes a run root during a long tool step (#5372)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from booley.flows.base import BooleyFlow
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.pid import is_pid_alive
from tests.time_budget import hang_guard_s, scaled

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


def wait_until(done: Callable[[], object], message: str) -> None:
    deadline = time.monotonic() + hang_guard_s()
    while not done():
        assert time.monotonic() < deadline, message
        time.sleep(0.01)


def in_process_child(
    monkeypatch: pytest.MonkeyPatch, flow: BooleyFlow, argv: list[str]
) -> Callable[..., Awaitable[tuple[int, str, str, bool]]]:
    """An endpoint child for ``mcp_runner_fake``: run *flow* here, as the child would."""

    async def child(
        _cmd: object, *, env: dict[str, str], **_kwargs: object
    ) -> tuple[int, str, str, bool]:
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
    """Start a tool, remove *root*, then block on the tool's pipes.

    The tool has its own session, as a Flow's does. With *agent_cli* it shares
    this process's group and is awaited in an event loop, as a Specialist's is.
    """

    def __init__(
        self, root: Path, scratch: Path, *, recreate: bool = False, agent_cli: bool = False
    ) -> None:
        self._root = root
        self._pid_file = scratch / "orphan.pid"
        self._recreate = recreate
        self._agent_cli = agent_cli
        self._tool: subprocess.Popen[bytes] | None = None
        self._pids: list[int] = []
        self._removed_at = 0.0
        self._returned = False

    def __call__(self) -> EndpointOutcome:
        command = [sys.executable, "-c", _TOOL, _ORPHAN, str(self._pid_file), str(hang_guard_s())]
        if self._agent_cli:
            asyncio.run(self._await_agent_cli(command))
            return EndpointOutcome()
        self._tool = subprocess.Popen(
            command, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._remove_root(self._tool.pid)
        try:
            self._tool.communicate()
            self._returned = True
        finally:
            if self._recreate:
                self._root.mkdir()
        return EndpointOutcome()

    async def _await_agent_cli(self, command: list[str]) -> None:
        pipe = asyncio.subprocess.PIPE
        cli = await asyncio.create_subprocess_exec(*command, stdout=pipe, stderr=pipe)
        self._remove_root(cli.pid)
        try:
            await cli.communicate()
            self._returned = True
        finally:
            await cli.communicate()  # reap, so the closing loop holds no live transport

    def _remove_root(self, tool_pid: int) -> None:
        self._pids = [tool_pid]
        wait_until(self._pid_file.is_file, "the tool never started its child")
        self._pids.append(int(self._pid_file.read_text()))
        safe_rmtree(self._root)
        self._removed_at = time.monotonic()

    def assert_stopped(self) -> None:
        assert not self._returned, "the tool step ran to completion"
        # Below the tool's sleep, which is the whole hang guard.
        assert time.monotonic() - self._removed_at < scaled(15)
        wait_until(lambda: not any(map(is_pid_alive, self._pids)), "a tool process is left")

    def close(self) -> None:
        """Leave nothing running, whatever the run under test did."""
        for pid in self._pids:
            if is_pid_alive(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        if self._tool is not None:
            # Its session too: the child may not have reported its PID yet.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._tool.pid, signal.SIGKILL)
            self._tool.communicate()
