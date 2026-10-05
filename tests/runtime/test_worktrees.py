"""The shared ``git worktree list --porcelain`` parser reports Git's records verbatim."""

import shutil
import subprocess
from pathlib import Path

import pytest

from booley.runtime.worktrees import WorktreeEntry, list_worktrees, parse_worktree_porcelain

SHA = "f4350f4c37961587b52be96f4d1f8a0c131b3ef2"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


@pytest.fixture
def primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed primary checkout whose name holds a space and non-ASCII text."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "pri mary-é"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.test")
    _git(root, "commit", "-q", "--allow-empty", "-m", "initial")
    return root


def _by_path(entries: tuple[WorktreeEntry, ...]) -> dict[Path, WorktreeEntry]:
    return {entry.path.resolve(): entry for entry in entries}


def test_real_listing_reports_primary_linked_detached_locked_and_prunable(primary: Path):
    tmp = primary.parent
    head = _git(primary, "rev-parse", "HEAD").strip()
    _git(primary, "worktree", "add", "-q", "-b", "feature", str(tmp / "linked tree"))
    _git(primary, "worktree", "add", "-q", "--detach", str(tmp / "detached"))
    _git(
        primary,
        "worktree",
        "add",
        "-q",
        "--lock",
        "--reason",
        "on usb",
        "-b",
        "lk",
        str(tmp / "lk"),
    )
    _git(primary, "worktree", "add", "-q", "-b", "plain-lock", str(tmp / "lk2"))
    _git(primary, "worktree", "lock", str(tmp / "lk2"))  # no reason given
    _git(primary, "worktree", "add", "-q", "-b", "gone", str(tmp / "gone"))
    shutil.rmtree(tmp / "gone")

    entries = list_worktrees(primary)

    assert entries == parse_worktree_porcelain(_git(primary, "worktree", "list", "--porcelain"))
    assert entries[0].path.resolve() == primary.resolve()
    assert entries[0].branch == "refs/heads/main"
    found = _by_path(entries)
    assert len(found) == 6
    linked = found[(tmp / "linked tree").resolve()]
    assert linked.path.name == "linked tree"
    assert (linked.head, linked.branch, linked.detached) == (head, "refs/heads/feature", False)
    assert (linked.locked, linked.prunable) == (None, None)
    detached = found[(tmp / "detached").resolve()]
    assert (detached.head, detached.branch, detached.detached) == (head, None, True)
    assert found[(tmp / "lk").resolve()].locked == "on usb"
    assert found[(tmp / "lk2").resolve()].locked == ""
    gone = found[(tmp / "gone").resolve()]
    assert gone.prunable  # Git explains why the registration is prunable
    assert gone.locked is None
    assert all(not entry.bare for entry in entries)


def test_real_listing_reports_bare_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")

    (entry,) = list_worktrees(bare)

    assert entry.path.resolve() == bare.resolve()
    assert entry.bare is True
    assert (entry.head, entry.branch, entry.detached) == (None, None, False)


def test_list_worktrees_propagates_git_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        list_worktrees(tmp_path)


def test_empty_listing_has_no_records():
    assert parse_worktree_porcelain("") == ()
    assert parse_worktree_porcelain("\n\n") == ()


def test_missing_final_blank_line_and_crlf_are_accepted():
    text = f"worktree /a\r\nHEAD {SHA}\r\nbranch refs/heads/main\r\n\r\nworktree /b\r\nbare"

    assert parse_worktree_porcelain(text) == (
        WorktreeEntry(Path("/a"), head=SHA, branch="refs/heads/main"),
        WorktreeEntry(Path("/b"), bare=True),
    )


def test_paths_are_verbatim_including_spaces_non_ascii_and_c_quotes():
    text = (
        "worktree /x/trailing  \n\n"
        "worktree /x/ré po\n\n"
        'worktree "/x/quo\\"ted\\n"\nlocked "reason\\nwith newline"\n'
    )

    paths = [entry.path for entry in parse_worktree_porcelain(text)]

    assert paths == [Path("/x/trailing  "), Path("/x/ré po"), Path('"/x/quo\\"ted\\n"')]
    assert parse_worktree_porcelain(text)[2].locked == '"reason\\nwith newline"'


def test_record_boundaries_and_stray_lines():
    text = (
        "branch refs/heads/orphan\n"  # attribute before any record: ignored
        "worktree /a\nbranch refs/heads/one\nfuture-attribute x\n"
        "worktree /b\n"  # a new record without a separating blank line
        "branch refs/heads/two\nbranch refs/heads/three\n\n"  # repeated: last wins
        "branch refs/heads/after-blank\n"  # attribute outside a record: ignored
        "prunable\n"
    )

    assert parse_worktree_porcelain(text) == (
        WorktreeEntry(Path("/a"), branch="refs/heads/one"),
        WorktreeEntry(Path("/b"), branch="refs/heads/three"),
    )


def test_bare_keywords_need_exact_lines():
    text = "worktree /a\nHEAD\nbranch\ndetached now\nbare repo\nprunable\n"

    assert parse_worktree_porcelain(text) == (WorktreeEntry(Path("/a"), prunable=""),)
