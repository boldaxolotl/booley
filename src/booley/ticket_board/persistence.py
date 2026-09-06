"""Crash-safe persistence primitives for Ticket control-plane records."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class WriteOnceConflictError(RuntimeError):
    """A write-once path already contains different bytes."""


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _staged_bytes(path: Path, content: bytes, mode: int) -> Path:
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


def atomic_replace_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Publish complete bytes at *path* with one atomic replacement."""
    temporary = _staged_bytes(path, content, mode)
    try:
        temporary.replace(path)
        _fsync_directory(path.parent)
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
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)
