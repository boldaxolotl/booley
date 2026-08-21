"""Conservative cross-platform process-liveness checks."""

from __future__ import annotations

import os
import sys


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
    return True
