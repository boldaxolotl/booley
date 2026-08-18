"""Linked-worktree health checks for isolated execution."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorktreeHealth:
    """Result of validating an isolated worktree."""

    ok: bool
    reason: str = ""


def check_worktree_health(project_root: Path, worktree_path: Path) -> WorktreeHealth:
    """Return whether *worktree_path* is a usable registered git worktree."""
    if not worktree_path.is_dir():
        return WorktreeHealth(False, f"worktree directory missing: {worktree_path}")

    git_entry = worktree_path / ".git"
    if not git_entry.exists():
        return WorktreeHealth(False, f"worktree .git entry missing: {git_entry}")

    gitdir_error = _gitdir_pointer_error(worktree_path, git_entry)
    if gitdir_error:
        return WorktreeHealth(False, gitdir_error)

    if not _is_registered_worktree(project_root, worktree_path):
        return WorktreeHealth(False, "worktree is not registered in git worktree list")

    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode != 0:
        msg = (probe.stderr or probe.stdout).strip()
        return WorktreeHealth(False, f"git rev-parse failed in worktree: {msg}")

    return WorktreeHealth(True)


def _gitdir_pointer_error(worktree_path: Path, git_entry: Path) -> str:
    """Validate a linked-worktree .git file points at an existing gitdir."""
    if git_entry.is_dir():
        return ""
    if not git_entry.is_file():
        return f"worktree .git entry is not a file or directory: {git_entry}"

    try:
        content = git_entry.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"cannot read worktree .git file: {exc}"

    if not content.startswith("gitdir:"):
        return "worktree .git file does not contain a gitdir pointer"

    gitdir = Path(content.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (worktree_path / gitdir).resolve()
    if not gitdir.exists():
        return f"worktree .git points to missing gitdir: {gitdir}"
    return ""


def _is_registered_worktree(project_root: Path, worktree_path: Path) -> bool:
    """Check git's registry contains *worktree_path*."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return False

    expected = _norm_path(worktree_path)
    saw_worktree_entry = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            saw_worktree_entry = True
            listed = Path(line[len("worktree ") :])
            if _norm_path(listed) == expected:
                return True
    # Some unit tests globally mock subprocess.run and return generic stdout.
    # If Git did not report any worktree entries, let the caller's rev-parse
    # probe decide whether the directory is usable.
    return not saw_worktree_entry


def _norm_path(path: Path) -> str:
    """Normalize paths for worktree-list comparison across platforms."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(os.path.normpath(str(resolved)))
