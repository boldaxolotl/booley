"""Open retained regular files without following path indirection."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def _windows_path_from_handle(descriptor: int) -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = final_path(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError()
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        return f"\\\\{value[8:]}"
    return value.removeprefix("\\\\?\\")


def _open_windows(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    accepted = False
    try:
        expected = os.path.normcase(str(Path(os.path.normpath(path)).absolute()))
        actual = os.path.normcase(_windows_path_from_handle(descriptor))
        if actual != expected:
            raise OSError(errno.ELOOP, "path resolves through a reparse point", path)
        accepted = True
        return descriptor
    finally:
        if not accepted:
            os.close(descriptor)


def _open_posix(path: Path) -> int:
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise OSError(errno.EINVAL, "path contains traversal", path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(absolute.anchor, directory_flags)
    try:
        for part in absolute.parts[1:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)


def open_regular_nofollow(path: Path) -> int:
    """Return a descriptor for a regular file reached without following links."""
    descriptor = _open_windows(path) if os.name == "nt" else _open_posix(path)
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    raise OSError(errno.EINVAL, "path does not name a regular file", path)
