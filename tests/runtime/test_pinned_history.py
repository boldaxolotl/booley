"""Raw-bytes Git runner and commit-OID validation over real Git."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.git_support import git, hostile, repository

from booley.runtime.pinned_history import FileCommitError, raw_git, tree_rows, validate_pin


def _repository(root: Path, algorithm: str = "sha1") -> Path:
    """A repository with one commit that holds ``README``."""
    root.mkdir()
    git(root, "init", "-q", "-b", "main", f"--object-format={algorithm}")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "README").write_bytes(b"project\n")
    git(root, "add", "README")
    git(root, "commit", "-qm", "baseline")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repository(tmp_path / "repo")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("rev-parse", "--verify", "-q", "refs/heads/nosuch"), "failed with exit status 1"),
        pytest.param(
            ("-c", "alias.die=!kill -KILL $PPID", "die"),
            "killed by SIGKILL",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX signals"),
        ),
    ],
    ids=["exit-status", "signal"],
)
def test_a_git_failure_without_stderr_names_how_git_ended(repo, args, message):
    with pytest.raises(FileCommitError, match=message):
        raw_git(repo, *args)


@pytest.mark.parametrize("algorithm", ["sha1", "sha256"])
def test_validate_pin_accepts_only_a_full_canonical_existing_commit(tmp_path, algorithm):
    root = _repository(tmp_path / algorithm, algorithm)
    head = git(root, "rev-parse", "HEAD")
    validate_pin(root, head)
    for pin in ("HEAD", "HEAD~0", head[:-1], head.upper()):
        with pytest.raises(FileCommitError, match="full canonical"):
            validate_pin(root, pin)
    with pytest.raises(FileCommitError, match="existing commit"):
        validate_pin(root, git(root, "rev-parse", "HEAD^{tree}"))
    with pytest.raises(FileCommitError):
        validate_pin(root, "0" * len(head))


def test_raw_git_ignores_inherited_repository_selection(repo, tmp_path, monkeypatch):
    foreign = repository(tmp_path / "foreign")
    # Detached auto-maintenance would race the snapshot with a transient lock.
    git(foreign, "config", "maintenance.auto", "false")
    git(foreign, "config", "gc.auto", "0")
    before = {
        str(p.relative_to(foreign)): p.read_bytes() for p in foreign.rglob("*") if p.is_file()
    }
    for key, value in hostile(foreign).items():
        monkeypatch.setenv(key, value)
    oid = raw_git(repo, "hash-object", "-w", "--stdin", input_bytes=b"local only\n")
    oid = oid.strip().decode()
    assert (repo / ".git/objects" / oid[:2] / oid[2:]).is_file()
    assert before == {
        str(p.relative_to(foreign)): p.read_bytes() for p in foreign.rglob("*") if p.is_file()
    }


def test_raw_git_reads_objects_without_replacements(repo):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-qm", "second")
    second = git(repo, "rev-parse", "HEAD")
    git(repo, "replace", first, second)
    assert b"baseline" in raw_git(repo, "cat-file", "commit", first)


def test_explicit_index_and_object_directory_override_inherited_values(
    repo, tmp_path, monkeypatch
):
    index = tmp_path / "explicit-index"
    objects = tmp_path / "explicit-objects"
    objects.mkdir()
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "poison-index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "poison-objects"))
    raw_git(
        repo,
        "read-tree",
        "HEAD",
        env={"GIT_INDEX_FILE": str(index), "GIT_OBJECT_DIRECTORY": str(repo / ".git/objects")},
    )
    assert index.is_file()
    oid = (
        raw_git(
            repo,
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=b"explicit objects\n",
            env={"GIT_OBJECT_DIRECTORY": str(objects)},
        )
        .strip()
        .decode()
    )
    assert (objects / oid[:2] / oid[2:]).is_file()
    assert not (tmp_path / "poison-index").exists()


@pytest.mark.skipif(os.name == "nt", reason="a carriage return is not a Windows file name")
def test_tree_rows_keep_every_path_byte(repo):
    odd = b"odd\rname"
    (repo / os.fsdecode(odd)).write_bytes(b"x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "odd name")
    rows = tree_rows(repo, "HEAD")
    assert set(rows) == {b"README", odd}
    assert rows[odd].startswith(b"100644 blob ")
