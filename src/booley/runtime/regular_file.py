"""Open retained regular files without following path indirection."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from booley.core.booley_dir_links import is_booley_dir_entry, lexical_booley_path


def _windows_path_from_handle(descriptor: int) -> str:  # pragma: no cover
    """Resolve a Windows descriptor; exercised by the native Windows CI shards."""
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


def _open_windows(path: Path, booley_dir: Path | None) -> int:  # pragma: no cover
    """Reject Windows reparse-point traversal; exercised by native Windows CI.

    The Booley Directory entry's reparse point is absorbed by re-spelling the final
    path through the entry before the comparison.
    """
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    accepted = False
    try:
        expected = os.path.normcase(str(Path(os.path.normpath(path)).absolute()))
        final = Path(_windows_path_from_handle(descriptor))
        if booley_dir is not None:
            # A link above the entry stays refused, as on POSIX: re-spelling through
            # the entry must not also absorb an aliased ancestor.
            entry_parent = booley_dir.absolute().parent
            if os.path.normcase(str(entry_parent.resolve())) != os.path.normcase(
                str(entry_parent)
            ):
                raise OSError(errno.ELOOP, "path resolves through a reparse point", path)
            final = lexical_booley_path(booley_dir.absolute(), final)
        actual = os.path.normcase(str(final))
        if actual != expected:
            raise OSError(errno.ELOOP, "path resolves through a reparse point", path)
        accepted = True
        return descriptor
    finally:
        if not accepted:
            os.close(descriptor)


def _open_posix(path: Path, booley_dir: Path | None) -> int:
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise OSError(errno.EINVAL, "path contains traversal", path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(absolute.anchor, directory_flags)
    try:
        walked = Path(absolute.anchor)
        for part in absolute.parts[1:-1]:
            walked /= part
            # Only the Booley Directory entry may be a followed link.
            flags = (
                os.O_RDONLY | os.O_DIRECTORY
                if is_booley_dir_entry(walked, booley_dir)
                else directory_flags
            )
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)


def open_regular_nofollow(path: Path, *, booley_dir: Path | None = None) -> int:
    """Return a descriptor for a regular file reached without following links.

    The exact *booley_dir* entry, when given, is the one component that may be a
    symlink; links above or below it and a linked final component are refused.
    """
    descriptor = (
        _open_windows(path, booley_dir) if os.name == "nt" else _open_posix(path, booley_dir)
    )
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    raise OSError(errno.EINVAL, "path does not name a regular file", path)
