from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from booley.harness.worktree_health import check_worktree_health


def test_missing_gitdir_pointer_marks_worktree_unhealthy(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    worktree = project_root / ".booley_project" / "worktrees" / "run-1"
    worktree.mkdir(parents=True)
    missing_gitdir = project_root / ".git" / "worktrees" / "run-1"
    (worktree / ".git").write_text(f"gitdir: {missing_gitdir}\n", encoding="utf-8")

    health = check_worktree_health(project_root, worktree)

    assert not health.ok
    assert "missing gitdir" in health.reason


def test_unregistered_worktree_marks_unhealthy(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    worktree = project_root / ".booley_project" / "worktrees" / "run-1"
    (worktree / ".git").mkdir(parents=True)

    listed = subprocess.CompletedProcess(
        args=["git", "worktree", "list", "--porcelain"],
        returncode=0,
        stdout=f"worktree {project_root}\nbranch refs/heads/main\n",
        stderr="",
    )
    with patch("booley.harness.worktree_health.subprocess.run", return_value=listed):
        health = check_worktree_health(project_root, worktree)

    assert not health.ok
    assert "not registered" in health.reason
