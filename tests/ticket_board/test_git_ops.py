"""Tests for ticket_board.git_ops: git wrapper functions (all mocked)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from booley.ticket_board.git_ops import (
    add_worktree,
    cleanup_worktree_and_branch,
    delete_branch,
    find_worktree_for_branch,
    git,
    git_ok,
    merge_branch,
    remove_worktree,
    remove_worktree_for_branch,
)

# ---------------------------------------------------------------------------
# git() / git_ok() low-level wrappers
# ---------------------------------------------------------------------------


class TestGitWrapper:
    @patch("subprocess.run")
    def test_git_returns_completed_process(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        result = git("status")
        assert result.returncode == 0
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_git_returns_none_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        assert git("status") is None

    @patch("subprocess.run")
    def test_git_returns_none_on_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        assert git("status") is None

    @patch("subprocess.run")
    def test_git_ok_returns_stdout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="  main  \n")
        assert git_ok("branch", "--show-current") == "main"

    @patch("subprocess.run")
    def test_git_ok_returns_none_on_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert git_ok("branch", "--show-current") is None


# ---------------------------------------------------------------------------
# find_worktree_for_branch
# ---------------------------------------------------------------------------


class TestFindWorktreeForBranch:
    @patch("booley.ticket_board.git_ops.get_main_worktree", return_value="/repo")
    @patch("booley.ticket_board.git_ops.git")
    def test_finds_matching_branch(self, mock_git, mock_main):
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout=(
                "worktree /repo\n"
                "branch refs/heads/main\n"
                "\n"
                "worktree /repo/worktrees/fix-bug\n"
                "branch refs/heads/fix-bug\n"
            ),
        )
        result = find_worktree_for_branch("fix-bug")
        assert result == "/repo/worktrees/fix-bug"

    @patch("booley.ticket_board.git_ops.get_main_worktree", return_value="/repo")
    @patch("booley.ticket_board.git_ops.git")
    def test_skips_main_worktree(self, mock_git, mock_main):
        """Even if main worktree has the branch, it should not be returned."""
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="worktree /repo\nbranch refs/heads/main\n",
        )
        result = find_worktree_for_branch("main")
        assert result is None

    @patch("booley.ticket_board.git_ops.git")
    def test_returns_none_on_git_failure(self, mock_git):
        mock_git.return_value = None
        assert find_worktree_for_branch("any") is None


# ---------------------------------------------------------------------------
# remove_worktree
# ---------------------------------------------------------------------------


class TestRemoveWorktree:
    @patch("booley.ticket_board.git_ops.safe_rmtree")
    @patch("booley.ticket_board.git_ops.git")
    def test_remove_calls_git_and_rmtree(self, mock_git, mock_rmtree, tmp_path):
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        (wt_dir / "file.txt").write_text("content")
        mock_git.return_value = MagicMock(returncode=0)

        remove_worktree(str(wt_dir))
        mock_git.assert_called_once()
        # rmtree called since dir still exists
        mock_rmtree.assert_called_once()

    @patch("booley.ticket_board.git_ops.safe_rmtree")
    @patch("booley.ticket_board.git_ops.git")
    def test_refuses_rmtree_on_git_root(self, mock_git, mock_rmtree, tmp_path):
        """Should skip rmtree if dir has .git/HEAD."""
        wt_dir = tmp_path / "real_repo"
        wt_dir.mkdir()
        git_dir = wt_dir / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main")
        mock_git.return_value = MagicMock(returncode=0)

        remove_worktree(str(wt_dir))
        mock_rmtree.assert_not_called()


# ---------------------------------------------------------------------------
# add_worktree
# ---------------------------------------------------------------------------


class TestAddWorktree:
    @patch("booley.ticket_board.git_ops.git")
    def test_success(self, mock_git):
        mock_git.return_value = MagicMock(returncode=0, stderr="")
        ok, err = add_worktree("/path/to/wt", "feature-branch")
        assert ok is True
        assert err == ""

    @patch("booley.ticket_board.git_ops.git")
    def test_failure(self, mock_git):
        mock_git.return_value = MagicMock(returncode=128, stderr="fatal: error")
        ok, err = add_worktree("/path/to/wt", "feature-branch")
        assert ok is False
        assert "fatal" in err

    @patch("booley.ticket_board.git_ops.git")
    def test_timeout(self, mock_git):
        mock_git.return_value = None  # timeout
        ok, _err = add_worktree("/path/to/wt", "feature-branch")
        assert ok is False


# ---------------------------------------------------------------------------
# delete_branch
# ---------------------------------------------------------------------------


class TestDeleteBranch:
    @patch("booley.ticket_board.git_ops.git")
    def test_successful_delete(self, mock_git, capsys):
        mock_git.return_value = MagicMock(returncode=0)
        assert delete_branch("old-feature") is True
        out = capsys.readouterr().out
        assert "Deleted branch" in out

    @patch("booley.ticket_board.git_ops.git")
    def test_unmerged_warning(self, mock_git, capsys):
        mock_git.return_value = MagicMock(
            returncode=1, stderr="error: branch 'x' is not fully merged"
        )
        assert delete_branch("unmerged") is False
        err = capsys.readouterr().err
        assert "unmerged commits" in err

    @patch("booley.ticket_board.git_ops.git")
    def test_timeout_noop(self, mock_git):
        mock_git.return_value = None
        assert delete_branch("timeout-branch") is False


# ---------------------------------------------------------------------------
# merge_branch
# ---------------------------------------------------------------------------


class TestMergeBranch:
    @patch("subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        ok, _err = merge_branch("feature", "merge commit", "/repo")
        assert ok is True

    @patch("subprocess.run")
    def test_conflict(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="CONFLICT")
        ok, err = merge_branch("feature", "merge commit", "/repo")
        assert ok is False
        assert "CONFLICT" in err

    @patch("subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=60)
        ok, _err = merge_branch("feature", "msg", "/repo")
        assert ok is False


# ---------------------------------------------------------------------------
# Composite helpers
# ---------------------------------------------------------------------------


class TestCompositeHelpers:
    @patch("booley.ticket_board.git_ops.branch_exists", return_value=True)
    @patch("booley.ticket_board.git_ops.delete_branch")
    @patch("booley.ticket_board.git_ops.remove_worktree_for_branch")
    def test_cleanup_worktree_and_branch(self, mock_remove, mock_delete, _mock_exists):
        mock_delete.return_value = True

        assert cleanup_worktree_and_branch("feature-x", force=True) is True

        mock_remove.assert_called_once_with("feature-x")
        mock_delete.assert_called_once_with("feature-x", force=True)

    @patch("booley.ticket_board.git_ops.branch_exists", return_value=False)
    @patch("booley.ticket_board.git_ops.delete_branch")
    @patch("booley.ticket_board.git_ops.remove_worktree_for_branch")
    def test_cleanup_succeeds_when_branch_is_already_absent(
        self, mock_remove, mock_delete, _mock_exists
    ):
        assert cleanup_worktree_and_branch("feature-x", force=True) is True
        mock_remove.assert_called_once_with("feature-x")
        mock_delete.assert_not_called()

    @patch("booley.ticket_board.git_ops.remove_worktree")
    @patch(
        "booley.ticket_board.git_ops.find_worktree_for_branch", return_value="/repo/worktrees/feat"
    )
    def test_remove_worktree_for_branch(self, mock_find, mock_remove, capsys):
        remove_worktree_for_branch("feat")
        mock_remove.assert_called_once_with("/repo/worktrees/feat")

    @patch("booley.ticket_board.git_ops.find_worktree_for_branch", return_value=None)
    def test_remove_worktree_for_branch_noop(self, mock_find):
        remove_worktree_for_branch("no-such")  # should not raise
