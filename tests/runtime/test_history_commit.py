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
    commit_file,
)

RECORD = "notes/summary.md"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


def _init(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on ``main`` with one baseline commit and the record on disk."""
    root = _init(tmp_path / "repo")
    (root / "README").write_text("project\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-qm", "baseline")
    (root / "notes").mkdir()
    (root / RECORD).write_text("summary\n", encoding="utf-8")
    return root


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _blob(root: Path, text: str) -> str:
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=root,
        input=text,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _record_blob(root: Path) -> str:
    return _blob(root, (root / RECORD).read_text(encoding="utf-8"))


def _concurrent_commit(root: Path, subject: str) -> str:
    """Move ``main`` the way another writer would, without touching the index."""
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    other = _git(root, "commit-tree", tree, "-p", "HEAD", "-m", subject)
    _git(root, "update-ref", "refs/heads/main", other)
    return other


def _commit(root: Path, **kwargs) -> str:
    return commit_file(root, RECORD, root / RECORD, "docs: add summary", **kwargs)


# Unpinned ----------------------------------------------------------------------------


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
    assert _git(repo, "reflog", "-1", "--format=%gs", "main") == "booley: commit file"


def test_repeat_returns_the_commit_that_holds_the_file(repo: Path) -> None:
    first = _commit(repo)
    assert _commit(repo) == first


def test_unborn_branch_gets_a_root_commit(tmp_path: Path) -> None:
    root = _init(tmp_path / "unborn")
    (root / "notes").mkdir()
    (root / RECORD).write_text("summary\n", encoding="utf-8")

    commit = _commit(root)

    assert _head(root) == commit
    assert _git(root, "rev-list", "--parents", "-n", "1", commit) == commit
    assert _git(root, "ls-files") == RECORD


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

    with pytest.raises(BranchKeptMovingError) as caught:
        _commit(repo, build=build_then_move)
    assert len(attempts) == COMMIT_ATTEMPTS
    assert _head(repo) == attempts[-1]
    assert "retried" not in str(caught.value)


def test_swap_hook_replaces_the_reflog_message(repo: Path) -> None:
    def swap(root, ref, commit, expected):
        return history_commit.compare_and_swap(
            root, ref, commit, expected, reflog_message="custom reflog"
        )

    _commit(repo, swap=swap)
    assert _git(repo, "reflog", "-1", "--format=%gs", "main") == "custom reflog"


def test_compare_and_swap_accepts_a_ref_already_at_the_commit(repo: Path) -> None:
    base = _head(repo)
    commit = history_commit.build_commit(repo, base, RECORD, _record_blob(repo), "m")
    assert history_commit.compare_and_swap(repo, "refs/heads/main", commit, base)
    # update-ref now fails (the ref is no longer at base), but it holds the commit.
    assert history_commit.compare_and_swap(repo, "refs/heads/main", commit, base)
    assert _head(repo) == commit


def test_compare_and_swap_reports_moved_ref(repo: Path) -> None:
    base = _head(repo)
    commit = history_commit.build_commit(repo, base, RECORD, _record_blob(repo), "m")
    _concurrent_commit(repo, "moved")
    assert history_commit.compare_and_swap(repo, "refs/heads/main", commit, base) is False


def test_compare_and_swap_raises_when_update_ref_fails_on_an_unmoved_ref(repo: Path) -> None:
    base = _head(repo)
    missing = "f" * len(base)
    with pytest.raises(FileCommitError, match="git update-ref refs/heads/main failed"):
        history_commit.compare_and_swap(repo, "refs/heads/main", missing, base)
    assert _head(repo) == base


def test_git_that_cannot_run_raises_file_commit_error(tmp_path: Path) -> None:
    with pytest.raises(FileCommitError, match="failed in"):
        history_commit.require_git(tmp_path / "missing", "status")
