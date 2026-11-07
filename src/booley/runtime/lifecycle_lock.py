"""Serialize host-wide Docker topology and Session lifecycle mutations."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

from booley.runtime.auth_token import config_dir
from booley.runtime.file_lock import LockTimeoutError, release_file_lock, wait_for_file_lock

_LOCK_DIR = "locks"
_LOCK_NAME = "docker-lifecycle.lock"
DEFAULT_WAIT_TIMEOUT_SECONDS = 120.0

logger = logging.getLogger(__name__)


class LifecycleLockError(RuntimeError):
    """Another Booley host lifecycle mutation is already running."""


def _lock_owner(handle: TextIO) -> str:
    """Return the recorded owner when the platform permits a contended read."""
    try:
        handle.seek(0)
        owner = handle.read().strip()
        return owner if owner.isprintable() else "another Booley command"
    except OSError:
        return "another Booley command"


@contextmanager
def host_lifecycle_lock(
    operation: str,
    *,
    wait_timeout_s: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the host-wide lock, waiting by default for bounded contention."""
    booley_config = config_dir()
    shared_config = booley_config.parent
    if not shared_config.exists():
        shared_config.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            shared_config.chmod(0o700)
    if not booley_config.exists():
        booley_config.mkdir(exist_ok=True, mode=0o700)
        if os.name != "nt":
            booley_config.chmod(0o700)
    directory = booley_config / _LOCK_DIR
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / _LOCK_NAME
    with path.open("a+", encoding="utf-8") as handle:
        if os.name != "nt":
            path.chmod(0o600)
        try:
            wait_for_file_lock(
                handle,
                timeout_s=wait_timeout_s,
                on_wait=lambda: logger.warning(
                    "host Docker lifecycle is busy (%s); waiting up to %gs",
                    _lock_owner(handle),
                    wait_timeout_s,
                ),
            )
        except LockTimeoutError as exc:
            raise LifecycleLockError(
                f"host Docker lifecycle is busy ({_lock_owner(handle)}); "
                f"timed out after waiting {wait_timeout_s:g}s"
            ) from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} operation={operation}\n")
            handle.flush()
            yield
        finally:
            release_file_lock(handle)
