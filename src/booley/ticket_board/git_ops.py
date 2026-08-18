"""Git worktree and branch helpers — shared by ticket board operations.

Consolidates the repeated subprocess patterns from operations.py into
reusable, best-effort helpers.  All functions are silent on failure
(print warnings to stderr) since they're used in cleanup paths.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from booley.runtime.filesystem_utils import safe_rmtree

# ---------------------------------------------------------------------------
# Low-level git wrapper
# ---------------------------------------------------------------------------


def git(*args: str, timeout: int = 10) -> subprocess.CompletedProcess[str] | None:
    """Run a git command, return CompletedProcess or None on error."""
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def git_ok(*args: str, **kwargs: int) -> str | None:
    """Run a git command and return stdout on success, None on failure."""
    r = git(*args, **kwargs)
    if r and r.returncode == 0:
        return r.stdout.strip()
    return None


# ---------------------------------------------------------------------------
# Worktree helpers
# ---------------------------------------------------------------------------


def get_main_worktree() -> str | None:
    """Return the absolute path of the main (non-linked) git worktree."""
    return git_ok("rev-parse", "--show-toplevel")


def _is_main_worktree(wt_path: str, main_wt: str | None) -> bool:
    """True if wt_path is the main worktree (case-insensitive on Windows)."""
    if not main_wt:
        return False
    return os.path.normcase(os.path.normpath(wt_path)) == os.path.normcase(
        os.path.normpath(main_wt)
    )


def find_worktree_for_branch(branch: str) -> str | None:
    """Return the worktree path for a branch, or None if not found.

    Skips the main worktree (never returns it).
    """
    r = git("worktree", "list", "--porcelain")
    if not r or r.returncode != 0:
        return None

    main_wt = get_main_worktree()
    current_wt = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            current_wt = line[len("worktree ") :]
        elif line.startswith("branch ") and current_wt:
            wt_branch = line[len("branch refs/heads/") :]
            if wt_branch == branch and not _is_main_worktree(current_wt, main_wt):
                return current_wt
    return None


def find_checkout_of_branch(branch: str) -> str | None:
    """Return the path of ANY worktree where *branch* is checked out.

    Unlike :func:`find_worktree_for_branch`, this includes the main worktree.
    Git refuses to check out a branch in two worktrees at once, so a merge
    into a checked-out branch must run *in* that checkout rather than in a
    fresh temp worktree (F-16a).
    """
    r = git("worktree", "list", "--porcelain")
    if not r or r.returncode != 0:
        return None

    current_wt = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            current_wt = line[len("worktree ") :]
        elif (
            line.startswith("branch ")
            and current_wt
            and line[len("branch refs/heads/") :] == branch
        ):
            return current_wt
    return None


def worktree_is_clean(wt_path: str) -> bool:
    """True when *wt_path* has no uncommitted changes (untracked files ok)."""
    r = git("-C", wt_path, "status", "--porcelain", "--untracked-files=no")
    return bool(r) and r.returncode == 0 and not r.stdout.strip()


def remove_worktree(wt_path: str) -> None:
    """Force-remove a worktree directory.  Best-effort with safety guard.

    Refuses to rmtree a directory containing .git/HEAD (real repo root).
    """
    git("worktree", "remove", "--force", wt_path, timeout=30)
    wt = Path(wt_path)
    if wt.exists():
        if (wt / ".git" / "HEAD").exists():
            print(f"  WARNING: skipping rmtree of {wt_path} (contains .git/HEAD)", file=sys.stderr)
        else:
            safe_rmtree(wt, protect_git_root=False)


def prune_worktrees() -> None:
    """Run git worktree prune."""
    git("worktree", "prune")


def add_worktree(path: str, branch: str, timeout: int = 30) -> tuple[bool, str]:
    """Create a worktree at *path* on *branch*.  Returns (ok, stderr)."""
    r = git("worktree", "add", path, branch, timeout=timeout)
    if not r:
        return False, "git timed out or not found"
    return r.returncode == 0, r.stderr.strip()


# ---------------------------------------------------------------------------
# Branch helpers
# ---------------------------------------------------------------------------


def branch_exists(branch: str) -> bool | None:
    """Return branch existence, or None when Git could not answer."""
    r = git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if not r:
        return None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    print(
        f"  WARNING: failed to inspect branch '{branch}': {r.stderr.strip()}",
        file=sys.stderr,
    )
    return None


def delete_branch(branch: str, *, force: bool = False) -> bool:
    """Delete a branch, returning whether Git completed the deletion."""
    flag = "-D" if force else "-d"
    r = git("branch", flag, branch)
    if not r:
        return False
    if r.returncode == 0:
        print(f"  Deleted branch: {branch}")
        return True
    if "not fully merged" in r.stderr and not force:
        print(
            f"  WARNING: branch '{branch}' has unmerged commits — "
            f"use 'git branch -D {branch}' to force-delete",
            file=sys.stderr,
        )
    else:
        print(
            f"  WARNING: failed to delete branch '{branch}': {r.stderr.strip()}",
            file=sys.stderr,
        )
    return False


def merge_branch(merge_from: str, message: str, cwd: str, timeout: int = 60) -> tuple[bool, str]:
    """Merge *merge_from* into HEAD in *cwd*.  Returns (ok, stderr)."""
    try:
        r = subprocess.run(
            ["git", "merge", "--no-ff", merge_from, "-m", message],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
        return r.returncode == 0, r.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Composite helpers
# ---------------------------------------------------------------------------


def remove_worktree_for_branch(branch: str) -> None:
    """Find and remove the worktree for *branch* (keep the branch itself)."""
    wt_path = find_worktree_for_branch(branch)
    if wt_path:
        remove_worktree(wt_path)
        print(f"  Removed worktree: {wt_path}")


def cleanup_worktree_and_branch(branch: str, *, force: bool = False) -> bool:
    """Remove a worktree and delete its branch, returning cleanup success."""
    remove_worktree_for_branch(branch)
    exists = branch_exists(branch)
    if exists is False:
        return True
    if exists is None:
        return False
    return delete_branch(branch, force=force)
