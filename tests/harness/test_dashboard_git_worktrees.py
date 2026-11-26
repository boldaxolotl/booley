"""The Dashboard's Git reads: the uncommitted count it shows beside a worktree."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from booley.harness.dashboard.git_worktrees import count_uncommitted
from tests.git_support import git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def test_count_skips_untracked_files_under_the_excluded_booley_directory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    # A developer's global ignore file may hide `.booley`; the test counts what Git reports.
    no_excludes = tmp_path / "no-excludes"
    no_excludes.write_bytes(b"")
    git(root, "config", "core.excludesFile", str(no_excludes))
    (root / "base.txt").write_bytes(b"base\n")
    git(root, "add", "base.txt")
    git(root, "commit", "-qm", "base")

    (root / "base.txt").write_bytes(b"edited\n")
    (root / "new.txt").write_bytes(b"new\n")
    # A Snapshot's copied Booley Directory is untracked but is not the agent's work.
    (root / ".booley").mkdir()
    (root / ".booley" / "booley.toml").write_bytes(b"# copy\n")

    assert count_uncommitted(root, ".booley") == 2
    assert count_uncommitted(root, None) == 3
