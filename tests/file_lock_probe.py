"""Probe advisory file locks from a second open file description (POSIX only)."""

from __future__ import annotations

from pathlib import Path


def lock_is_held(path: Path) -> bool:
    """Report whether another open file description currently holds *path*'s lock."""
    import fcntl

    with path.open("a+b") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False
