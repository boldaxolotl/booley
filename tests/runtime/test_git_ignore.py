"""Real Git ignore membership preserves file operands and explicit directory intent."""

from __future__ import annotations

import os
import subprocess

import pytest
from tests.conftest import symlink_or_skip
from tests.goals.conftest import git

from booley.runtime.git_ignore import ignored_paths


@pytest.fixture
def repository(tmp_path, monkeypatch, isolated_git_attributes):
    for key in tuple(os.environ):
        if key.startswith("GIT_CONFIG_") and key not in {
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
        }:
            monkeypatch.delenv(key)
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    empty = tmp_path / "empty-excludes"
    empty.write_bytes(b"")
    git(root, "config", "core.excludesFile", str(empty))
    return root


@pytest.mark.parametrize("exists", [False, True])
def test_directory_only_rule_matches_explicit_directory(repository, exists):
    path = repository / ".baseline-wt-123-abc"
    (repository / ".gitignore").write_text("/.baseline-wt-*/\n")
    if exists:
        path.mkdir()
    assert ignored_paths(repository, [path], directories=frozenset({path})) == {path}
    assert path.exists() is exists


def test_missing_unhinted_path_and_existing_file_are_not_directories(repository):
    path = repository / ".baseline-wt-123-abc"
    (repository / ".gitignore").write_text("/.baseline-wt-*/\n")
    assert ignored_paths(repository, [path]) == set()
    assert not path.exists()
    path.write_bytes(b"file")
    assert ignored_paths(repository, [path]) == set()


@pytest.mark.parametrize("exists", [False, True])
def test_file_rule_matches_file_operand(repository, exists):
    path = repository / "data.hex"
    (repository / ".gitignore").write_text("*.hex\n")
    if exists:
        path.write_bytes(b"data")
    assert ignored_paths(repository, [path]) == {path}
    assert path.exists() is exists


@pytest.mark.parametrize("directory", [False, True])
def test_symlink_leaf_keeps_git_file_semantics(repository, directory):
    target = repository / "target"
    target.mkdir() if directory else target.write_bytes(b"data")
    link = repository / "linked"
    symlink_or_skip(link, target, target_is_directory=directory)
    (repository / ".gitignore").write_text("/linked/\n")
    assert ignored_paths(repository, [link]) == set()
    (repository / ".gitignore").write_text("/linked\n")
    assert ignored_paths(repository, [link]) == {target.resolve()}


def test_mixed_batch_is_nul_safe_and_honors_negation(repository):
    names = ["has space", "has\nnewline", "δ-data", "kept", "ordinary.txt"]
    paths = [repository / name for name in names]
    (repository / ".gitignore").write_text("/*/\n!/kept/\n*.txt\n", encoding="utf-8")
    assert ignored_paths(repository, paths, directories=frozenset(paths[:-1])) == {
        *paths[:3],
        paths[-1],
    }
    assert all(not path.exists() for path in paths)


def test_existing_directory_needs_no_hint(repository):
    path = repository / "scratch"
    path.mkdir()
    (repository / ".gitignore").write_text("/scratch/\n")
    assert ignored_paths(repository, [path]) == {path}


def test_no_index_controls_tracked_file_membership(repository):
    path = repository / "tracked.hex"
    path.write_bytes(b"data")
    git(repository, "add", "tracked.hex")
    (repository / ".gitignore").write_text("*.hex\n")
    assert ignored_paths(repository, [path]) == set()
    assert ignored_paths(repository, [path], no_index=True) == {path}


def test_prospective_publication_file_matches_ancestor_rule_without_mkdir(repository):
    parent = repository / "goals/.publication-token"
    staged = parent / "staging"
    history = repository / "goals/history/summary.md"
    (repository / ".gitignore").write_text("/goals/*/\n!/goals/history/\n")
    assert not parent.exists()
    assert ignored_paths(repository, [staged, history], no_index=True) == {staged}
    assert not parent.exists()


def test_empty_batch_needs_no_repository(tmp_path):
    assert ignored_paths(tmp_path / "absent", []) == set()


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_git_error_transport_remains_oserror(repository, monkeypatch, failure):
    from booley.runtime import git_ignore

    original = subprocess.run

    def fail(command, **kwargs):
        if command[1] != "check-ignore":
            return original(command, **kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 128, b"", b"test Git failure")

    monkeypatch.setattr(git_ignore.subprocess, "run", fail)
    with pytest.raises(OSError, match="timed out" if failure == "timeout" else "test Git failure"):
        ignored_paths(repository, [repository / "absent"])
