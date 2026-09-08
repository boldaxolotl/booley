"""Public worktree-relocation boundary behavior."""

import subprocess
from pathlib import Path

import pytest

from booley.runtime.worktree_relocation import (
    WorktreeMove,
    WorktreeRelocationError,
    preflight_worktree_moves,
    relocate_worktree,
)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    repository = tmp_path / "r"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "tracked").write_text("value\n", encoding="utf-8")
    _git(repository, "add", "tracked")
    _git(repository, "commit", "-m", "initial")
    source = tmp_path / "w"
    _git(repository, "worktree", "add", "-b", "ticket", str(source))
    return repository, source, "refs/heads/ticket"


def test_preflight_rejects_occupied_destination(tmp_path: Path) -> None:
    repository, source, ref = _linked_worktree(tmp_path)
    destination = tmp_path / "d"
    destination.mkdir()

    with pytest.raises(WorktreeRelocationError, match="destination already exists"):
        preflight_worktree_moves((WorktreeMove(repository, ref, source, destination),))

    assert source.is_dir()


def test_relocation_replay_is_idempotent(tmp_path: Path) -> None:
    repository, source, ref = _linked_worktree(tmp_path)
    destination = tmp_path / "nested/d"

    preflight_worktree_moves((WorktreeMove(repository, ref, source, source),))
    relocate_worktree(repository, ref, source, destination)
    relocate_worktree(repository, ref, source, destination)

    assert _git(destination, "branch", "--show-current") == "ticket"


def test_relocation_rejects_missing_state(tmp_path: Path) -> None:
    repository, source, ref = _linked_worktree(tmp_path)
    source.rename(tmp_path / "lost")
    move = WorktreeMove(repository, ref, source, tmp_path / "d")

    with pytest.raises(WorktreeRelocationError, match="source is unavailable"):
        preflight_worktree_moves((move,))
    with pytest.raises(WorktreeRelocationError, match="both unavailable"):
        relocate_worktree(repository, ref, source, move.destination)


def test_relocation_rejects_unregistered_ref(tmp_path: Path) -> None:
    repository, source, _ref = _linked_worktree(tmp_path)
    move = WorktreeMove(repository, "refs/heads/missing", source, tmp_path / "d")

    with pytest.raises(WorktreeRelocationError, match="is not registered"):
        preflight_worktree_moves((move,))
    with pytest.raises(WorktreeRelocationError, match="is not registered"):
        relocate_worktree(repository, move.ref, source, move.destination)


def test_relocation_rejects_nonrepository(tmp_path: Path) -> None:
    with pytest.raises(WorktreeRelocationError, match="git worktree list --porcelain failed"):
        relocate_worktree(tmp_path, "refs/heads/ticket", tmp_path / "w", tmp_path / "d")
