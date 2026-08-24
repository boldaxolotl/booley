"""Durably publish a completed artifact to project storage."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO

_COPY_BUFFER_BYTES = 8 * 1024 * 1024
_FALLOCATE_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP})


@dataclass(frozen=True)
class PublicationMetrics:
    """Timing and storage attribution for one durable publication."""

    source_bytes: int
    destination_free_bytes: int
    source_free_bytes: int
    copy_seconds: float
    file_sync_seconds: float
    directory_sync_seconds: float
    total_seconds: float

    def as_dict(self) -> dict[str, int | float]:
        """Return JSON-serializable publication metrics."""
        return asdict(self)


def _preflight(source: Path, destination: Path) -> tuple[os.stat_result, int, int]:
    source_stat = source.stat()
    source_free = shutil.disk_usage(source.parent).free
    destination_free = shutil.disk_usage(destination.parent).free
    if destination_free < source_stat.st_size:
        raise OSError(
            errno.ENOSPC,
            f"durable publication needs {source_stat.st_size} bytes, "
            f"but {destination.parent} has {destination_free} free",
            destination,
        )
    return source_stat, source_free, destination_free


def _preallocate(output: IO[bytes], size: int) -> None:
    if hasattr(os, "posix_fallocate"):
        try:
            os.posix_fallocate(output.fileno(), 0, size)
        except OSError as exc:
            if exc.errno not in _FALLOCATE_UNSUPPORTED:
                raise
            output.truncate(size)
    else:
        output.truncate(size)
    output.seek(0)


def _copy_sequential(source: IO[bytes], output: IO[bytes]) -> None:
    buffer = bytearray(_COPY_BUFFER_BYTES)
    view = memoryview(buffer)
    count = source.readinto(buffer)
    while count:
        remaining = view[:count]
        while remaining:
            written = output.write(remaining)
            if written is None or written <= 0:
                raise OSError(errno.EIO, "durable publication copy made no progress")
            remaining = remaining[written:]
        count = source.readinto(buffer)


def _sync_file(output: IO[bytes]) -> None:
    sync = getattr(os, "fdatasync", os.fsync)
    sync(output.fileno())


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _temporary_path(destination: Path) -> Path:
    suffix = f".{os.getpid()}.{time.monotonic_ns()}.tmp"
    return destination.with_name(f".{destination.name}{suffix}")


def _replace_atomically(temporary: Path, destination: Path) -> None:
    temporary.replace(destination)


def publish_durable(source: Path, destination: Path) -> PublicationMetrics:
    """Copy *source* to *destination* with atomic, synced publication."""
    started = time.monotonic()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_stat, source_free, destination_free = _preflight(source, destination)
    temporary = _temporary_path(destination)
    try:
        with (
            source.open("rb", buffering=0) as input_file,
            temporary.open("xb", buffering=0) as output_file,
        ):
            _preallocate(output_file, source_stat.st_size)
            copy_started = time.monotonic()
            _copy_sequential(input_file, output_file)
            copy_seconds = time.monotonic() - copy_started
            os.fchmod(output_file.fileno(), stat.S_IMODE(source_stat.st_mode))
            os.utime(output_file.fileno(), ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            sync_started = time.monotonic()
            _sync_file(output_file)
            file_sync_seconds = time.monotonic() - sync_started
        _replace_atomically(temporary, destination)
        sync_started = time.monotonic()
        _sync_directory(destination.parent)
        directory_sync_seconds = time.monotonic() - sync_started
    finally:
        temporary.unlink(missing_ok=True)
    return PublicationMetrics(
        source_bytes=source_stat.st_size,
        source_free_bytes=source_free,
        destination_free_bytes=destination_free,
        copy_seconds=copy_seconds,
        file_sync_seconds=file_sync_seconds,
        directory_sync_seconds=directory_sync_seconds,
        total_seconds=time.monotonic() - started,
    )
