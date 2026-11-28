"""Spawn and terminate owned subprocess groups across platforms."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypedDict

from booley.runtime.pid import open_windows_process

logger = logging.getLogger(__name__)

# ``subprocess.CREATE_NEW_PROCESS_GROUP`` exists only on Windows, so Pyright on other
# platforms cannot see it; the Windows class is also exercised there with ``is_windows=True``.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_SUSPENDED = 0x00000004
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_JOB_BASIC_ACCOUNTING = 1
_JOB_BASIC_PROCESS_ID_LIST = 3
_ERROR_MORE_DATA = 234
# Members named in one graceful request; the forced stop covers any beyond it.
_JOB_MEMBER_LIMIT = 64
_JOB_POLL_SECONDS = 0.05


class NewGroupKwargs(TypedDict, total=False):
    """Keyword arguments that start a child in its own process group or session."""

    start_new_session: bool
    creationflags: int


class _AccountingInformation(ctypes.Structure):
    """``JOBOBJECT_BASIC_ACCOUNTING_INFORMATION``."""

    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _ProcessIdList(ctypes.Structure):
    """``JOBOBJECT_BASIC_PROCESS_ID_LIST`` with room for ``_JOB_MEMBER_LIMIT`` members."""

    _fields_ = [
        ("NumberOfAssignedProcesses", ctypes.c_uint32),
        ("NumberOfProcessIdsInList", ctypes.c_uint32),
        ("ProcessIdList", ctypes.c_size_t * _JOB_MEMBER_LIMIT),
    ]


def _windows_libraries() -> tuple[Any, Any, Callable[[], int]]:
    """Return ``(kernel32, ntdll, last_error)`` with the Job Object signatures declared."""
    if sys.platform != "win32":
        raise OSError("Windows Job Objects are Windows-only")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    buffer = ctypes.c_void_p
    signatures: dict[str, tuple[list[Any], Any]] = {
        "CloseHandle": ([handle], boolean),
        "CreateJobObjectW": ([buffer, wintypes.LPCWSTR], handle),
        "AssignProcessToJobObject": ([handle, handle], boolean),
        "IsProcessInJob": ([handle, handle, ctypes.POINTER(boolean)], boolean),
        "QueryInformationJobObject": ([handle, ctypes.c_int, buffer, dword, buffer], boolean),
        "TerminateJobObject": ([handle, wintypes.UINT], boolean),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(kernel32, name)
        function.argtypes, function.restype = argtypes, restype
    ntdll.NtResumeProcess.argtypes = [handle]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    return kernel32, ntdll, ctypes.get_last_error


class _WindowsJobObject:
    """A Job Object without kill-on-close that holds one owned process tree.

    Every call on the job handle holds the lock, so a closed (and reusable) handle
    value never reaches Windows. A release during a stop is deferred to its end.
    """

    def __init__(self, kernel32: Any, last_error: Callable[[], int], handle: int) -> None:
        self._kernel32 = kernel32
        self._last_error = last_error
        self._handle: int | None = handle
        self._lock = threading.Lock()
        self._stops = 0
        self._released = False

    @classmethod
    def own(cls, process: subprocess.Popen[Any]) -> _WindowsJobObject | None:
        """Put the suspended *process* in a new job, then resume it.

        ``None`` when the job cannot be created or assigned: the child runs without
        one. A child that cannot be resumed is killed and ``OSError`` is raised.
        """
        try:
            kernel32, ntdll, last_error = _windows_libraries()
            access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME
            handle = open_windows_process(process.pid, access)
            if handle is None:
                raise OSError(f"could not open process {process.pid}: error {last_error()}")
            try:
                job = cls._assigned(kernel32, last_error, handle, process.pid)
                status = ntdll.NtResumeProcess(handle)
                if status < 0:
                    if job is not None:
                        job.release()
                    raise OSError(f"could not resume process {process.pid}: status {status}")
            finally:
                kernel32.CloseHandle(handle)
        except BaseException:
            _discard_child(process)  # never leave a suspended child behind
            raise
        return job

    @classmethod
    def _assigned(
        cls, kernel32: Any, last_error: Callable[[], int], process: int, pid: int
    ) -> _WindowsJobObject | None:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.debug("could not create a job for process %s: error %s", pid, last_error())
            return None
        if not kernel32.AssignProcessToJobObject(job, process):
            logger.debug("could not assign process %s to a job: error %s", pid, last_error())
            kernel32.CloseHandle(job)
            return None
        return cls(kernel32, last_error, job)

    @contextlib.contextmanager
    def members(self) -> Iterator[list[int]]:
        """Yield the member PIDs; none of them can be reused while the block runs."""
        handles: list[int] = []
        pids: list[int] = []
        try:
            with self._lock:
                for pid in self._listed():
                    access = _PROCESS_QUERY_LIMITED_INFORMATION
                    handle = open_windows_process(pid, access)
                    if handle is None:
                        continue
                    handles.append(handle)
                    # The listed PID may have been reused since: ask about the open process.
                    inside = ctypes.c_int()
                    found = self._kernel32.IsProcessInJob(
                        handle, self._handle, ctypes.byref(inside)
                    )
                    if found and inside.value:
                        pids.append(pid)
            yield pids
        finally:
            for handle in handles:
                self._kernel32.CloseHandle(handle)

    def _listed(self) -> list[int]:
        if self._handle is None:
            return []
        listed = _ProcessIdList()
        if not self._query(_JOB_BASIC_PROCESS_ID_LIST, listed):
            error = self._last_error()
            if error != _ERROR_MORE_DATA:  # a truncated list is still filled
                logger.debug("could not list the job's processes: error %s", error)
                return []
        return list(listed.ProcessIdList[: listed.NumberOfProcessIdsInList])

    def _query(self, information_class: int, into: ctypes.Structure) -> bool:
        return bool(
            self._kernel32.QueryInformationJobObject(
                self._handle, information_class, ctypes.byref(into), ctypes.sizeof(into), None
            )
        )

    def active(self) -> bool:
        """Whether any member is still running; ``True`` when that cannot be asked."""
        with self._lock:
            if self._handle is None:
                return False
            accounting = _AccountingInformation()
            if not self._query(_JOB_BASIC_ACCOUNTING, accounting):
                return True
            return accounting.ActiveProcesses > 0

    def terminate(self) -> None:
        """Kill every member."""
        with self._lock:
            if self._handle is not None and not self._kernel32.TerminateJobObject(self._handle, 1):
                logger.debug("could not terminate the job: error %s", self._last_error())

    @contextlib.contextmanager
    def stopping(self) -> Iterator[None]:
        """Keep the handle open until the block ends, even if it is released meanwhile."""
        with self._lock:
            self._stops += 1
        try:
            yield
        finally:
            with self._lock:
                self._stops -= 1
                self._close_if_done()

    def release(self) -> None:
        """Close the handle once no stop is in flight; the members keep running."""
        with self._lock:
            self._released = True
            self._close_if_done()

    def _close_if_done(self) -> None:
        if self._released and not self._stops and self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _discard_child(process: subprocess.Popen[Any]) -> None:
    """Kill and reap a child that could not be started properly, closing its pipes."""
    with contextlib.suppress(OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=5)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()


@dataclass(frozen=True)
class ProcessGroup:
    """Stable identity captured immediately after a grouped child is spawned."""

    id: int
    job: _WindowsJobObject | None = None


# Spawn-time identities of the owned children that have a Windows job.
_OWNED: weakref.WeakKeyDictionary[Any, ProcessGroup] = weakref.WeakKeyDictionary()


def capture_process_group(
    process: subprocess.Popen[Any] | asyncio.subprocess.Process,
) -> ProcessGroup:
    """Return the identity :func:`own_process_group` recorded, else one by process ID."""
    owned = _OWNED.get(process) if _OWNED else None
    return owned if owned is not None else ProcessGroup(process.pid)


def new_group_kwargs(*, is_windows: bool | None = None) -> NewGroupKwargs:
    """Return subprocess kwargs that create a new process group/session."""
    return _platform(is_windows).new_group_kwargs()


def owned_group_kwargs(*, is_windows: bool | None = None) -> NewGroupKwargs:
    """Return the spawn kwargs of a synchronous child whose whole tree the caller owns.

    On Windows the child starts suspended: pass it to :func:`own_process_group` as
    the next statement, and to :func:`release_process_group` once supervision ends.
    """
    return _platform(is_windows).owned_group_kwargs()


def own_process_group(
    process: subprocess.Popen[Any], *, is_windows: bool | None = None
) -> ProcessGroup:
    """Take ownership of a child just spawned with :func:`owned_group_kwargs`.

    On Windows the child joins a Job Object and is resumed; without one, stopping
    it walks the tree from its PID. Raises ``OSError`` (child killed) when it
    cannot be resumed.
    """
    if _platform(is_windows) is _PosixProcessGroups:
        return ProcessGroup(process.pid)
    job = _WindowsJobObject.own(process)
    group = ProcessGroup(process.pid, job)
    if job is not None:
        _OWNED[process] = group
    return group


def release_process_group(process: subprocess.Popen[Any]) -> None:
    """End ownership of *process*; whatever still runs in its tree keeps running."""
    group = _OWNED.pop(process, None) if _OWNED else None
    if group is not None and group.job is not None:
        group.job.release()


def foreign_group_of(pid: int) -> int | None:
    """Return *pid*'s process group, or ``None`` when it shares the caller's (POSIX).

    Raises ``ProcessLookupError`` when *pid* is gone.
    """
    group = os.getpgid(pid)
    return None if group == os.getpgrp() else group


def signal_group(group_id: int, signum: int) -> None:
    """Send *signum* to a group (POSIX); a gone or forbidden group raises."""
    os.killpg(group_id, signum)


def _taskkill(pid: int, *, force: bool) -> None:
    """Stop the tree under *pid* by walking parent IDs (needs a live leader)."""
    _run_taskkill([*(["/F"] if force else []), "/T", "/PID", str(pid)], pid)


def _run_taskkill(arguments: list[str], pid: int) -> None:
    command = ["taskkill", *arguments]
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
    job = group.job
    with job.stopping() if job is not None else contextlib.nullcontext():
        platform.request(group)
        deadline = time.monotonic() + grace_seconds
        if process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=grace_seconds)
        # The job's other members get the grace period too, even after the leader exits.
        while job is not None and job.active() and time.monotonic() < deadline:
            time.sleep(_JOB_POLL_SECONDS)
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
    def new_group_kwargs() -> NewGroupKwargs:
        return {"start_new_session": True}

    @staticmethod
    def owned_group_kwargs() -> NewGroupKwargs:
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
    def new_group_kwargs() -> NewGroupKwargs:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP}

    @staticmethod
    def owned_group_kwargs() -> NewGroupKwargs:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED}

    @staticmethod
    def request(group: ProcessGroup) -> None:
        if group.job is None:
            _taskkill(group.id, force=False)
            return
        with group.job.members() as members:
            if members:
                _run_taskkill([part for pid in members for part in ("/PID", str(pid))], group.id)

    @staticmethod
    def force(group: ProcessGroup, process: subprocess.Popen[Any]) -> None:
        if group.job is None:
            _taskkill(group.id, force=True)
        else:
            group.job.terminate()
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
        if group.job is not None:
            return group.job.active()
        # Without a job Windows has no safe stdlib probe for every member of a tree:
        # keep termination conservative even when the recorded leader has exited.
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
