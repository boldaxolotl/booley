"""Merge and cleanup operations for Ticket Mode's inner project repository."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.ticket_repositories import (
    project_repository_expected,
    project_ticket_branch,
    resolve_inner_project_repo,
)


def merge_project_ticket_branch(project_root: Path, slug: str, message: str) -> tuple[bool, str]:
    """Merge a ticket's inner branch into its recorded base branch."""
    repository = resolve_inner_project_repo(project_root)
    if repository is None:
        if project_repository_expected(project_root):
            return False, "configured project repository is unavailable"
        return True, ""
    branch = project_ticket_branch(slug)
    if not _branch_exists(repository, branch):
        return False, f"expected project branch {branch!r} does not exist"
    base = _branch_upstream(repository, branch)
    if not base:
        return False, f"project branch {branch!r} has no recorded merge target"
    checkout = _find_checkout(repository, base)
    if checkout is not None:
        return _merge_in_checkout(checkout, branch, message)
    return _merge_in_temporary_worktree(repository, base, branch, message)


def cleanup_project_ticket_branch(project_root: Path, slug: str) -> bool:
    """Remove a ticket's inner worktree and branch, preserving the main checkout."""
    repository = resolve_inner_project_repo(project_root)
    if repository is None:
        return not project_repository_expected(project_root)
    branch = project_ticket_branch(slug)
    checkout = _find_checkout(repository, branch)
    if checkout is not None and checkout != repository:
        result = _git(repository, "worktree", "remove", "--force", str(checkout))
        if result.returncode != 0:
            return False
    _git(repository, "worktree", "prune")
    if not _branch_exists(repository, branch):
        return True
    return _git(repository, "branch", "-D", branch).returncode == 0


def _merge_in_checkout(checkout: Path, branch: str, message: str) -> tuple[bool, str]:
    status = _git(checkout, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        return False, status.stderr.strip()
    if status.stdout.strip():
        return False, f"project merge checkout at {checkout} has uncommitted changes"
    result = _git(checkout, "merge", "--no-ff", branch, "-m", message)
    if result.returncode != 0:
        _git(checkout, "merge", "--abort")
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, detail


def _merge_in_temporary_worktree(
    repository: Path,
    base: str,
    branch: str,
    message: str,
) -> tuple[bool, str]:
    path = Path(tempfile.mkdtemp(prefix="booley_project_merge_"))
    add = _git(repository, "worktree", "add", str(path), base)
    if add.returncode != 0:
        safe_rmtree(path, protect_git_root=False)
        return False, add.stderr.strip()
    try:
        return _merge_in_checkout(path, branch, message)
    finally:
        _git(repository, "worktree", "remove", "--force", str(path))
        if path.exists():
            safe_rmtree(path, protect_git_root=False)


def _branch_upstream(repository: Path, branch: str) -> str:
    result = _git(repository, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _branch_exists(repository: Path, branch: str) -> bool:
    result = _git(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    return result.returncode == 0


def _find_checkout(repository: Path, branch: str) -> Path | None:
    result = _git(repository, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return None
    current: Path | None = None
    wanted = f"branch refs/heads/{branch}"
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == wanted and current is not None:
            return current
    return None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
