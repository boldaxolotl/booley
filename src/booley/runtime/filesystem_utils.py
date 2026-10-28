"""Shared filesystem helpers for Booley infrastructure."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


# Windows refuses to rename onto a file that another handle holds open without
# FILE_SHARE_DELETE, which is how Python's open() opens files. A reader polling
# a protocol file therefore makes a concurrent atomic replace fail transiently,
# and the replace in flight makes a concurrent read fail the same way.
_WINDOWS_TRANSIENT_SHARING_ERRORS = frozenset({5, 32})  # ACCESS_DENIED, SHARING_VIOLATION
_SHARING_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.25, 0.25)


def _is_transient_windows_sharing_error(exc: PermissionError) -> bool:
    return getattr(exc, "winerror", None) in _WINDOWS_TRANSIENT_SHARING_ERRORS


def _is_transient_windows_read_error(exc: PermissionError) -> bool:
    """Match a sharing error raised by ``open()``, which goes through the C runtime.

    The Windows C runtime reports a sharing violation, or a file pending delete
    during a concurrent ``os.replace``, as plain ``EACCES`` without ``winerror``.
    On POSIX a bare ``EACCES`` is a real permission error and is never retried.
    """
    if _is_transient_windows_sharing_error(exc):
        return True
    return (
        sys.platform == "win32"
        and getattr(exc, "winerror", None) is None
        and exc.errno == errno.EACCES
    )


def replace_file(
    source: Path,
    destination: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Atomically replace ``destination`` with ``source``, tolerating Windows readers.

    Retries only the transient Windows sharing errors, with a bounded backoff
    of about 1.1 s in total; every other error, and the last transient one,
    propagates unchanged. POSIX renames never hit this path.
    """
    for delay in _SHARING_RETRY_DELAYS_SECONDS:
        try:
            source.replace(destination)
        except PermissionError as exc:
            if not _is_transient_windows_sharing_error(exc):
                raise
            logger.debug("Retrying replace of %s after %s", destination, exc)
            sleep(delay)
        else:
            return
    source.replace(destination)


def unlink_file(
    path: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Remove ``path``, tolerating a concurrent reader on Windows.

    Counterpart of :func:`replace_file`: a reader holding the file open without
    FILE_SHARE_DELETE makes the unlink fail transiently. Retries only those
    transient Windows sharing errors with the same bounded backoff (about 1.1 s
    in total); the last transient error, ``FileNotFoundError`` and every other
    ``OSError`` propagate unchanged. POSIX unlinks never hit the retry path.
    """
    for delay in _SHARING_RETRY_DELAYS_SECONDS:
        try:
            path.unlink()
        except PermissionError as exc:
            if not _is_transient_windows_sharing_error(exc):
                raise
            logger.debug("Retrying unlink of %s after %s", path, exc)
            sleep(delay)
        else:
            return
    path.unlink()


def read_file_bytes(
    path: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Read ``path``, tolerating a concurrent atomic replace on Windows.

    Counterpart of :func:`replace_file`: retries only the transient Windows
    sharing errors, including the bare ``EACCES`` the C runtime raises from
    ``open()``, with the same bounded backoff (about 1.1 s in total). A real
    permission denial on Windows therefore raises after that backoff. The
    last transient error, ``FileNotFoundError`` and every other ``OSError``
    propagate unchanged, so an unreadable file is never reported as absent.
    POSIX reads never hit the retry path.
    """
    for delay in _SHARING_RETRY_DELAYS_SECONDS:
        try:
            return _read_all(path)
        except PermissionError as exc:
            if not _is_transient_windows_read_error(exc):
                raise
            logger.debug("Retrying read of %s after %s", path, exc)
            sleep(delay)
    return _read_all(path)


def _read_all(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read()


def _strip_windows_reparse_points(root: Path) -> None:
    """Remove NTFS junctions/reparse points under root before rmtree."""
    try:
        for dirpath, dirnames, _ in os.walk(str(root), topdown=True):
            for dirname in list(dirnames):
                path = Path(dirpath) / dirname
                try:
                    attrs = getattr(path.lstat(), "st_file_attributes", 0)
                    if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        path.rmdir()
                        dirnames.remove(dirname)
                except OSError as e:
                    logger.debug("Failed to remove reparse point %s: %s", path, e)
    except OSError as e:
        logger.debug("Failed to walk %s for reparse points: %s", root, e)


def safe_rmtree(path: str | Path, *, protect_git_root: bool = True) -> None:
    """Remove a tree while handling Windows read-only files and junctions.

    Git object files and Docker-created artifacts can be read-only on Windows.
    Worktrees can also contain NTFS junctions that `shutil.rmtree` may follow
    or fail to stat. This helper centralizes the retry behavior so cleanup
    paths do not each grow their own slightly different version.
    """
    root = Path(path)
    if not root.exists():
        return
    if protect_git_root and (root / ".git" / "HEAD").exists():
        raise ValueError(f"refusing to remove git root: {root}")

    if sys.platform == "win32":
        _strip_windows_reparse_points(root)

    def _on_error(func, failed_path, _exc_info):
        Path(failed_path).chmod(stat.S_IWRITE)
        func(failed_path)

    shutil.rmtree(root, onerror=_on_error)
