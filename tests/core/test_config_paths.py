"""Tests for the shared worktree-to-main-repository lookup."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.core.config_paths import main_repository_root


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_linked_worktree_marker_resolves_to_main_root(tmp_path: Path, absolute: bool) -> None:
    main = tmp_path / "main"
    gitdir = main / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    worktree = tmp_path / "feature"
    worktree.mkdir()
    marker = str(gitdir) if absolute else "../main/.git/worktrees/feature"
    (worktree / ".git").write_bytes(f"gitdir: {marker}\n".encode())

    assert main_repository_root(worktree) == main.resolve()


@pytest.mark.parametrize(
    "marker", [b"not a gitdir marker\n", b"gitdir: /somewhere/without/dotgit\n", b"\xff\xfe"]
)
def test_unrecognised_marker_is_not_a_linked_worktree(tmp_path: Path, marker: bytes) -> None:
    (tmp_path / ".git").write_bytes(marker)

    assert main_repository_root(tmp_path) is None


def test_main_checkout_and_plain_directory_have_no_main_root(tmp_path: Path) -> None:
    assert main_repository_root(tmp_path) is None
    (tmp_path / ".git").mkdir()
    assert main_repository_root(tmp_path) is None
