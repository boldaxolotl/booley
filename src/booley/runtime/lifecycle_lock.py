"""Serialize host-wide Docker topology and Session lifecycle mutations."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

from booley.runtime.auth_token import config_dir
from booley.runtime.file_lock import (
    LockContentionError,
    LockTimeoutError,
    acquire_file_lock,
    release_file_lock,
    wait_for_file_lock,
)

_LOCK_DIR = "locks"
_LOCK_NAME = "docker-lifecycle.lock"

logger = logging.getLogger(__name__)


class LifecycleLockError(RuntimeError):
    """Another Booley host lifecycle mutation is already running."""


def _lock_owner(handle: TextIO) -> str:
    """Return the recorded owner when the platform permits a contended read."""
    try:
        handle.seek(0)
        return handle.read().strip() or "another Booley command"
    except OSError:
        return "another Booley command"


@contextmanager
def host_lifecycle_lock(
    operation: str,
    *,
    wait_timeout_s: float | None = None,
) -> Iterator[None]:
    """Hold the host-wide lock, optionally waiting for bounded contention."""
    directory = config_dir() / _LOCK_DIR
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / _LOCK_NAME
    with path.open("a+", encoding="utf-8") as handle:
        if os.name != "nt":
            path.chmod(0o600)
        try:
            if wait_timeout_s is None:
                acquire_file_lock(handle)
            else:
                wait_for_file_lock(
                    handle,
                    timeout_s=wait_timeout_s,
                    on_wait=lambda: logger.warning(
                        "host Docker lifecycle is busy (%s); waiting up to %gs",
                        _lock_owner(handle),
                        wait_timeout_s,
                    ),
                )
        except (LockContentionError, LockTimeoutError) as exc:
            owner = _lock_owner(handle)
            timeout = (
                "retry after it finishes"
                if wait_timeout_s is None
                else f"timed out after waiting {wait_timeout_s:g}s"
            )
            raise LifecycleLockError(
                f"host Docker lifecycle is busy ({owner}); {timeout}"
            ) from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} operation={operation}\n")
            handle.flush()
            yield
        finally:
            release_file_lock(handle)
