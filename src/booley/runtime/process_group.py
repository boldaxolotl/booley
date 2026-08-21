"""Spawn and terminate owned subprocess groups across platforms."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProcessGroup:
    """Stable identity captured immediately after a grouped child is spawned."""

    id: int


def capture_process_group(
    process: subprocess.Popen[Any] | asyncio.subprocess.Process,
) -> ProcessGroup:
    """Capture the group identity established by :func:`new_group_kwargs`."""
    return ProcessGroup(process.pid)


def new_group_kwargs(*, is_windows: bool | None = None) -> dict[str, Any]:
    """Return subprocess kwargs that create a new process group/session."""
    windows = sys.platform == "win32" if is_windows is None else is_windows
    if windows:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _taskkill(pid: int, *, force: bool) -> None:
    command = ["taskkill"]
    if force:
        command.append("/F")
    command.extend(["/T", "/PID", str(pid)])
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(command, capture_output=True, timeout=10, check=False)


def request_group_termination(
    process: subprocess.Popen[Any],
    group: ProcessGroup,
    *,
    is_windows: bool | None = None,
) -> None:
    """Request graceful termination without waiting for the group."""
    windows = sys.platform == "win32" if is_windows is None else is_windows
    if windows:
        _taskkill(group.id, force=False)
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(group.id, signal.SIGTERM)


def force_group_termination(
    process: subprocess.Popen[Any],
    group: ProcessGroup,
    *,
    is_windows: bool | None = None,
) -> None:
    """Force termination without waiting for the group."""
    windows = sys.platform == "win32" if is_windows is None else is_windows
    if windows:
        _taskkill(group.id, force=True)
        with contextlib.suppress(OSError):
            process.kill()
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(group.id, signal.SIGKILL)


def terminate_process_group(
    process: subprocess.Popen[Any],
    group: ProcessGroup,
    *,
    grace_seconds: float = 2,
    is_windows: bool | None = None,
) -> None:
    """Gracefully terminate an owned group, then force surviving members."""
    windows = sys.platform == "win32" if is_windows is None else is_windows
    if windows and process.poll() is not None:
        return
    request_group_termination(process, group, is_windows=windows)
    if process.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace_seconds)
    if windows:
        if process.poll() is None:
            force_group_termination(process, group, is_windows=True)
        return
    try:
        os.killpg(group.id, 0)
    except ProcessLookupError:
        return
    except OSError:
        pass
    else:
        force_group_termination(process, group, is_windows=False)


async def _async_taskkill(pid: int, *, force: bool) -> None:
    command = ["taskkill"]
    if force:
        command.append("/F")
    command.extend(["/T", "/PID", str(pid)])
    with contextlib.suppress(Exception):
        killer = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=10)


def force_async_process_group_now(
    process: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """Synchronously signal a forced stop during coroutine cancellation."""
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(group.id, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return


async def force_async_process_group(
    process: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """Force an asyncio child group down and reap the direct child."""
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        await _async_taskkill(group.id, force=True)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(group.id, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=5)


async def terminate_async_process_group(
    process: asyncio.subprocess.Process,
    group: ProcessGroup,
    *,
    grace_seconds: float = 2,
) -> None:
    """Gracefully terminate an asyncio child group, then force and reap it."""
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        await _async_taskkill(group.id, force=False)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(group.id, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    await force_async_process_group(process, group)
