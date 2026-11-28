"""``mkdir -p`` that never recreates a run root or a removed checkout."""

from __future__ import annotations

import errno
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from booley.core.booley_layout import BOOLEY_DIR_NAME

# Directories the running endpoint must not recreate (its checkout and Booley Directory).
_run_roots: tuple[Path, ...] = ()


class RemovedDirectoryError(FileNotFoundError):
    """Creating a directory would recreate a checkout or Booley Directory someone removed."""

    def __init__(self, directory: Path) -> None:
        super().__init__(
            errno.ENOENT, f"{directory} was removed; not recreating it", str(directory)
        )
        self.directory = directory


@contextmanager
def run_roots(*roots: Path) -> Iterator[None]:
    """Refuse recreating *roots* in :func:`make_directories` while the block runs.

    The roots are process-wide, not per thread: Flows write from worker threads. A
    process runs one endpoint invocation at a time, so nested blocks only add roots.
    """
    global _run_roots
    previous = _run_roots
    _run_roots = (*previous, *(root.absolute() for root in roots))
    try:
        yield
    finally:
        _run_roots = previous


def missing_directories(path: Path) -> list[Path]:
    """The directories ``mkdir -p path`` would create, outermost first.

    Raises :class:`RemovedDirectoryError` when that would recreate a run root, or a
    ``.booley`` whose parent is gone: someone removed it, so recreating it would
    leave an orphan that no worktree owns.
    """
    missing: list[Path] = []
    current = path.absolute()
    while not current.is_dir():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    missing.reverse()
    for index, directory in enumerate(missing):
        if directory in _run_roots:
            raise RemovedDirectoryError(directory)
        # Outside a run (a child process has no run roots) the name is the only signal.
        if directory.name == BOOLEY_DIR_NAME and index > 0:
            raise RemovedDirectoryError(missing[index - 1])
    return missing


def make_directories(path: Path) -> None:
    """``mkdir -p`` that never recreates a run root or a removed checkout.

    Directories are created one level at a time, so a root removed meanwhile fails
    the next ``mkdir`` instead of coming back.
    """
    for directory in missing_directories(path):
        directory.mkdir(exist_ok=True)
