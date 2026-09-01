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

from .git_status import GitStatusEntry, parse_porcelain_v1_z

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


def _worktree_relative_path(wt_path: str, path: str | Path) -> str | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix().removeprefix("./")
    try:
        return candidate.resolve().relative_to(Path(wt_path).resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _is_allowed_unstaged_rename(
    wt_path: str,
    entry: GitStatusEntry,
    allowed: tuple[str | Path, str | Path] | None,
) -> bool:
    if allowed is None or entry.staged or not entry.unstaged:
        return False
    source = _worktree_relative_path(wt_path, allowed[0])
    destination = _worktree_relative_path(wt_path, allowed[1])
    if source is None or destination is None:
        return False
    if entry.status[1] == "R":
        return entry.path == destination and entry.source_path == source
    source_deleted = entry.status == " D" and entry.path == source
    destination_untracked = entry.status == "??" and entry.path == destination
    if not (source_deleted or destination_untracked):
        return False
    root = Path(wt_path)
    return not (root / source).exists() and (root / destination).is_file()


def worktree_is_clean(
    wt_path: str,
    *,
    allowed_unstaged_rename: tuple[str | Path, str | Path] | None = None,
) -> bool:
    """True when no blocking changes exist in *wt_path*.

    Callers may exempt one Harness-owned, unstaged Ticket Board rename. Staged
    paths and every unrelated change remain blocking.
    """
    r = git("-C", wt_path, "status", "--porcelain", "-z", "--untracked-files=all")
    if not r or r.returncode != 0:
        return False
    for entry in parse_porcelain_v1_z(r.stdout):
        if not _is_allowed_unstaged_rename(wt_path, entry, allowed_unstaged_rename):
            return False
    return True


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
        if r.returncode == 0:
            return True, r.stderr.strip()
        # A failed content merge leaves MERGE_HEAD and conflict entries behind.
        # Completion must be retryable and must not poison the target checkout.
        subprocess.run(
            ["git", "merge", "--abort"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
        return False, (r.stderr or r.stdout).strip()
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
