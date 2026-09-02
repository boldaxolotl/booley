"""Serialize host-wide Docker topology and Session lifecycle mutations."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from booley.runtime.auth_token import config_dir
from booley.runtime.file_lock import LockContentionError, nonblocking_file_lock

_LOCK_DIR = "locks"
_LOCK_NAME = "docker-lifecycle.lock"


class LifecycleLockError(RuntimeError):
    """Another Booley host lifecycle mutation is already running."""


@contextmanager
def host_lifecycle_lock(operation: str) -> Iterator[None]:
    """Hold the one host-wide mutation lock without waiting indefinitely."""
    directory = config_dir() / _LOCK_DIR
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / _LOCK_NAME
    with path.open("a+", encoding="utf-8") as handle:
        if os.name != "nt":
            path.chmod(0o600)
        try:
            with nonblocking_file_lock(handle):
                handle.seek(0)
                handle.truncate()
                handle.write(f"pid={os.getpid()} operation={operation}\n")
                handle.flush()
                yield
        except LockContentionError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "another Booley command"
            raise LifecycleLockError(
                f"host Docker lifecycle is busy ({owner}); retry after it finishes"
            ) from exc
