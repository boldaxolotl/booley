"""Windows anonymous-pipe readiness without changing descriptor blocking mode."""

from __future__ import annotations


def windows_pipe_readable(fd: int) -> bool:
    """Poll anonymous pipes without the Python 3.12 nonblocking prerequisite."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    available = wintypes.DWORD()
    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    peek.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    peek.restype = wintypes.BOOL
    if not peek(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error == 109:  # ERROR_BROKEN_PIPE: let os.read consume EOF.
            return True
        raise ctypes.WinError(error)
    return available.value > 0
