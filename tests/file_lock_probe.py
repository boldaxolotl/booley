"""Probe advisory file locks from a second open file description (POSIX only)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO


def handle_lock_is_held(handle: BinaryIO) -> bool:
    """Report whether another open file description holds the lock on *handle*'s inode.

    The inode may already be unlinked: ``flock`` works on the open description.
    """
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def lock_is_held(path: Path) -> bool:
    """Report whether another open file description currently holds *path*'s lock."""
    with path.open("a+b") as probe:
        return handle_lock_is_held(probe)


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


def open_invocation_locks(root: Path) -> list[tuple[Path, BinaryIO]]:
    """Open a read handle on every Simulation invocation lock under *root*.

    The suite's ``tmp_path`` retention policy deletes a passing test's directory
    before the teardown guard runs.  A handle keeps each lock's inode reachable,
    so ``handle_lock_is_held`` can still probe it.  The caller closes the
    handles.  A lock removed between the walk and the open was released, so it
    is skipped.
    """
    with ExitStack() as opened:
        locks: list[tuple[Path, BinaryIO]] = []
        for path in sorted(invocation_lock_paths(root)):
            try:
                handle = path.open("rb")
            except FileNotFoundError:
                continue
            opened.callback(handle.close)
            locks.append((path, handle))
        opened.pop_all()
        return locks
