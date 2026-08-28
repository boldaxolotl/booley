"""Conservative cross-platform process-liveness checks."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum
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


@dataclass(frozen=True)
class ProcessIdentity:
    """PID plus Linux identity fields that survive argv changes."""

    pid: int
    pid_namespace: str
    start_ticks: int

    def to_payload(self) -> dict[str, Any]:
        """Serialize this identity for a durable protocol record."""
        return {
            "pid": self.pid,
            "pid_namespace": self.pid_namespace,
            "start_ticks": self.start_ticks,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProcessIdentity | None:
        """Parse one untrusted protocol identity, or return ``None``."""
        try:
            data = require_dict(payload, field="process identity")
            return cls(
                pid=require_int(data.get("pid"), field="process identity pid"),
                pid_namespace=require_str(data, "pid_namespace"),
                start_ticks=require_int(
                    data.get("start_ticks"), field="process identity start_ticks"
                ),
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
    """Capture durable Linux identity for *pid*, or ``None`` if unreadable."""
    if pid <= 0:
        return None
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = _parse_proc_stat(stat)
    namespace = _read_namespace(proc_root, pid)
    if parsed is None or namespace is None:
        return None
    _state, start_ticks = parsed
    return ProcessIdentity(pid=pid, pid_namespace=namespace, start_ticks=start_ticks)


def observe_process(
    identity: ProcessIdentity, *, proc_root: Path = Path("/proc")
) -> ProcessObservation:
    """Observe *identity* without mistaking zombies or PID reuse for work."""
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
    if namespace != identity.pid_namespace or start_ticks != identity.start_ticks:
        return ProcessObservation(REUSED)
    return ProcessObservation(ZOMBIE if state == "Z" else RUNNING)


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
