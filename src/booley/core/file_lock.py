"""Cross-platform file locks with nonblocking and bounded-wait acquisition.

The public interface deliberately accepts an already-open file.  Callers keep
ownership of file creation, permissions, and any diagnostic content while this
module owns the platform locking semantics.
"""

from __future__ import annotations

import contextvars
import errno
import io
import math
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import IO, Any

_CHILD_LEASE_FD: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "booley_child_lease_fd", default=None
)


def active_child_lease_fd() -> int | None:
    """Return the leased descriptor a supervised child must inherit, if any."""
    return _CHILD_LEASE_FD.get()


def set_child_lease_fd(fd: int) -> contextvars.Token[int | None]:
    """Mark one leased descriptor for child inheritance in this context."""
    return _CHILD_LEASE_FD.set(fd)


def reset_child_lease_fd(token: contextvars.Token[int | None]) -> None:
    """Clear a previously installed child lease descriptor."""
    _CHILD_LEASE_FD.reset(token)


class LockContentionError(BlockingIOError):
    """The requested file lock is currently held by another process."""


class LockTimeoutError(TimeoutError):
    """The requested file lock stayed contended for the allowed wait."""


def _raise_lock_error(exc: OSError) -> None:
    contention_codes = {errno.EACCES, errno.EAGAIN}
    if sys.platform == "win32":
        contention_codes.add(errno.EDEADLK)
    if exc.errno in contention_codes:
        raise LockContentionError(exc.errno, exc.strerror) from exc
    raise exc


def acquire_file_lock(handle: IO[Any], *, preserve_contents: bool = False) -> None:
    """Acquire an exclusive nonblocking lock or raise ``LockContentionError``.

    ``preserve_contents`` locks existing Windows byte ranges beyond EOF without
    padding empty files. Errors from preparation or the platform API propagate,
    so broken storage never becomes ordinary contention.
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0 and not preserve_contents:
                # Keep the normal producer's sentinel byte. Observers can
                # lock beyond EOF without padding the existing file.
                handle.write("\0" if isinstance(handle, io.TextIOBase) else b"\0")
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


def wait_for_file_lock(
    handle: IO[Any],
    *,
    timeout_s: float,
    on_wait: Callable[[], None] | None = None,
    preserve_contents: bool = False,
) -> None:
    """Acquire a lock within *timeout_s*, reporting contention periodically."""
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise ValueError("file-lock timeout must be finite and non-negative")
    deadline = time.monotonic() + timeout_s
    next_report = 0.0
    first_attempt = True
    while first_attempt or time.monotonic() <= deadline:
        first_attempt = False
        try:
            if preserve_contents:
                acquire_file_lock(handle, preserve_contents=True)
            else:
                acquire_file_lock(handle)
            return
        except LockContentionError:
            now = time.monotonic()
            if on_wait is not None and now >= next_report:
                on_wait()
                next_report = now + 30.0
            remaining = deadline - now
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))
    raise LockTimeoutError(f"file lock remained busy for {timeout_s:g}s")


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
