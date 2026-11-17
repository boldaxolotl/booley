"""``commit_scope`` in a real linked worktree: stale-lock recovery and the Stealth message policy."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.git_support import git, repository

from booley.runtime.agent_errors import BlockingError
from booley.runtime.git import commit_scope

_EXPLICIT = 'banned_words = ["quokka"]\nbanned_substrings = ["zorb"]\n'


def _worktree(tmp_path: Path, stealth: str) -> Path:
    """A linked worktree of a Stealth Project whose policy is *stealth*, with one new file."""
    repo = repository(tmp_path / "repo")
    (repo / "init.txt").write_bytes(b"init\n")
    git(repo, "add", "init.txt")
    git(repo, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt))
    (wt / ".booley").mkdir()
    (wt / ".booley" / "booley.toml").write_bytes(f"[stealth]\nenabled = true\n{stealth}".encode())
    (wt / "rtl.sv").write_bytes(b"module rtl; endmodule\n")
    return wt


def test_commit_scope_removes_a_stale_index_lock_and_commits(tmp_path: Path) -> None:
    wt = _worktree(tmp_path, _EXPLICIT)
    lock = Path(git(wt, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_bytes(b"")

    commit_scope(wt, ["rtl.sv"], "feat(rtl): recover stale lock\n\nA body line is allowed.")

    assert not lock.exists()
    assert git(wt, "log", "-1", "--format=%B").startswith("feat(rtl): recover stale lock")


@pytest.mark.parametrize(
    ("stealth", "message"),
    [
        ("max_body_lines = 0\n", "feat: add rtl\n\nbody line"),
        ("", "feat: add rtl, as copilot suggested"),
        (_EXPLICIT, "feat: add the quokka"),
        (_EXPLICIT, "feat: add zorbTimer"),
    ],
    ids=["body-cap", "default-word", "explicit-word", "substring-in-identifier"],
)
def test_commit_scope_rejects_a_message_the_stealth_policy_bans(
    tmp_path: Path, stealth: str, message: str
) -> None:
    wt = _worktree(tmp_path, stealth)
    head = git(wt, "rev-parse", "HEAD")

    with pytest.raises(BlockingError, match="Commit message validation failed"):
        commit_scope(wt, ["rtl.sv"], message)

    assert git(wt, "rev-parse", "HEAD") == head


def test_a_stealth_project_lists_the_banned_phrases_it_enforces(tmp_path: Path) -> None:
    from booley.commit_policy import banned_phrases

    wt = _worktree(tmp_path, _EXPLICIT)

    assert banned_phrases(wt) == ["quokka", "zorb"]  # words, then substrings
