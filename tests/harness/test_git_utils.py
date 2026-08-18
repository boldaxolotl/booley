"""Tests for git_utils.py — focused on untested functions and real-git integration.

Existing test_utils.py covers the re-exported scope helpers (expand_scope_globs,
scope_matches_file, is_scope_unknown, commit_scope with mocks).  This file adds:
  - git_run basics
  - commit_scope integration with a real tmp git repo (main-worktree guard,
    nothing-to-commit, git-add failure, auto-fix message body)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.harness.blocking import BlockingError
from booley.harness.git_utils import (
    BOOLEY_EXCLUDE_HEADER,
    _git_common_dir,
    _has_glob_chars,
    add_git_excludes,
    commit_scope,
    git_run,
)

# ---------------------------------------------------------------------------
# Helpers — tiny git repo factory
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> Path:
    """Create a bare-minimum git repo with one commit. Returns repo root."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True, check=True
    )
    # Initial commit so HEAD exists
    (path / "init.txt").write_text("init", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "init.txt"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True
    )
    return path


def _make_worktree(repo: Path, wt_path: Path, branch: str = "test-branch") -> Path:
    """Create a linked worktree (has .git file, not .git directory)."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(wt_path), "-b", branch],
        capture_output=True,
        check=True,
    )
    return wt_path


# ===========================================================================
# _has_glob_chars
# ===========================================================================


class TestHasGlobChars:
    def test_plain_path(self):
        assert not _has_glob_chars("rtl/foo.sv")

    def test_star(self):
        assert _has_glob_chars("rtl/*.sv")

    def test_question_mark(self):
        assert _has_glob_chars("rtl/foo?.sv")

    def test_bracket(self):
        assert _has_glob_chars("rtl/foo[12].sv")


# ===========================================================================
# git_run
# ===========================================================================


class TestGitRun:
    def test_returns_completed_process(self, tmp_path: Path):
        _init_repo(tmp_path)
        result = git_run(tmp_path, ["rev-parse", "--is-inside-work-tree"])
        assert result.returncode == 0
        assert result.stdout.strip() == "true"

    def test_bad_command_nonzero(self, tmp_path: Path):
        _init_repo(tmp_path)
        result = git_run(tmp_path, ["log", "--oneline", "nonexistent-ref-999"])
        assert result.returncode != 0


# ===========================================================================
# commit_scope — integration with real git repos
# ===========================================================================


class TestCommitScopeIntegration:
    """Tests that need a real git repo (main worktree guard, nothing-to-commit, etc.)."""

    def test_main_worktree_guard_blocks(self, tmp_path: Path):
        """commit_scope refuses when .git is a directory (main repo)."""
        repo = _init_repo(tmp_path)
        (repo / "new.sv").write_text("wire x;\n", encoding="utf-8")
        # .git is a directory → main worktree → should silently refuse
        commit_scope(repo, ["new.sv"], "should not commit")
        # File should NOT be committed
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "should not commit" not in result.stdout

    def test_worktree_allows_commit(self, tmp_path: Path):
        """commit_scope succeeds in a linked worktree (.git is a file)."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        (wt / "new.sv").write_text("wire x;\n", encoding="utf-8")
        commit_scope(wt, ["new.sv"], "feat: add wire")
        result = subprocess.run(
            ["git", "-C", str(wt), "log", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert "feat: add wire" in result.stdout

    def test_literal_scope_with_pathspec_metacharacters(self, tmp_path: Path):
        """Literal staging accepts real filenames that contain glob syntax."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        literal_path = "rtl/mem[0].sv"
        (wt / "rtl").mkdir()
        (wt / literal_path).write_text("wire x;\n", encoding="utf-8")

        commit_scope(wt, [literal_path], "feat: add literal path", literal=True)

        result = subprocess.run(
            ["git", "-C", str(wt), "show", "--format=", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert literal_path in result.stdout.splitlines()

    def test_pre_staged_outsider_is_left_uncommitted(self, tmp_path: Path):
        """Authorized work commits even when an unrelated path was staged."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        (wt / "authorized.sv").write_text("wire allowed;\n", encoding="utf-8")
        (wt / "outside.sv").write_text("wire needs_triage;\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(wt), "add", "outside.sv"],
            capture_output=True,
            check=True,
        )

        commit_scope(wt, ["authorized.sv"], "fix: preserve scoped work")

        committed = subprocess.run(
            ["git", "-C", str(wt), "show", "--format=", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        status = subprocess.run(
            ["git", "-C", str(wt), "status", "--short"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert committed.stdout.splitlines() == ["authorized.sv"]
        assert status.stdout.splitlines() == ["?? outside.sv"]

    def test_unmatched_glob_scope_is_ignored(self, tmp_path: Path):
        """Missing glob matches do not make git add fail."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        (wt / "rtl").mkdir()
        (wt / "rtl" / "new.sv").write_text("wire x;\n", encoding="utf-8")

        commit_scope(
            wt,
            ["rtl/*.sv", "verif/lane2/*.sv"],
            "feat: add scoped rtl",
        )

        result = subprocess.run(
            ["git", "-C", str(wt), "log", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert "feat: add scoped rtl" in result.stdout

    def test_nothing_to_commit(self, tmp_path: Path):
        """No changes → commit_scope returns quietly (no exception)."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        # No file changes — "nothing to commit" path
        commit_scope(wt, ["init.txt"], "feat: noop")

    def test_git_add_failure_raises_blocking(self, tmp_path: Path):
        """git add on nonexistent file → BlockingError."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        with pytest.raises(BlockingError, match="git add failed"):
            commit_scope(wt, ["this_file_does_not_exist.sv"], "feat: ghost")

    def test_empty_scope_skips(self, tmp_path: Path):
        """Empty scope list → no git operations at all."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        # Should return silently
        commit_scope(wt, [], "wip")

    def test_unknown_scope_uses_add_dot(self, tmp_path: Path):
        """Wildcard scope ['*'] stages tracked modifications via 'git add -u'."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        # Modify a tracked file (git add -u only stages tracked changes)
        (wt / "init.txt").write_text("modified content\n", encoding="utf-8")
        commit_scope(wt, ["*"], "fix: wildcard commit")
        result = subprocess.run(
            ["git", "-C", str(wt), "log", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert "fix: wildcard commit" in result.stdout

    def test_stale_index_lock_is_removed_and_retried(self, tmp_path: Path):
        """A stale linked-worktree index.lock should not block commit_scope."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        (wt / "new.sv").write_text("wire x;\n", encoding="utf-8")
        git_dir = subprocess.check_output(
            ["git", "-C", str(wt), "rev-parse", "--git-dir"],
            text=True,
        ).strip()
        git_dir_path = Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = wt / git_dir_path
        lock_path = git_dir_path / "index.lock"
        lock_path.write_text("", encoding="utf-8")

        commit_scope(wt, ["new.sv"], "feat: recover stale lock")

        assert not lock_path.exists()
        result = subprocess.run(
            ["git", "-C", str(wt), "log", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert "feat: recover stale lock" in result.stdout

    def test_active_git_owner_keeps_index_lock(self, tmp_path: Path):
        """A lock with a matching live git process is left alone."""
        repo = _init_repo(tmp_path)
        wt = _make_worktree(repo, tmp_path / "wt")
        (wt / "new.sv").write_text("wire x;\n", encoding="utf-8")
        git_dir = subprocess.check_output(
            ["git", "-C", str(wt), "rev-parse", "--git-dir"],
            text=True,
        ).strip()
        git_dir_path = Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = wt / git_dir_path
        lock_path = git_dir_path / "index.lock"
        lock_path.write_text("", encoding="utf-8")

        with (
            patch("booley.harness.git_utils._git_process_owns_worktree", return_value=True),
            pytest.raises(BlockingError, match="git add failed"),
        ):
            commit_scope(wt, ["new.sv"], "feat: keep busy lock")

        assert lock_path.exists()


# ===========================================================================
# add_git_excludes — worktree-aware info/exclude (ADR 0018 WS0)
# ===========================================================================


def _is_ignored(wt: Path, rel: str) -> bool:
    """True if git treats *rel* under *wt* as ignored/excluded."""
    result = subprocess.run(
        ["git", "-C", str(wt), "check-ignore", "-q", rel],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


class TestAddGitExcludes:
    def test_main_worktree_excludes_and_is_honored(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")

        modified = add_git_excludes(repo, [".devcontainer", ".booley_project"])

        assert modified is True
        exclude = repo / ".git" / "info" / "exclude"
        body = exclude.read_text(encoding="utf-8")
        assert BOOLEY_EXCLUDE_HEADER in body
        assert "/.devcontainer" in body and "/.booley_project" in body
        # The entries must actually be honored by git.
        assert _is_ignored(repo, ".devcontainer/devcontainer.json")
        assert _is_ignored(repo, ".booley_project/booley.toml")

    def test_idempotent(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")

        assert add_git_excludes(repo, [".devcontainer"]) is True
        # Second call adds nothing new and reports no modification.
        assert add_git_excludes(repo, [".devcontainer"]) is False
        body = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert body.count("/.devcontainer") == 1

    def test_linked_worktree_writes_to_common_dir_and_is_honored(self, tmp_path):
        """The crux: a linked worktree honors the COMMON-dir info/exclude only.

        Git treats ``info/`` as a shared path, so the per-worktree
        ``.git/worktrees/<id>/info/exclude`` is ignored. add_git_excludes must
        target ``$GIT_COMMON_DIR/info/exclude`` for the exclude to take effect.
        """
        repo = _init_repo(tmp_path / "repo")
        wt = _make_worktree(repo, tmp_path / "wt")

        modified = add_git_excludes(wt, [".devcontainer"])

        assert modified is True
        # Written to the shared dir, not the per-worktree info dir.
        common_exclude = repo / ".git" / "info" / "exclude"
        assert "/.devcontainer" in common_exclude.read_text(encoding="utf-8")
        per_wt_exclude = _git_common_dir(wt)  # resolves to repo/.git, not worktrees/<id>
        assert per_wt_exclude == (repo / ".git").resolve()
        # And it is actually honored from inside the linked worktree.
        assert _is_ignored(wt, ".devcontainer/devcontainer.json")

    def test_non_git_dir_is_noop(self, tmp_path):
        # Best-effort: no .git, no crash, reports no modification.
        assert add_git_excludes(tmp_path, [".devcontainer"]) is False
