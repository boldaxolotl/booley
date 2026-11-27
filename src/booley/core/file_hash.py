"""SHA-256 of one file by path, read in bounded chunks.

A function whose job is digesting one file calls this module, so the chunking,
the unreadable-file variant and the Windows sharing retry are stated here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

from booley.core.sharing_retry import open_with_sharing_retry

_CHUNK_BYTES = 1 << 20


def file_sha256_and_size(path: Path, *, retry_sharing: bool = False) -> tuple[str, int]:
    """Hex SHA-256 and byte count of *path*; ``OSError`` propagates.

    ``retry_sharing`` opens through ``open_with_sharing_retry`` for a file
    Booley replaces atomically while another process polls it (Windows only).
    """

    def open_file() -> BinaryIO:
        return path.open("rb")

    digest = hashlib.sha256()
    size = 0
    with open_with_sharing_retry(open_file, path) if retry_sharing else open_file() as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def file_sha256(path: Path, *, retry_sharing: bool = False) -> str:
    """Hex SHA-256 of *path*'s bytes; ``OSError`` propagates."""
    return file_sha256_and_size(path, retry_sharing=retry_sharing)[0]


def file_sha256_or_none(path: Path, *, retry_sharing: bool = False) -> str | None:
    """Hex SHA-256 of *path*'s bytes, or ``None`` when it is absent or unreadable."""
    try:
        return file_sha256(path, retry_sharing=retry_sharing)
    except OSError:
        return None
