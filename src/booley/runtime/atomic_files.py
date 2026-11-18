"""Crash-safe file publication primitives shared by durable record owners."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from booley.runtime import timefmt
from booley.runtime.filesystem_utils import replace_file, unlink_file

STAGED_SUFFIX = ".booley-staged"
"""Suffix of a durable record staged beside its final name, never read as a record."""


class WriteOnceConflictError(RuntimeError):
    """A write-once path already contains different bytes."""


def fsync_directory(directory: Path) -> None:
    """Persist a directory entry change (create, rename, unlink); a no-op on Windows."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def keep_unreadable(path: Path) -> Path:
    """Move an unreadable state file aside, beside itself, and return the kept path.

    The copy is named ``<name>.unreadable-<UTC time>``, with ``-2``, ``-3``, ...
    appended when that name is taken, so an earlier kept copy is never replaced.
    The caller holds the lock that serializes writers of *path*. ``OSError``
    propagates; one raised by the rename leaves *path* in place, one raised by
    the directory sync after it leaves the file at the kept path.
    """
    stem = f"{path.name}.unreadable-{timefmt.compact_utc_now()}"
    kept = path.with_name(stem)
    number = 1
    while kept.exists():
        number += 1
        kept = path.with_name(f"{stem}-{number}")
    replace_file(path, kept)
    fsync_directory(path.parent)
    return kept


def _staged_bytes(path: Path, content: bytes, mode: int) -> Path:
    from booley.core.booley_layout import reject_retired_path

    reject_retired_path(path.absolute())
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        temporary.chmod(mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def atomic_replace_bytes(
    path: Path, content: bytes, *, mode: int = 0o600, mtime_ns: int | None = None
) -> None:
    """Publish complete bytes at *path* with one atomic replacement.

    *mtime_ns* stamps the staged file before the rename, so the published file
    never shows another modification time.
    """
    temporary = _staged_bytes(path, content, mode)
    try:
        if mtime_ns is not None:
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
        replace_file(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_once(path: Path, content: bytes, *, mode: int = 0o600) -> bool:
    """Atomically create *path* or accept byte-identical existing content."""
    temporary = _staged_bytes(path, content, mode)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise WriteOnceConflictError(f"conflicting write-once record: {path}") from None
            return False
        fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def durable_unlink(path: Path) -> bool:
    """Remove *path* and persist the removal; return whether it existed."""
    try:
        unlink_file(path)
    except FileNotFoundError:
        return False
    fsync_directory(path.parent)
    return True
