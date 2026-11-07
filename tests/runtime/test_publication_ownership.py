"""Summary staging anchors in the worktree root, whatever depth the Booley Directory has."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from booley.runtime.history_commit import FileCommitError
from booley.runtime.publication_ownership import PublicationOwnership

# The checkout's Booley Directory is two components deep (`[project].dir`).
PREFIX = "tools/booley"


def _ownership(directory: Path) -> PublicationOwnership:
    """An ownership whose sealed records live in memory."""
    sealed: dict[str, dict[str, Any]] = {}
    directory.mkdir(parents=True, exist_ok=True)
    return PublicationOwnership(
        directory,
        sealed.__setitem__,
        lambda name: sealed[name],
        lambda _name: None,
    )


def _worktree(tmp_path: Path, ignore: str) -> tuple[Path, Path]:
    """A Git worktree root with *ignore* as its root ``.gitignore`` and the destination path."""
    root = tmp_path / "wt"
    (root / PREFIX / "goals" / "history").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, timeout=30)
    (root / ".gitignore").write_bytes(ignore.encode())
    return root, root / PREFIX / "goals" / "history" / "g.md"


def test_summary_anchor_uses_worktree_root_for_nested_prefix(tmp_path: Path) -> None:
    # The managed ignore rule is matched relative to the worktree root. Taking the
    # fourth parent of the destination (`tools`) as the root checks the wrong path.
    root, destination = _worktree(tmp_path, f"/{PREFIX}/goals/.publication-*/\n")
    ownership = _ownership(tmp_path / "operation")
    ownership.validate_summary_anchor(destination, root)
    ownership.materialize(destination, root, b"summary\n")
    assert destination.read_bytes() == b"summary\n"


def test_summary_anchor_refuses_without_managed_ignore_policy(tmp_path: Path) -> None:
    root, destination = _worktree(tmp_path, "")
    with pytest.raises(FileCommitError, match="transient ignore policy"):
        _ownership(tmp_path / "operation").validate_summary_anchor(destination, root)


def test_summary_anchor_refuses_symlinked_anchor(tmp_path: Path) -> None:
    root, destination = _worktree(tmp_path, f"/{PREFIX}/goals/.publication-*/\n")
    ownership = _ownership(tmp_path / "operation")
    anchor = destination.parent.parent / (".publication-" + ownership.directory.name)
    try:
        anchor.symlink_to(tmp_path)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(FileCommitError, match="publication staging anchor"):
        ownership.validate_summary_anchor(destination, root)
