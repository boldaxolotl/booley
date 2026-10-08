"""Exact raw publication proof and post-CAS index reconciliation over real Git."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from tests.runtime import test_history_commit
from tests.runtime.test_history_commit import (
    RECORD,
    _git,
    _head,
    _record_blob,
)

from booley.runtime.history_commit import FileCommitError, build_commit
from booley.runtime.pinned_history import (
    PinnedPublication,
    proves_publication,
    publish_pinned,
    raw_git,
    validate_pin,
)

repo = test_history_commit.repo


@pytest.fixture
def publication(repo):
    return PinnedPublication(_head(repo), "refs/heads/main", RECORD, _record_blob(repo))


def test_raw_proof_accepts_only_one_parent_exact_mode_blob_and_tree(repo, publication):
    commit = build_commit(repo, publication.pin, RECORD, publication.blob, "publication")
    assert proves_publication(repo, commit, publication)
    assert not proves_publication(repo, publication.pin, publication)
    assert not proves_publication(repo, commit, replace(publication, path="README"))
    other = raw_git(repo, "hash-object", "README").strip().decode()
    assert not proves_publication(repo, commit, replace(publication, blob=other))
    _git(repo, "replace", commit, publication.pin)
    assert proves_publication(repo, commit, publication)


@pytest.mark.parametrize("pin", ["HEAD", "HEAD~0", "a" * 39, "A" * 40, "0" * 40])
def test_invalid_pin_refuses_before_writes(repo, publication, pin):
    before = (repo / ".git/index").read_bytes()
    count = _git(repo, "count-objects", "-v")
    with pytest.raises(FileCommitError):
        publish_pinned(
            repo,
            replace(publication, pin=pin),
            "x",
            lambda _commit: pytest.fail("journaled invalid pin"),
        )
    assert (repo / ".git/index").read_bytes() == before
    assert _git(repo, "count-objects", "-v") == count


def test_intended_commit_is_durable_before_ref_cas_and_index_only_after_ref(repo, publication):
    seen = []
    before = (repo / ".git/index").read_bytes()

    def journal(commit):
        assert _head(repo) == publication.pin
        assert (repo / ".git/index").read_bytes() == before
        seen.append(commit)

    published = publish_pinned(repo, publication, "summary", journal)
    assert seen == [published]
    assert _git(repo, "ls-files", "--stage", RECORD).split()[1] == publication.blob
    assert (
        publish_pinned(
            repo,
            replace(publication, intended_commit=published),
            "ignored",
            lambda _: pytest.fail(),
        )
        == published
    )


@pytest.mark.parametrize(
    "stage", ["unrelated", "destination", "ancestor", "intent-to-add", "conflicted"]
)
def test_preserves_unrelated_or_conflicting_staging_without_wedging(repo, publication, stage):
    if stage == "unrelated":
        (repo / "README").write_bytes(b"user staged work\n")
        _git(repo, "add", "README")
    elif stage == "destination":
        (repo / RECORD).write_bytes(b"user's different summary\n")
        _git(repo, "add", RECORD)
    elif stage == "ancestor":
        blob = raw_git(repo, "hash-object", "-w", "README").strip().decode()
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},notes")
    elif stage == "intent-to-add":
        _git(repo, "add", "-N", RECORD)
    else:
        blob = raw_git(repo, "hash-object", "README").strip()
        import subprocess

        subprocess.run(
            ["git", "update-index", "--index-info"],
            cwd=repo,
            input=b"100644 " + blob + b" 1\t" + RECORD.encode() + b"\n",
            check=True,
            capture_output=True,
            timeout=30,
        )
    before = raw_git(repo, "ls-files", "--stage", "-z")
    commit = publish_pinned(repo, publication, "summary", lambda _: None)
    assert proves_publication(repo, commit, publication)
    after = raw_git(repo, "ls-files", "--stage", "-z")
    if stage != "unrelated":
        assert before == after
    else:
        assert before.split(b"\0")[0] == after.split(b"\0")[0]


def test_same_sha_manual_branch_switch_refuses_without_index_effect(repo, publication):
    before = (repo / ".git/index").read_bytes()

    def switch(branch):
        assert branch == publication.branch
        _git(repo, "checkout", "-qb", "sibling")

    with pytest.raises(FileCommitError, match="worktree is on"):
        publish_pinned(repo, publication, "summary", lambda _: None, check_branch=switch)
    assert _git(repo, "rev-parse", "main") == publication.pin
    assert (repo / ".git/index").read_bytes() == before


def test_concurrent_ordinary_commit_before_cas_refuses_without_staging(repo, publication):
    before = (repo / ".git/index").read_bytes()

    def journal(_commit):
        _git(repo, "commit", "--allow-empty", "-qm", "ordinary commit")

    with pytest.raises(FileCommitError, match="moved"):
        publish_pinned(repo, publication, "summary", journal)
    assert (repo / ".git/index").read_bytes() == before


def test_index_lock_conflict_after_ref_is_retryable(repo, publication):
    commit = []
    lock = repo / ".git/index.lock"

    def journal(oid):
        commit.append(oid)
        lock.write_bytes(b"user lock")

    with pytest.raises(FileCommitError, match="index is locked"):
        publish_pinned(repo, publication, "summary", journal)
    assert _head(repo) == commit[0]
    assert lock.read_bytes() == b"user lock"
    lock.unlink()
    assert (
        publish_pinned(
            repo,
            replace(publication, intended_commit=commit[0]),
            "summary",
            lambda _: pytest.fail(),
        )
        == commit[0]
    )


@pytest.mark.parametrize("algorithm", ["sha1", "sha256"])
def test_sha256_and_shallow_raw_parent_proof(tmp_path, algorithm):
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main", f"--object-format={algorithm}")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "README").write_bytes(b"base\n")
    _git(root, "add", "README")
    _git(root, "commit", "-qm", "base")
    clone = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth=1", root.as_uri(), str(clone))
    _git(clone, "config", "user.name", "Test")
    _git(clone, "config", "user.email", "test@example.invalid")
    source = clone / "summary"
    source.write_bytes(b"proof")
    blob = raw_git(clone, "hash-object", "-w", "summary").strip().decode()
    publication = PinnedPublication(_head(clone), "refs/heads/main", "summary", blob)
    commit = publish_pinned(clone, publication, "summary", lambda _: None)
    validate_pin(clone, commit)
    assert proves_publication(clone, commit, publication)


@pytest.mark.skipif(
    os.name == "nt", reason="Windows filesystems forbid carriage-return path components"
)
def test_literal_carriage_return_path_is_preserved(repo, publication):
    call = replace(publication, path="notes/carriage\r.md")
    commit = publish_pinned(repo, call, "summary", lambda _: None)
    assert proves_publication(repo, commit, call)
    assert b"carriage\r.md\0" in raw_git(repo, "ls-files", "-z")


def test_index_handoff_does_not_delete_successor_git_lock(repo, publication, monkeypatch):
    lock = repo / ".git/index.lock"
    replace_path = Path.replace

    def replace_then_acquire_successor(path, destination):
        result = replace_path(path, destination)
        if path == lock:
            lock.write_bytes(b"successor Git process owns this lock")
        return result

    monkeypatch.setattr(Path, "replace", replace_then_acquire_successor)
    commit = publish_pinned(repo, publication, "summary", lambda _: None)
    assert proves_publication(repo, commit, publication)
    assert lock.read_bytes() == b"successor Git process owns this lock"


@pytest.mark.parametrize("attack", ["summary", "recipe", "parent", "tree", "headers"])
def test_exact_object_recipe_refuses_substitution_before_any_git_write(repo, publication, attack):
    from booley.runtime.pinned_history import build_commit as build_pinned_commit
    from booley.runtime.pinned_history import restore_publication_objects

    commit = build_pinned_commit(
        repo, publication.pin, publication.path, publication.blob, "summary"
    )
    raw = raw_git(repo, "cat-file", "commit", commit)
    content = raw_git(repo, "cat-file", "blob", publication.blob)
    if attack == "summary":
        content += b"substituted"
    elif attack == "recipe":
        raw += b"substituted"
    elif attack == "parent":
        raw = raw.replace(b"parent " + publication.pin.encode(), b"parent " + commit.encode())
    elif attack == "tree":
        tree = raw_git(repo, "rev-parse", publication.pin + "^{tree}").strip()
        raw = b"tree " + tree + b"\n" + raw.split(b"\n", 1)[1]
    else:
        raw = raw.replace(b"\n\n", b"\nforeign header\n\n", 1)
    if attack in {"parent", "tree", "headers"}:
        commit = (
            raw_git(repo, "hash-object", "-t", "commit", "--stdin", input_bytes=raw)
            .strip()
            .decode()
        )
    before = (_git(repo, "count-objects", "-v"), _head(repo), (repo / ".git/index").read_bytes())
    with pytest.raises(FileCommitError):
        restore_publication_objects(
            repo, replace(publication, intended_commit=commit), content, raw
        )
    assert before == (
        _git(repo, "count-objects", "-v"),
        _head(repo),
        (repo / ".git/index").read_bytes(),
    )


def test_dry_tree_oid_preserves_git_ordering_and_rejects_committed_ancestors(repo, publication):
    from booley.runtime.pinned_history import publication_tree, publication_tree_oid

    for path in ("new/deep/summary.md", "README/summary.md", "README"):
        candidate = replace(publication, path=path)
        before = _git(repo, "count-objects", "-v")
        if path.startswith("README"):
            with pytest.raises(FileCommitError):
                publication_tree_oid(repo, candidate)
        else:
            predicted = publication_tree_oid(repo, candidate)
            assert before == _git(repo, "count-objects", "-v")
            assert predicted == publication_tree(repo, publication.pin, path, publication.blob)
