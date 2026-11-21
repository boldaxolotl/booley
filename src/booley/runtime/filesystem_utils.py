"""Shared filesystem helpers for Booley infrastructure."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from booley.core.booley_layout import BOOLEY_DIR_NAME
from booley.core.sharing_retry import (
    is_transient_read_error,
    is_transient_sharing_error,
    open_with_sharing_retry,
    read_file_bytes,
    replace_file,
    unlink_file,
)

__all__ = [
    "RemovedDirectoryError",
    "is_transient_read_error",
    "is_transient_sharing_error",
    "make_directories",
    "missing_directories",
    "open_with_sharing_retry",
    "read_file_bytes",
    "replace_file",
    "run_roots",
    "safe_rmtree",
    "unlink_file",
]

logger = logging.getLogger(__name__)

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


def _strip_windows_reparse_points(root: Path) -> None:
    """Remove NTFS junctions/reparse points under root before rmtree."""
    try:
        for dirpath, dirnames, _ in os.walk(str(root), topdown=True):
            for dirname in list(dirnames):
                path = Path(dirpath) / dirname
                try:
                    attrs = getattr(path.lstat(), "st_file_attributes", 0)
                    if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        path.rmdir()
                        dirnames.remove(dirname)
                except OSError as e:
                    logger.debug("Failed to remove reparse point %s: %s", path, e)
    except OSError as e:
        logger.debug("Failed to walk %s for reparse points: %s", root, e)


def safe_rmtree(path: str | Path, *, protect_git_root: bool = True) -> None:
    """Remove a tree while handling Windows read-only files and junctions.

    Git object files and Docker-created artifacts can be read-only on Windows.
    Worktrees can also contain NTFS junctions that `shutil.rmtree` may follow
    or fail to stat. This helper centralizes the retry behavior so cleanup
    paths do not each grow their own slightly different version.
    """
    root = Path(path)
    if not root.exists():
        return
    if protect_git_root and (root / ".git" / "HEAD").exists():
        raise ValueError(f"refusing to remove git root: {root}")

    if sys.platform == "win32":
        _strip_windows_reparse_points(root)

    def _on_error(func, failed_path, _exc_info):
        # chmod follows links: never alter the target of a symlink we remove.
        if not Path(failed_path).is_symlink():
            Path(failed_path).chmod(stat.S_IWRITE)
        func(failed_path)

    shutil.rmtree(root, onerror=_on_error)
