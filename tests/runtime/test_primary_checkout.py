"""Doctor's lookup of the primary checkout, over real Git topologies."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.git_support import git

from booley.core.booley_layout import BooleyLayoutError
from booley.runtime.primary_checkout import primary_checkout


def _repository(root: Path, *init: str) -> None:
    root.mkdir()
    git(root, "init", "-q", "-b", "main", *init)
    git(root, "commit", "-q", "--allow-empty", "-m", "base")


def test_linked_worktree_reports_the_primary_checkout(tmp_path: Path) -> None:
    root, linked = tmp_path / "rtl", tmp_path / "linked"
    _repository(root)
    git(root, "worktree", "add", "-q", "--detach", str(linked))

    assert primary_checkout(root) == root.resolve()
    assert primary_checkout(linked) == root.resolve()


@pytest.mark.parametrize("configured", [False, True])
def test_separate_git_dir_needs_core_worktree(tmp_path: Path, configured: bool) -> None:
    root, metadata, linked = tmp_path / "rtl", tmp_path / "metadata", tmp_path / "linked"
    _repository(root, "--separate-git-dir", str(metadata))
    git(root, "worktree", "add", "-q", "--detach", str(linked))

    if configured:
        git(root, "config", "core.worktree", str(root))
        assert primary_checkout(linked) == root.resolve()
    else:
        with pytest.raises(BooleyLayoutError, match="cannot locate the primary Project Root"):
            primary_checkout(linked)


def test_a_folder_outside_git_names_the_failed_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    with pytest.raises(BooleyLayoutError, match="git rev-parse --absolute-git-dir"):
        primary_checkout(tmp_path)
