"""Crash-safe file publication primitives shared by durable record owners."""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from booley.core.sharing_retry import replace_file, unlink_file

STAGED_SUFFIX = ".booley-staged"
"""Suffix of a durable record staged beside its final name, never read as a record."""


class WriteOnceConflictError(RuntimeError):
    """A write-once path already contains different bytes."""


class Durability(StrEnum):
    """What a publication fsyncs before it returns."""

    FULL = "full"
    """The staged file before the replace, and its directory after it."""
    FILE = "file"
    """The staged file only."""
    NONE = "none"
    """Nothing."""


def fsync_directory(directory: Path) -> None:
    """Persist a directory entry change (create, rename, unlink); a no-op on Windows."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _staged_bytes(path: Path, content: bytes, *, mode: int | None, durability: Durability) -> Path:
    # Deferred: booley_layout imports private_store, which imports this module.
    from booley.core.booley_layout import reject_retired_path
    from booley.core.directories import make_directories

    reject_retired_path(path.absolute())
    make_directories(path.parent)
    # with_name keeps *path*'s own path class, so the replace and cleanup are calls on
    # that type, which a test can schedule step by step.
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    # Mode 0o666 lets the kernel apply the umask, which cannot be read thread-safely.
    descriptor = os.open(temporary, flags, 0o666 if mode is None else 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            if mode is not None:
                # After the write, which would clear set-id bits for an unprivileged owner.
                temporary.chmod(mode)
            if durability is not Durability.NONE:
                os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def atomic_replace_bytes(
    path: Path,
    content: bytes,
    *,
    mode: int | None = 0o600,
    keep_mode: bool = False,
    durability: Durability = Durability.FULL,
    mtime_ns: int | None = None,
) -> None:
    """Publish complete bytes at *path* with one atomic replacement.

    The file gets *mode*; ``mode=None`` is the process's umask default, and
    *keep_mode* takes the existing file's mode instead, falling back to *mode*
    when *path* is absent. The replace is the commit point: a failure before it
    leaves the previous file and no staged file. A ``FULL`` directory fsync that
    fails after it raises with the new content already published.

    *mtime_ns* stamps the staged file before the rename, so the published file
    never shows another modification time.
    """
    if keep_mode and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    temporary = _staged_bytes(path, content, mode=mode, durability=durability)
    try:
        if mtime_ns is not None:
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
        replace_file(temporary, path)
        if durability is Durability.FULL:
            fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: int | None = 0o600,
    keep_mode: bool = False,
    durability: Durability = Durability.FULL,
) -> None:
    """Publish *text* as UTF-8, newlines unchanged; see :func:`atomic_replace_bytes`."""
    atomic_replace_bytes(
        path, text.encode("utf-8"), mode=mode, keep_mode=keep_mode, durability=durability
    )


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably publish *payload* as one canonical JSON line, readable by the owner only."""
    atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")


def atomic_write_once(path: Path, content: bytes, *, mode: int = 0o600) -> bool:
    """Atomically create *path* or accept byte-identical existing content."""
    temporary = _staged_bytes(path, content, mode=mode, durability=Durability.FULL)
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
