"""Shared filesystem helpers for Booley infrastructure."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


BOOLEY_COPY_EXCLUDES = (
    ".git",
    ".venv",
    "worktrees",
    "tmp",
    "target",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "*.pyc",
)


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
        Path(failed_path).chmod(stat.S_IWRITE)
        func(failed_path)

    shutil.rmtree(root, onerror=_on_error)


def copy_booley_tree(src: Path, dst: Path) -> None:
    """Copy a .booley tree using the canonical infrastructure exclusions."""
    if dst.exists():
        # Worktree copies may contain a stale .git/ from prior runs;
        # safe to remove since this is a copy, not the real repo.
        safe_rmtree(dst, protect_git_root=False)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*BOOLEY_COPY_EXCLUDES))
