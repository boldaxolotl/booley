"""Which files of a worktree's Booley Directory Git does not keep.

Common Booley State belongs to the Main Booley Directory, so a copy inside a
worktree is evidence nobody else holds: Doctor reports it, and
``booley worktree rm`` moves it to the Main Booley Directory before deleting the
worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.core.booley_dir_parts import BooleyDirClass, iter_booley_dir
from booley.runtime import project_repositories

# Checkout-tied Flow reports (with their run artifacts, Trace Artifacts included)
# are not Common Booley State, yet they are the evidence of a worktree's Flow runs.
FLOW_REPORT_DIRS = frozenset(
    {"flow-reports", "mcp-tool-reports", ".runtime/flow-reports", ".runtime/mcp-tool-reports"}
)


def _is_nested_worktree_entry(path: str) -> bool:
    """Whether *path* holds other worktrees, never evidence: each has its own removal."""
    return path == "worktrees" or path.startswith(".baseline-wt-")


def untracked_common_paths(worktree: Path, booley_dir: Path) -> list[str]:
    """Return the Common Booley State paths in *booley_dir* that Git does not track.

    Raises:
        OSError: the directory cannot be walked.
        subprocess.CalledProcessError: ``git ls-files`` failed, so tracked paths cannot be told apart.
        ValueError: Git printed a path name that is not valid UTF-8.
    """
    common = [
        path
        for path, booley_class in iter_booley_dir(booley_dir)
        if booley_class is BooleyDirClass.COMMON
    ]
    if not common:
        return []
    tracked = tracked_booley_paths(worktree, booley_dir)
    return [path for path in common if not is_tracked(path, tracked)]


def tracked_booley_paths(worktree: Path, booley_dir: Path) -> set[str]:
    """Return Git-tracked paths under *booley_dir*, relative to it and ``/``-separated.

    Raises:
        subprocess.CalledProcessError: ``git ls-files`` failed, so tracked paths cannot be told apart.
    """
    own_repository = (booley_dir / ".git").exists()
    cwd = booley_dir if own_repository else worktree
    relative = booley_dir.relative_to(worktree).as_posix()
    args = ["ls-files", "-z"] if own_repository else ["ls-files", "-z", "--", relative]
    result = project_repositories.run_git(cwd, *args)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, args, stderr=result.stderr)
    prefix = "" if own_repository else f"{relative}/"
    return {name.removeprefix(prefix) for name in result.stdout.split("\0") if name}


def is_tracked(path: str, tracked: set[str]) -> bool:
    """Return whether *path*, or any file below it, is in *tracked*."""
    return path in tracked or any(name.startswith(f"{path}/") for name in tracked)


def worktree_evidence_paths(booley_dir: Path, *, tracked: set[str] | None) -> list[str]:
    """Return the entries of a worktree's Booley Directory that hold evidence.

    That is every Common Booley State entry except other worktrees, plus the
    Flow report directories. Paths are ``/``-separated and relative to
    *booley_dir*. With *tracked* (from :func:`tracked_booley_paths`), entries Git
    tracks are left out: they are on the branch the removal keeps.

    Raises:
        OSError: the directory cannot be walked.
    """
    entries = [
        path
        for path, booley_class in iter_booley_dir(booley_dir)
        if path in FLOW_REPORT_DIRS
        or (booley_class is BooleyDirClass.COMMON and not _is_nested_worktree_entry(path))
    ]
    if tracked is None:
        return entries
    return [path for path in entries if not is_tracked(path, tracked)]
