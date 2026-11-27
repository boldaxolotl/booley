"""Where the per-Project worktree mutation lock lives outside Git."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from booley.core import booley_layout
from booley.core.worktree_lock import worktree_mutation_lock


def test_lock_outside_git_keeps_host_config_private(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with worktree_mutation_lock(root):
        directory = booley_layout.layout_metadata(root)

    assert (directory / "worktree-mutation.lock").is_file()
    assert not (root / ".git").exists()
    if os.name != "nt":
        assert stat.S_IMODE(booley_layout.config_dir().stat().st_mode) == 0o700
