"""The Git reads the web Dashboard makes: registered worktrees and uncommitted changes.

Every Git call runs with ``GIT_OPTIONAL_LOCKS=0`` so a status read never takes
``index.lock`` from under a working agent, and at bytes level so non-UTF-8 paths neither
crash the read nor change what it reports.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.git_environment import inherited_git_environment
from booley.runtime.project_repositories import parse_porcelain_z

_GIT_TIMEOUT_S = 60


class WorktreeGitError(RuntimeError):
    """A Git command the Dashboard needs failed."""


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    environment = inherited_git_environment()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_LITERAL_PATHSPECS"] = "1"
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=environment,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeGitError(f"git {args[0]} could not run in {cwd}: {exc}") from exc


def _git(cwd: Path, *args: str) -> bytes:
    """Return stdout of a Git command that must succeed."""
    result = _run(cwd, *args)
    if result.returncode != 0:
        detail = " ".join(result.stderr.decode("utf-8", "replace").split())
        raise WorktreeGitError(f"git {args[0]} failed in {cwd}: {detail}")
    return result.stdout


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def count_uncommitted(repo: Path, excluded_untracked: str | None) -> int:
    """Count ``git status`` entries, skipping untracked ones under *excluded_untracked*."""
    # Explicit flags: Git configuration (`ignore = dirty`, `showUntrackedFiles = no`)
    # must not hide changes from the count.
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--ignore-submodules=none",
        "--untracked-files=normal",
    )
    changes = parse_porcelain_z(_text(status))
    count = 0
    for change in changes:
        under_excluded = excluded_untracked is not None and (
            change.path == excluded_untracked or change.path.startswith(excluded_untracked + "/")
        )
        if change.status == "??" and under_excluded:
            continue
        count += 1
    return count


@dataclass(frozen=True)
class Registration:
    """One registered worktree of a repository."""

    path: Path
    head: str  # HEAD commit
    branch: str | None  # Branch ref; ``None``: detached HEAD
    locked: str | None  # ``git worktree lock`` reason ("" without one); ``None``: not locked


def worktree_registrations(repository: Path) -> list[Registration]:
    """Every registered worktree of *repository*, its own (main) checkout first."""
    raw = _git(repository, "worktree", "list", "--porcelain", "-z")
    records: list[list[str]] = [[]]
    for field in _text(raw).split("\0"):
        if field:
            records[-1].append(field)
        elif records[-1]:
            records.append([])
    entries: list[Registration] = []
    for record in records:
        if not record or not record[0].startswith("worktree "):
            continue
        branch = next((f[7:] for f in record if f.startswith("branch ")), None)
        head = next((f[5:] for f in record if f.startswith("HEAD ")), "")
        lock = next((f for f in record if f == "locked" or f.startswith("locked ")), None)
        entries.append(
            Registration(
                Path(os.fsdecode(record[0][9:].encode("utf-8", "surrogateescape"))),
                head,
                branch,
                None if lock is None else lock[7:],
            )
        )
    return entries
