"""Shared filesystem helpers for Booley infrastructure."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from pathlib import Path

from booley.core.atomic_files import fsync_directory
from booley.core.directories import (
    RemovedDirectoryError,
    make_directories,
    missing_directories,
    run_roots,
)
from booley.core.sharing_retry import (
    is_transient_read_error,
    is_transient_sharing_error,
    open_with_sharing_retry,
    read_file_bytes,
    replace_file,
    unlink_file,
)
from booley.runtime import timefmt

__all__ = [
    "RemovedDirectoryError",
    "is_transient_read_error",
    "is_transient_sharing_error",
    "keep_unreadable",
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


def keep_unreadable(path: Path) -> Path:
    """Move an unreadable state file aside, beside itself, and return the kept path.

    The copy is named ``<name>.unreadable-<UTC time>``, with ``-2``, ``-3``, ...
    appended when that name is taken, so an earlier kept copy is never replaced.
    The caller holds the lock that serializes writers of *path*. ``OSError``
    propagates; one raised by the rename leaves *path* in place, one raised by
    the directory sync after it leaves the file at the kept path.
    """
    stem = f"{path.name}.unreadable-{timefmt.compact_utc_now()}"
    kept = path.with_name(stem)
    number = 1
    while kept.exists():
        number += 1
        kept = path.with_name(f"{stem}-{number}")
    replace_file(path, kept)
    fsync_directory(path.parent)
    return kept


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
