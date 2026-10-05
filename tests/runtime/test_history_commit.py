"""Commit one file onto the checked-out branch (generic history commit sequence)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.runtime import history_commit
from booley.runtime.history_commit import (
    COMMIT_ATTEMPTS,
    BranchKeptMovingError,
    DetachedHeadError,
    FileCommitError,
    HeadMismatchError,
    commit_file,
)

RECORD = "notes/summary.md"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on ``main`` with one baseline commit and the record on disk."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "README").write_text("project\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-qm", "baseline")
    (root / "notes").mkdir()
    (root / RECORD).write_text("summary\n", encoding="utf-8")
    return root


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _concurrent_commit(root: Path, subject: str) -> str:
    """Move ``main`` the way another writer would, without touching the index."""
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    other = _git(root, "commit-tree", tree, "-p", "HEAD", "-m", subject)
    _git(root, "update-ref", "refs/heads/main", other)
    return other


def _commit(root: Path, **kwargs) -> str:
    return commit_file(root, RECORD, root / RECORD, "docs: add summary", **kwargs)


def test_commits_only_the_file_and_stages_it(repo: Path) -> None:
    (repo / "work.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "work.txt")
    base = _head(repo)

    commit = _commit(repo)

    assert commit == _head(repo)
    assert _git(repo, "rev-parse", "HEAD^") == base
    assert _git(repo, "show", "--name-only", "--format=", "HEAD") == RECORD
    assert _git(repo, "log", "-1", "--format=%s") == "docs: add summary"
    # The user's staged work stays staged, uncommitted.
    assert _git(repo, "diff", "--cached", "--name-only") == "work.txt"
    assert "booley: commit file" in _git(repo, "reflog", "-1", "--format=%gs", "main")


def test_repeat_returns_the_commit_that_holds_the_file(repo: Path) -> None:
    first = _commit(repo)
    assert _commit(repo) == first
    assert _commit(repo, expected_head=first) == first


def test_other_committed_content_is_refused(repo: Path) -> None:
    _commit(repo)
    (repo / RECORD).write_text("edited\n", encoding="utf-8")
    with pytest.raises(FileCommitError, match="already committed with other content"):
        _commit(repo)


def test_detached_head_is_refused(repo: Path) -> None:
    _git(repo, "checkout", "-q", "--detach")
    with pytest.raises(DetachedHeadError) as caught:
        _commit(repo)
    assert caught.value.repository == repo


def test_check_branch_runs_each_attempt_before_anything_is_written(repo: Path) -> None:
    seen: list[str] = []

    class RefusedError(Exception):
        pass

    def refuse(ref: str) -> None:
        seen.append(ref)
        raise RefusedError(ref)

    head = _head(repo)
    with pytest.raises(RefusedError):
        _commit(repo, check_branch=refuse)
    assert seen == ["refs/heads/main"]
    assert _head(repo) == head
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_moving_branch_is_rebuilt_then_given_up(repo: Path) -> None:
    attempts: list[str] = []

    def build_then_move(root, parent, path, blob, message):
        commit = history_commit.build_commit(root, parent, path, blob, message)
        attempts.append(_concurrent_commit(root, f"concurrent {len(attempts)}"))
        return commit

    with pytest.raises(BranchKeptMovingError):
        _commit(repo, build=build_then_move)
    assert len(attempts) == COMMIT_ATTEMPTS
    assert _head(repo) == attempts[-1]


def test_expected_head_commits_directly_on_it(repo: Path) -> None:
    validated = _head(repo)
    commit = _commit(repo, expected_head=validated)
    assert _git(repo, "rev-parse", f"{commit}^") == validated
    assert _head(repo) == commit


def test_expected_head_refuses_a_different_head_without_writing(repo: Path) -> None:
    validated = _head(repo)
    moved = _concurrent_commit(repo, "after validation")

    with pytest.raises(HeadMismatchError) as caught:
        _commit(repo, expected_head=validated)

    assert (caught.value.expected, caught.value.actual) == (validated, moved)
    assert _head(repo) == moved
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_expected_head_refuses_even_when_the_moved_head_holds_the_file(repo: Path) -> None:
    validated = _head(repo)
    _commit(repo)  # someone else committed the same content past the validated head
    with pytest.raises(HeadMismatchError):
        _commit(repo, expected_head=validated)


def test_expected_head_never_rebuilds_onto_a_moved_branch(repo: Path) -> None:
    validated = _head(repo)
    builds: list[str | None] = []

    def build_then_move(root, parent, path, blob, message):
        builds.append(parent)
        commit = history_commit.build_commit(root, parent, path, blob, message)
        _concurrent_commit(root, "raced")
        return commit

    with pytest.raises(HeadMismatchError):
        _commit(repo, expected_head=validated, build=build_then_move)

    assert builds == [validated]
    assert _git(repo, "log", "-1", "--format=%s") == "raced"
    assert _git(repo, "rev-parse", "HEAD^") == validated


def test_swap_hook_replaces_the_reflog_message(repo: Path) -> None:
    def swap(root, ref, commit, expected):
        return history_commit.compare_and_swap(
            root, ref, commit, expected, reflog_message="custom reflog"
        )

    _commit(repo, swap=swap)
    assert _git(repo, "reflog", "-1", "--format=%gs", "main") == "custom reflog"


def test_git_that_cannot_run_raises_file_commit_error(tmp_path: Path) -> None:
    with pytest.raises(FileCommitError, match="failed in"):
        history_commit.require_git(tmp_path / "missing", "status")
