"""Small durable-file primitives shared by Simulation Campaign publishers."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path


def durable_copy(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    """Copy a regular file and durably publish its directory entry.

    The destination is create-only. A process death before the directory fsync
    can leave, at worst, an interrupted attempt; no Result refers to the copy
    until all such copies and their manifest have been committed.
    """
    durable_directory(destination.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, mode)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb", closefd=False) as writer:
            for chunk in iter(lambda: reader.read(64 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
    except BaseException:
        with suppress(OSError):
            destination.unlink()
        raise
    finally:
        os.close(descriptor)
    fsync_directory(destination.parent)


def durable_create(destination: Path, raw: bytes, *, mode: int = 0o600) -> None:
    """Create one immutable byte record and durably publish its name."""
    durable_directory(destination.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as writer:
            writer.write(raw)
            writer.flush()
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
    except BaseException:
        with suppress(OSError):
            destination.unlink()
        raise
    finally:
        os.close(descriptor)
    fsync_directory(destination.parent)


def fsync_directory(directory: Path) -> None:
    """Persist directory metadata after a create-only publication."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_directory(directory: Path) -> None:
    """Create missing directory components and persist each parent entry."""
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir()
        fsync_directory(path.parent)


__all__ = ["durable_copy", "durable_create", "durable_directory", "fsync_directory"]
