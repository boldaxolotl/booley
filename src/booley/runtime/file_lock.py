"""Compatibility imports; shared host storage lives in booley.core.file_lock."""

from booley.core.file_lock import (
    LockContentionError,
    LockTimeoutError,
    acquire_file_lock,
    nonblocking_file_lock,
    release_file_lock,
    try_file_lock,
    wait_for_file_lock,
)

__all__ = [
    "LockContentionError",
    "LockTimeoutError",
    "acquire_file_lock",
    "nonblocking_file_lock",
    "release_file_lock",
    "try_file_lock",
    "wait_for_file_lock",
]
