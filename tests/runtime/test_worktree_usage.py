"""The last-used stamp: which paths count as interactive worktrees."""

from __future__ import annotations

import os
from pathlib import Path

from tests.goals.conftest import git

from booley.runtime import worktree_usage


def test_only_interactive_worktrees_are_stamped(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "commit", "-q", "--allow-empty", "-m", "base")
    common = main / ".booley"
    interactive = common / "worktrees" / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(interactive))
    outside = tmp_path / "outside"
    git(main, "worktree", "add", "-q", "-b", "outside", str(outside))
    (common / "worktrees" / "plain").mkdir()
    (interactive / "sub").mkdir()
    stamps = common / "worktrees" / ".last-used"

    for path in (outside, common / "worktrees" / "plain", main, common / "worktrees"):
        worktree_usage.record_worktree_use(path, common)
    assert not stamps.exists()

    worktree_usage.record_worktree_use(interactive / "sub", common)
    assert [entry.name for entry in stamps.iterdir()] == ["wt"]


def test_last_used_reads_the_stamp_mtime(tmp_path: Path) -> None:
    assert worktree_usage.last_used(tmp_path, "wt") is None

    worktree_usage.stamp_worktree(tmp_path, "wt")
    os.utime(tmp_path / "worktrees" / ".last-used" / "wt", (1000, 1000))

    assert worktree_usage.last_used(tmp_path, "wt") == 1000
