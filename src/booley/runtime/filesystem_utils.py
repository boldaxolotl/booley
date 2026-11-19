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

    The roots are process-wide, not per thread: Flows write from worker threads.
    """
    global _run_roots
    previous = _run_roots
    _run_roots = (*previous, *(root.absolute() for root in roots))
    try:
        yield
    finally:
        _run_roots = previous


def make_directories(path: Path) -> None:
    """``mkdir -p`` that never recreates a run root or a removed checkout.

    A missing run root, or a missing ``.booley`` under a missing parent, raises
    :class:`RemovedDirectoryError` before anything is created. Directories are
    created one level at a time, so a root removed meanwhile fails the next
    ``mkdir`` instead of coming back.
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
        if directory.name == BOOLEY_DIR_NAME and index > 0:
            raise RemovedDirectoryError(missing[index - 1])
    for directory in missing:
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
