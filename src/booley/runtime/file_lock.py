"""Cross-platform nonblocking file locks.

The public interface deliberately accepts an already-open file.  Callers keep
ownership of file creation, permissions, and any diagnostic content while this
module owns the platform locking semantics.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, Any


class LockContentionError(BlockingIOError):
    """The requested file lock is currently held by another process."""


def _raise_lock_error(exc: OSError) -> None:
    contention_codes = {errno.EACCES, errno.EAGAIN}
    if sys.platform == "win32":
        contention_codes.add(errno.EDEADLK)
    if exc.errno in contention_codes:
        raise LockContentionError(exc.errno, exc.strerror) from exc
    raise exc


def acquire_file_lock(handle: IO[Any]) -> None:
    """Acquire an exclusive nonblocking lock or raise ``LockContentionError``.

    Errors while preparing the file, and unexpected errors from the platform
    lock API, are allowed to propagate so callers never mistake broken storage
    for ordinary contention.
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            _raise_lock_error(exc)
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        _raise_lock_error(exc)


def release_file_lock(handle: IO[Any]) -> None:
    """Release a lock acquired by :func:`acquire_file_lock`."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def nonblocking_file_lock(handle: IO[Any]) -> Iterator[None]:
    """Hold an exclusive nonblocking lock for the duration of the context."""
    acquire_file_lock(handle)
    try:
        yield
    finally:
        release_file_lock(handle)


@contextmanager
def try_file_lock(handle: IO[Any]) -> Iterator[bool]:
    """Yield whether the nonblocking lock was acquired; propagate other errors."""
    try:
        acquire_file_lock(handle)
    except LockContentionError:
        yield False
        return
    try:
        yield True
    finally:
        release_file_lock(handle)
