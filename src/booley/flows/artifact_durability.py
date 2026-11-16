"""Flow-neutral durable-file publication primitives."""

from __future__ import annotations

import glob
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from booley.runtime.atomic_files import STAGED_SUFFIX


def durable_copy(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    """Copy a regular file and durably publish its directory entry.

    The bytes are staged beside *destination* and claimed under its final name
    only when whole, so a killed writer never leaves a partial record there.
    """

    def write(writer: BinaryIO) -> None:
        with source.open("rb") as reader:
            for chunk in iter(lambda: reader.read(64 * 1024), b""):
                writer.write(chunk)

    _publish(destination, write, mode)


def durable_in_place(path: Path, *, mode: int = 0o600) -> None:
    """Make a regular file that already has its final name durable where it is.

    Opens without following a link, refuses anything but a regular file
    (``OSError``), gives it *mode* on POSIX, then persists the bytes and the
    directory entry.
    """
    flags = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    flags |= (os.O_RDWR | getattr(os, "O_BINARY", 0)) if os.name == "nt" else os.O_RDONLY
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"not a regular file: {path.name}")
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def durable_create(destination: Path, raw: bytes, *, mode: int = 0o600) -> None:
    """Create one immutable byte record and durably publish its name.

    Raises ``FileExistsError`` and leaves the existing file untouched when
    *destination* already exists.
    """
    _publish(destination, lambda writer: writer.write(raw), mode)


def _publish(destination: Path, write: Callable[[BinaryIO], object], mode: int) -> None:
    durable_directory(destination.parent)
    descriptor, raw_staged = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=STAGED_SUFFIX
    )
    staged = Path(raw_staged)
    try:
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as writer:
                write(writer)
                writer.flush()
                if os.name != "nt":
                    os.fchmod(descriptor, mode)
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _claim(staged, destination, mode)
        fsync_directory(destination.parent)
    finally:
        staged.unlink(missing_ok=True)


def _claim(staged: Path, destination: Path, mode: int) -> None:
    """Give *staged* the final name without replacing an existing one.

    A POSIX hard link is atomic and keeps the staged file's mode. Windows
    renames and sets the mode afterwards: a read-only file cannot be unlinked
    there, so a staged file stays writable until it is claimed.
    """
    if os.name == "nt":
        staged.rename(destination)
        destination.chmod(mode)
        return
    os.link(staged, destination)
    staged.unlink(missing_ok=True)


def discard_staged_files(root: Path) -> None:
    """Remove every staged durable record under *root*.

    The caller must exclude every writer of *root*: no publication may be in
    flight. A Simulation Record's manifest publication is excluded only because
    nothing can lock a record before its manifest exists.
    """
    for path in root.rglob(f".*{STAGED_SUFFIX}"):
        if path.is_file() and not path.is_symlink():
            path.unlink(missing_ok=True)


def discard_published_staging(destination: Path) -> None:
    """Drop the staged link a killed POSIX publication left beside *destination*.

    A kill between the link and the unlink leaves the record complete under its
    final name with a second link, which single-link readers refuse. Safe
    without a lock: the writer of such a sibling has already published and only
    unlinks it with ``missing_ok``. A missing destination is a no-op, so the
    caller's own read reports it as usual.
    """
    try:
        status = destination.lstat()
    except FileNotFoundError:
        return
    if status.st_nlink <= 1:
        return
    pattern = f".{glob.escape(destination.name)}.*{STAGED_SUFFIX}"
    for sibling in destination.parent.glob(pattern):
        try:
            other = sibling.lstat()
        except FileNotFoundError:
            continue
        if (other.st_dev, other.st_ino) == (status.st_dev, status.st_ino):
            sibling.unlink(missing_ok=True)


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
        path.mkdir(exist_ok=True)
        fsync_directory(path.parent)


__all__ = [
    "discard_published_staging",
    "discard_staged_files",
    "durable_copy",
    "durable_create",
    "durable_directory",
    "durable_in_place",
    "fsync_directory",
]
