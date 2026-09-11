"""Conservative cross-platform process-liveness checks."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_int, require_str


class ProcessState(StrEnum):
    """Observation of one durable process identity."""

    RUNNING = "running"
    ZOMBIE = "zombie"
    DEAD = "dead"
    REUSED = "reused"
    UNKNOWN = "unknown"


RUNNING = ProcessState.RUNNING
ZOMBIE = ProcessState.ZOMBIE
DEAD = ProcessState.DEAD
REUSED = ProcessState.REUSED
UNKNOWN = ProcessState.UNKNOWN

_IDENTITY_KINDS = {
    "linux-procfs-start",
    "posix-ps-start",
    "windows-creation-time",
}


@dataclass(frozen=True)
class ProcessIdentity:
    """PID plus platform identity fields that survive argv changes."""

    pid: int
    identity_scope: str
    start_token: int
    identity_kind: str = "linux-procfs-start"

    @property
    def pid_namespace(self) -> str:
        """Compatibility name for callers displaying the identity scope."""
        return self.identity_scope

    @property
    def start_ticks(self) -> int:
        """Compatibility name for callers comparing the identity token."""
        return self.start_token

    def to_payload(self) -> dict[str, Any]:
        """Serialize this identity for a durable protocol record."""
        return {
            "pid": self.pid,
            "identity_kind": self.identity_kind,
            "identity_scope": self.identity_scope,
            "start_token": self.start_token,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProcessIdentity | None:
        """Parse one untrusted protocol identity, or return ``None``."""
        try:
            data = require_dict(payload, field="process identity")
            scope_key = "identity_scope" if "identity_scope" in data else "pid_namespace"
            token_key = "start_token" if "start_token" in data else "start_ticks"
            identity_scope = require_str(data, scope_key)
            identity_kind = data.get("identity_kind")
            if identity_kind is None:
                if identity_scope == "windows":
                    identity_kind = "windows-creation-time"
                elif identity_scope.startswith("ps:"):
                    identity_kind = "posix-ps-start"
                else:
                    identity_kind = "linux-procfs-start"
            kind = require_str({"identity_kind": identity_kind}, "identity_kind")
            if kind not in _IDENTITY_KINDS:
                raise BoundaryError(f"unsupported process identity kind {kind}")
            if kind == "windows-creation-time" and identity_scope != "windows":
                raise BoundaryError("Windows process identity has an invalid scope")
            if kind == "posix-ps-start" and not identity_scope.startswith("ps:"):
                raise BoundaryError("POSIX process identity has an invalid scope")
            return cls(
                pid=require_int(data.get("pid"), field="process identity pid"),
                identity_scope=identity_scope,
                start_token=require_int(data.get(token_key), field="process identity start token"),
                identity_kind=kind,
            )
        except BoundaryError:
            return None


@dataclass(frozen=True)
class ProcessObservation:
    """Current state of one previously captured process identity."""

    state: ProcessState


def _parse_proc_stat(stat: str) -> tuple[str, int] | None:
    """Return ``(state, start_ticks)`` from one ``/proc/<pid>/stat`` line."""
    try:
        fields = stat.rsplit(")", 1)[1].split()
        return fields[0], int(fields[19])
    except (IndexError, ValueError):
        return None


def _read_namespace(proc_root: Path, pid: int) -> str | None:
    try:
        return str((proc_root / str(pid) / "ns" / "pid").readlink())
    except OSError:
        return None


def capture_process_identity(
    pid: int, *, proc_root: Path = Path("/proc")
) -> ProcessIdentity | None:
    """Capture durable process identity for *pid*, or ``None`` if unreadable."""
    if pid <= 0:
        return None
    if sys.platform == "win32" and proc_root == Path("/proc"):
        return _windows_process_identity(pid)
    if not sys.platform.startswith("linux") and proc_root == Path("/proc"):
        return _portable_process_identity(pid)
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = _parse_proc_stat(stat)
    namespace = _read_namespace(proc_root, pid)
    if parsed is None or namespace is None:
        return None
    _state, start_ticks = parsed
    return ProcessIdentity(pid=pid, identity_scope=namespace, start_token=start_ticks)


def _observe_windows_process(identity: ProcessIdentity) -> ProcessObservation:
    if sys.platform != "win32":
        return ProcessObservation(UNKNOWN)
    current = _windows_process_identity(identity.pid)
    if current is None:
        state = UNKNOWN if _windows_pid_alive(identity.pid) else DEAD
        return ProcessObservation(state)
    if current.start_token != identity.start_token:
        return ProcessObservation(REUSED)
    state = RUNNING if _windows_pid_alive(identity.pid) else DEAD
    return ProcessObservation(state)


def _portable_process_identity(pid: int) -> ProcessIdentity | None:
    """Use the POSIX process start string when procfs is unavailable."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = result.stdout.strip()
    if result.returncode != 0 or not started:
        return None
    start_token = int.from_bytes(sha256(started.encode()).digest()[:8], "big")
    return ProcessIdentity(
        pid=pid,
        identity_scope=f"ps:{sys.platform}",
        start_token=start_token,
        identity_kind="posix-ps-start",
    )


def _observe_portable_process(identity: ProcessIdentity) -> ProcessObservation:
    current = _portable_process_identity(identity.pid)
    if current is None:
        state = UNKNOWN if is_pid_alive(identity.pid) else DEAD
        return ProcessObservation(state)
    if current != identity:
        return ProcessObservation(REUSED)
    state = RUNNING if is_pid_alive(identity.pid) else DEAD
    return ProcessObservation(state)


def _observe_procfs_process(
    identity: ProcessIdentity, *, proc_root: Path = Path("/proc")
) -> ProcessObservation:
    try:
        stat = (proc_root / str(identity.pid) / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProcessObservation(DEAD)
    except OSError:
        return ProcessObservation(UNKNOWN)
    parsed = _parse_proc_stat(stat)
    namespace = _read_namespace(proc_root, identity.pid)
    if parsed is None or namespace is None:
        return ProcessObservation(UNKNOWN)
    state, start_ticks = parsed
    if namespace != identity.identity_scope or start_ticks != identity.start_token:
        return ProcessObservation(REUSED)
    return ProcessObservation(ZOMBIE if state == "Z" else RUNNING)


def observe_process(
    identity: ProcessIdentity, *, proc_root: Path = Path("/proc")
) -> ProcessObservation:
    """Observe *identity* without mistaking zombies or PID reuse for work."""
    if identity.identity_kind == "windows-creation-time":
        return _observe_windows_process(identity)
    if identity.identity_kind == "posix-ps-start":
        return _observe_portable_process(identity)
    return _observe_procfs_process(identity, proc_root=proc_root)


def _windows_pid_alive(pid: int) -> bool:
    """Return whether a Windows PID is live, treating uncertainty as live."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Invalid PID is the one definitive "not found" result.  Access
        # denial and transient failures must not make a live owner reapable.
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        exit_code = wintypes.DWORD()
        still_active = 259
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == still_active
        return True
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_identity(pid: int) -> ProcessIdentity | None:
    """Capture a Windows PID's creation time as its durable identity."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    process_query_limited_information = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        start_ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ProcessIdentity(
            pid=pid,
            identity_scope="windows",
            start_token=start_ticks,
            identity_kind="windows-creation-time",
        )
    finally:
        kernel32.CloseHandle(handle)


def _linux_pid_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    parsed = _parse_proc_stat(stat)
    return parsed is None or parsed[0] != "Z"


def is_pid_alive(pid: int) -> bool:
    """Return whether *pid* is live, conservatively on indeterminate errors."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    if sys.platform.startswith("linux"):
        return _linux_pid_alive(pid)
    return True
