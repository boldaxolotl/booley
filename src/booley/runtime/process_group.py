"""Spawn and terminate owned subprocess groups across platforms."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


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
    return _platform(is_windows).new_group_kwargs()


def _taskkill(pid: int, *, force: bool) -> None:
    command = ["taskkill"]
    if force:
        command.append("/F")
    command.extend(["/T", "/PID", str(pid)])
    try:
        result = subprocess.run(command, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("taskkill failed for process group %s: %s", pid, exc)
        return
    if result.returncode != 0:
        logger.debug("taskkill exited %s for process group %s", result.returncode, pid)


def request_group_termination(
    group: ProcessGroup,
    *,
    is_windows: bool | None = None,
) -> None:
    """Request graceful termination without waiting for the group."""
    _platform(is_windows).request(group)


def force_group_termination(
    process: subprocess.Popen[Any],
    group: ProcessGroup,
    *,
    is_windows: bool | None = None,
) -> None:
    """Force termination without waiting for the group."""
    _platform(is_windows).force(group, process)


def terminate_process_group(
    process: subprocess.Popen[Any],
    group: ProcessGroup,
    *,
    grace_seconds: float = 2,
    is_windows: bool | None = None,
) -> None:
    """Gracefully terminate an owned group, then force surviving members."""
    platform = _platform(is_windows)
    platform.request(group)
    if process.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace_seconds)
    if platform.alive(group):
        platform.force(group, process)
    if process.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=5)


async def _async_taskkill(pid: int, *, force: bool) -> None:
    command = ["taskkill"]
    if force:
        command.append("/F")
    command.extend(["/T", "/PID", str(pid)])
    try:
        killer = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.debug("could not launch taskkill for process group %s: %s", pid, exc)
        return
    try:
        returncode = await asyncio.wait_for(killer.wait(), timeout=10)
    except TimeoutError:
        logger.debug("taskkill timed out for process group %s", pid)
        with contextlib.suppress(ProcessLookupError, OSError):
            killer.kill()
        with contextlib.suppress(ProcessLookupError, TimeoutError, OSError):
            await asyncio.wait_for(killer.wait(), timeout=1)
        return
    if returncode != 0:
        logger.debug("taskkill exited %s for process group %s", returncode, pid)


class _PosixProcessGroups:
    """POSIX process-group operations."""

    @staticmethod
    def new_group_kwargs() -> dict[str, Any]:
        return {"start_new_session": True}

    @staticmethod
    def request(group: ProcessGroup) -> None:
        try:
            os.killpg(group.id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("could not request termination of process group %s: %s", group.id, exc)

    @staticmethod
    def force(group: ProcessGroup, process: subprocess.Popen[Any]) -> None:
        del process
        try:
            os.killpg(group.id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("could not force termination of process group %s: %s", group.id, exc)

    @staticmethod
    def force_now(group: ProcessGroup, process: asyncio.subprocess.Process) -> None:
        del process
        try:
            os.killpg(group.id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("could not force termination of process group %s: %s", group.id, exc)

    @staticmethod
    def alive(group: ProcessGroup) -> bool:
        try:
            os.killpg(group.id, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    @staticmethod
    async def request_async(group: ProcessGroup) -> None:
        _PosixProcessGroups.request(group)

    @staticmethod
    async def force_async(group: ProcessGroup) -> None:
        try:
            os.killpg(group.id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("could not force termination of process group %s: %s", group.id, exc)


class _WindowsProcessGroups:
    """Windows process-tree operations."""

    @staticmethod
    def new_group_kwargs() -> dict[str, Any]:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    @staticmethod
    def request(group: ProcessGroup) -> None:
        _taskkill(group.id, force=False)

    @staticmethod
    def force(group: ProcessGroup, process: subprocess.Popen[Any]) -> None:
        _taskkill(group.id, force=True)
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()

    @staticmethod
    def force_now(group: ProcessGroup, process: asyncio.subprocess.Process) -> None:
        _taskkill(group.id, force=True)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()

    @staticmethod
    def alive(group: ProcessGroup) -> bool:
        # Windows has no safe stdlib probe for every member of a process tree.
        # Keep cleanup conservative even when the recorded leader has exited.
        del group
        return True

    @staticmethod
    async def request_async(group: ProcessGroup) -> None:
        await _async_taskkill(group.id, force=False)

    @staticmethod
    async def force_async(group: ProcessGroup) -> None:
        await _async_taskkill(group.id, force=True)


_Platform = type[_PosixProcessGroups] | type[_WindowsProcessGroups]


def _platform(is_windows: bool | None = None) -> _Platform:
    windows = sys.platform == "win32" if is_windows is None else is_windows
    return _WindowsProcessGroups if windows else _PosixProcessGroups


def force_async_process_group_now(
    process: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """Synchronously signal a forced stop during coroutine cancellation."""
    _platform().force_now(group, process)


async def _reap_async_leader(
    process: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    if process.returncode is not None:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (ProcessLookupError, TimeoutError, OSError) as exc:
        logger.debug("could not reap process-group leader %s: %s", group.id, exc)


async def force_async_process_group(
    process: asyncio.subprocess.Process,
    group: ProcessGroup,
) -> None:
    """Force an asyncio child group down and reap the direct child."""
    await _platform().force_async(group)
    await _reap_async_leader(process, group)


async def terminate_async_process_group(
    process: asyncio.subprocess.Process,
    group: ProcessGroup,
    *,
    grace_seconds: float = 2,
) -> None:
    """Gracefully terminate an asyncio child group, then force survivors."""
    platform = _platform()
    await platform.request_async(group)
    if process.returncode is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    if platform.alive(group):
        await platform.force_async(group)
    await _reap_async_leader(process, group)


async def terminate_adopted_process_group(
    group: ProcessGroup,
    *,
    grace_seconds: float = 2,
) -> None:
    """Terminate a group whose original ``Process`` object is unavailable."""
    platform = _platform()
    await platform.request_async(group)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_seconds
    while platform.alive(group) and loop.time() < deadline:
        await asyncio.sleep(0.1)
    if platform.alive(group):
        await platform.force_async(group)
