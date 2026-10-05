"""Commit one file onto the checked-out branch (generic history commit sequence)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from booley.runtime import history_commit
from booley.runtime.history_commit import (
    COMMIT_ATTEMPTS,
    BranchKeptMovingError,
    DetachedHeadError,
    FileCommitError,
    HeadMismatchError,
    InvalidExpectedHeadError,
    commit_file,
)

RECORD = "notes/summary.md"


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30, env=env
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


def _index(root: Path) -> str:
    return _git(root, "ls-files", "-s")


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


def _child(root: Path, parents: list[str], entries: dict[str, tuple[str, str]]) -> str:
    """Point ``main`` at a commit of ``parents[0]``'s tree plus *entries* (path -> mode, blob)."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(root, "read-tree", parents[0], env=env)
        for path, (mode, blob) in entries.items():
            _git(root, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
        tree = _git(root, "write-tree", env=env)
    flags = [arg for parent in parents for arg in ("-p", parent)]
    commit = _git(root, "commit-tree", tree, *flags, "-m", "crafted")
    _git(root, "update-ref", "refs/heads/main", commit)
    return commit


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


# Pinned: argument validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "make_pin",
    [
        pytest.param(lambda root: "", id="empty"),
        pytest.param(lambda root: _head(root)[:12], id="abbreviated"),
        pytest.param(lambda root: _head(root).upper(), id="uppercase"),
        pytest.param(lambda root: "HEAD", id="revision-HEAD"),
        pytest.param(lambda root: "main~0", id="revision-expression"),
        pytest.param(lambda root: _head(root) + "0" * 24, id="sha256-length-in-sha1-repo"),
        pytest.param(lambda root: "0123456789" * 4, id="missing-object"),
        pytest.param(lambda root: _blob(root, "not a commit\n"), id="blob"),
        pytest.param(lambda root: _git(root, "rev-parse", "HEAD^{tree}"), id="tree"),
    ],
)
def test_invalid_pin_is_refused_before_any_write(repo: Path, make_pin) -> None:
    pin = make_pin(repo)
    head, index = _head(repo), _index(repo)
    with pytest.raises(InvalidExpectedHeadError):
        _commit(repo, expected_head=pin)
    assert (_head(repo), _index(repo)) == (head, index)
    # The record's blob was never written to the object database.
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{_blob_id(repo)}"], cwd=repo, check=False, timeout=30
    )
    assert probe.returncode != 0


def _blob_id(root: Path) -> str:
    """The record blob's ID without writing it."""
    return _git(root, "hash-object", "--", RECORD)


def test_pin_on_an_unborn_branch_is_refused(tmp_path: Path) -> None:
    root = _init(tmp_path / "unborn")
    (root / "notes").mkdir()
    (root / RECORD).write_text("summary\n", encoding="utf-8")
    tree = _git(root, "write-tree")
    orphan = _git(root, "commit-tree", tree, "-m", "loose")
    with pytest.raises(HeadMismatchError, match="unborn"):
        _commit(root, expected_head=orphan)
    unborn = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=root, check=False, timeout=30
    )
    assert unborn.returncode != 0
    assert _index(root) == ""


# Pinned: publishing ----------------------------------------------------------------------


def test_pin_commits_directly_on_it_then_stages(repo: Path) -> None:
    validated = _head(repo)
    commit = _commit(repo, expected_head=validated)
    assert _git(repo, "rev-parse", f"{commit}^") == validated
    assert _head(repo) == commit
    assert f"100644 {_record_blob(repo)} 0\t{RECORD}" in _index(repo)
    assert _git(repo, "status", "--porcelain", "--", RECORD) == ""


def test_pin_already_holding_the_file_returns_it(repo: Path) -> None:
    first = _commit(repo)
    assert _commit(repo, expected_head=first) == first


def test_pin_replaces_other_content_on_the_validated_head(repo: Path) -> None:
    first = _commit(repo)
    (repo / RECORD).write_text("edited\n", encoding="utf-8")
    commit = _commit(repo, expected_head=first)
    assert _git(repo, "show", f"{commit}:{RECORD}") == "edited"


def test_pinned_repeat_after_success_returns_the_same_commit(repo: Path) -> None:
    validated = _head(repo)
    commit = _commit(repo, expected_head=validated)
    index = _index(repo)
    assert _commit(repo, expected_head=validated) == commit
    assert _index(repo) == index


def test_pinned_repeat_after_crash_before_staging_converges(repo: Path) -> None:
    validated = _head(repo)
    index = _index(repo)

    class CrashError(Exception):
        pass

    def swap_then_crash(root, ref, commit, expected):
        history_commit.compare_and_swap(root, ref, commit, expected)
        raise CrashError

    with pytest.raises(CrashError):
        _commit(repo, expected_head=validated, swap=swap_then_crash)
    published = _head(repo)
    assert _index(repo) == index  # the crash came before staging

    assert _commit(repo, expected_head=validated) == published
    assert _git(repo, "status", "--porcelain", "--", RECORD) == ""


def test_pinned_success_keeps_a_different_user_staged_entry(repo: Path) -> None:
    validated = _head(repo)
    (repo / RECORD).write_text("user draft\n", encoding="utf-8")
    _git(repo, "add", RECORD)
    staged = _index(repo)
    source = repo / "summary.src"
    source.write_text("summary\n", encoding="utf-8")

    commit = commit_file(repo, RECORD, source, "docs: add summary", expected_head=validated)

    assert _git(repo, "show", f"{commit}:{RECORD}") == "summary"
    assert _index(repo) == staged


# Pinned: refusals leave refs and index untouched -----------------------------------------


def _assert_refused(root: Path, validated: str) -> None:
    head, index = _head(root), _index(root)
    with pytest.raises(HeadMismatchError) as caught:
        _commit(root, expected_head=validated)
    assert (_head(root), _index(root)) == (head, index)
    assert caught.value.expected == validated
    assert caught.value.actual == head
    assert "no ref was moved" in str(caught.value)


def test_pinned_refuses_a_moved_head_without_touching_the_index(repo: Path) -> None:
    validated = _head(repo)
    _concurrent_commit(repo, "after validation")
    _assert_refused(repo, validated)


def test_pinned_refuses_a_moved_head_that_holds_the_file(repo: Path) -> None:
    validated = _head(repo)
    _concurrent_commit(repo, "after validation")
    _commit(repo)  # someone else committed the same content past the validated head
    _assert_refused(repo, validated)


def test_pinned_refuses_a_head_two_commits_ahead(repo: Path) -> None:
    validated = _head(repo)
    _commit(repo, expected_head=validated)
    _concurrent_commit(repo, "on top")
    _assert_refused(repo, validated)


def test_pinned_refuses_an_unrelated_head(repo: Path) -> None:
    validated = _head(repo)
    blob = _record_blob(repo)
    orphan_tree = _git(repo, "rev-parse", f"{validated}^{{tree}}")
    orphan = _git(repo, "commit-tree", orphan_tree, "-m", "orphan")
    _child(repo, [orphan], {RECORD: ("100644", blob)})
    _assert_refused(repo, validated)


def test_pinned_refuses_a_merge_child(repo: Path) -> None:
    validated = _head(repo)
    side = _git(repo, "commit-tree", _git(repo, "rev-parse", "HEAD^{tree}"), "-m", "side")
    _child(repo, [validated, side], {RECORD: ("100644", _record_blob(repo))})
    _assert_refused(repo, validated)


def test_pinned_refuses_a_child_with_extra_changes(repo: Path) -> None:
    validated = _head(repo)
    extra = _blob(repo, "extra\n")
    _child(
        repo, [validated], {RECORD: ("100644", _record_blob(repo)), "extra.txt": ("100644", extra)}
    )
    _assert_refused(repo, validated)


def test_pinned_refuses_a_child_with_other_content(repo: Path) -> None:
    validated = _head(repo)
    _child(repo, [validated], {RECORD: ("100644", _blob(repo, "other\n"))})
    _assert_refused(repo, validated)


def test_pinned_refuses_a_child_with_executable_mode(repo: Path) -> None:
    validated = _head(repo)
    _child(repo, [validated], {RECORD: ("100755", _record_blob(repo))})
    _assert_refused(repo, validated)


def test_pinned_lost_race_is_refused_without_retry_or_index_change(repo: Path) -> None:
    validated = _head(repo)
    index = _index(repo)
    builds: list[str | None] = []
    raced: list[str] = []

    def build_then_move(root, parent, path, blob, message):
        builds.append(parent)
        commit = history_commit.build_commit(root, parent, path, blob, message)
        raced.append(_concurrent_commit(root, "raced"))
        return commit

    with pytest.raises(HeadMismatchError) as caught:
        _commit(repo, expected_head=validated, build=build_then_move)

    assert builds == [validated]
    assert _head(repo) == raced[0]
    assert _index(repo) == index
    message = str(caught.value)
    assert f"moved from the validated head {validated} to {raced[0]}" in message
    assert "no ref was moved" in message
    assert caught.value.actual == raced[0]
