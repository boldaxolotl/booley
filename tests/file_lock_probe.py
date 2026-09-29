"""Probe advisory file locks from a second open file description (POSIX only)."""

from __future__ import annotations

import os
from collections.abc import Iterator
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


def invocation_lock_paths(root: Path) -> Iterator[Path]:
    """Yield Simulation invocation locks without traversing Git internals.

    Git may repack and remove loose-object directories in the background.  Those
    directories cannot contain Simulation invocation locks, so pruning them also
    keeps the teardown probe from racing Git maintenance.
    """
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name != ".git"]
        parent = Path(directory)
        for filename in filenames:
            if filename.startswith(".invocation-") and filename.endswith(".lock"):
                yield parent / filename
