"""Tests for shared utilities (utils.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.harness.blocking import BlockingError
from booley.harness.git_utils import (
    commit_scope,
    expand_scope_globs,
    is_scope_unknown,
    scope_matches_dirty_file,
    scope_matches_file,
)

# ===========================================================================
# commit_scope
# ===========================================================================


class TestCommitScope:
    @patch("booley.harness.git_utils.subprocess.run")
    def test_with_explicit_scope(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        commit_scope(Path("/wt"), ["rtl/foo.sv", "tb/bar.sv"], "fix: stuff")
        # Three calls: git add <files>, git diff --cached (scope check), git commit
        assert mock_run.call_count == 3
        add_args = mock_run.call_args_list[0][0][0]
        assert "rtl/foo.sv" in add_args
        assert "tb/bar.sv" in add_args

    @patch("booley.harness.git_utils.subprocess.run")
    def test_empty_scope_skips_commit(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        commit_scope(Path("/wt"), [], "wip commit")
        # No subprocess calls -- empty scope detected before git operations
        assert mock_run.call_count == 0

    @patch("booley.harness.git_utils.subprocess.run")
    def test_commit_scope_add_and_commit(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        commit_scope(
            Path("/wt"),
            ["rtl/foo.sv"],
            "feat: stuff",
        )
        # add + diff --cached (scope check) + commit == 3 subprocess calls
        assert mock_run.call_count == 3

    @patch("booley.harness.git_utils.subprocess.run")
    def test_out_of_scope_staged_files_are_unstaged(self, mock_run):
        """A raw staged outsider is preserved but excluded from the commit."""

        def side_effect(args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if cmd == "add":
                return subprocess.CompletedProcess(args=args, returncode=0)
            if cmd == "diff":
                # Simulate out-of-scope file already staged
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="rtl/foo.sv\0rtl/rogue_file.sv\0",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = side_effect
        commit_scope(Path("/wt"), ["rtl/foo.sv"], "fix: stuff")

        reset_args = mock_run.call_args_list[2][0][0]
        assert reset_args[1:7] == [
            "--literal-pathspecs",
            "reset",
            "--quiet",
            "HEAD",
            "--",
            "rtl/rogue_file.sv",
        ]
        assert mock_run.call_args_list[-1][0][0][1:] == ["commit", "-m", "fix: stuff"]

    @patch("booley.harness.git_utils.subprocess.run")
    def test_out_of_scope_staged_deletion_is_unstaged(self, mock_run):
        """An outside deletion is preserved in the worktree, not committed."""

        def side_effect(args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if cmd == "diff":
                assert "--diff-filter=ACMRD" in args
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="rtl/foo.sv\0README.md\0",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = side_effect
        commit_scope(Path("/wt"), ["rtl/foo.sv"], "fix: stuff")

        reset_args = mock_run.call_args_list[2][0][0]
        assert reset_args[-1] == "README.md"

    @patch("booley.harness.git_utils.subprocess.run")
    def test_out_of_scope_unstage_failure_blocks(self, mock_run):
        """Do not commit a mixed index if the outsider cannot be unstaged."""

        def side_effect(args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if cmd == "diff":
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="rtl/foo.sv\0rtl/rogue_file.sv\0",
                    stderr="",
                )
            if "reset" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr="index is locked",
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = side_effect
        with pytest.raises(BlockingError, match="Could not unstage out-of-scope"):
            commit_scope(Path("/wt"), ["rtl/foo.sv"], "fix: stuff")

    @patch("booley.harness.git_utils.subprocess.run")
    def test_in_scope_staged_files_pass(self, mock_run):
        """All staged files within scope → commit proceeds normally."""

        def side_effect(args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if cmd == "diff":
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="rtl/foo.sv\0",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = side_effect
        # Should not raise
        commit_scope(Path("/wt"), ["rtl/foo.sv"], "fix: stuff")

    @patch("booley.harness.git_utils.subprocess.run")
    def test_unknown_scope_skips_staged_check(self, mock_run):
        """Wildcard scope skips the out-of-scope check."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        commit_scope(Path("/wt"), ["*"], "fix: stuff")
        # git config (submodule discovery) + git add --all + git commit = 3 calls
        assert mock_run.call_count == 3
        add_args = mock_run.call_args_list[1][0][0]
        assert "--all" in add_args


# ===========================================================================
# expand_scope_globs / scope_matches_file
# ===========================================================================


class TestExpandScopeGlobs:
    def test_literal_paths_pass_through(self, tmp_path: Path):
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "foo.sv").touch()
        result = expand_scope_globs(tmp_path, ["rtl/foo.sv"])
        assert result == ["rtl/foo.sv"]

    def test_glob_expands_to_matching_files(self, tmp_path: Path):
        rtl = tmp_path / "rtl"
        rtl.mkdir()
        (rtl / "foo.sv").touch()
        (rtl / "bar.sv").touch()
        (rtl / "readme.txt").touch()
        result = expand_scope_globs(tmp_path, ["rtl/*.sv"])
        assert sorted(result) == ["rtl/bar.sv", "rtl/foo.sv"]

    def test_glob_no_match_is_ignored(self, tmp_path: Path):
        result = expand_scope_globs(tmp_path, ["rtl/*.sv"])
        assert result == []

    def test_mixed_literal_and_glob(self, tmp_path: Path):
        rtl = tmp_path / "rtl"
        rtl.mkdir()
        (rtl / "a.sv").touch()
        (rtl / "b.sv").touch()
        tb = tmp_path / "tb"
        tb.mkdir()
        (tb / "tb_top.sv").touch()
        result = expand_scope_globs(tmp_path, ["tb/tb_top.sv", "rtl/*.sv"])
        assert result[0] == "tb/tb_top.sv"
        assert "rtl/a.sv" in result
        assert "rtl/b.sv" in result

    def test_deduplicates(self, tmp_path: Path):
        rtl = tmp_path / "rtl"
        rtl.mkdir()
        (rtl / "foo.sv").touch()
        result = expand_scope_globs(tmp_path, ["rtl/foo.sv", "rtl/*.sv"])
        assert result == ["rtl/foo.sv"]

    def test_excludes_directories(self, tmp_path: Path):
        rtl = tmp_path / "rtl"
        rtl.mkdir()
        (rtl / "sub").mkdir()
        (rtl / "foo.sv").touch()
        result = expand_scope_globs(tmp_path, ["rtl/*"])
        assert "rtl/foo.sv" in result
        assert not any("sub" in r for r in result)


class TestScopeMatchesFile:
    def test_literal_match(self):
        assert scope_matches_file(["rtl/foo.sv"], "rtl/foo.sv")

    def test_literal_no_match(self):
        assert not scope_matches_file(["rtl/foo.sv"], "rtl/bar.sv")

    def test_glob_match(self):
        assert scope_matches_file(["rtl/*.sv"], "rtl/my_enc_dec.sv")

    def test_glob_no_match_wrong_ext(self):
        assert not scope_matches_file(["rtl/*.sv"], "rtl/readme.txt")

    def test_glob_no_match_wrong_dir(self):
        assert not scope_matches_file(["rtl/*.sv"], "tb/test.sv")

    def test_mixed_scope(self):
        scope = ["rtl/my_module.sv", "tb/*.sv"]
        assert scope_matches_file(scope, "rtl/my_module.sv")
        assert scope_matches_file(scope, "tb/tb_top.sv")
        assert not scope_matches_file(scope, "rtl/other_module.sv")

    def test_unknown_scope_matches_everything(self):
        assert scope_matches_file(["*"], "rtl/foo.sv")
        assert scope_matches_file(["*"], "tb/bar_tb.sv")
        assert scope_matches_file(["*"], "any/path/at/all.txt")

    def test_new_scope_glob_does_not_own_deletions(self):
        scope = ["verif/lane1/*.sv [new]"]
        assert scope_matches_dirty_file(scope, "verif/lane1/new_tb.sv", "??")
        assert scope_matches_dirty_file(scope, "verif/lane1/new_tb.sv", " M")
        assert not scope_matches_dirty_file(scope, "verif/lane1/old_tb.sv", " D")

    def test_normal_scope_glob_owns_deletions(self):
        assert scope_matches_dirty_file(["verif/lane1/*.sv"], "verif/lane1/old_tb.sv", " D")


class TestIsScopeUnknown:
    def test_wildcard_sentinel(self):
        assert is_scope_unknown(["*"])

    def test_normal_scope_not_unknown(self):
        assert not is_scope_unknown(["rtl/foo.sv"])
        assert not is_scope_unknown(["rtl/*.sv"])

    def test_empty_scope_not_unknown(self):
        assert not is_scope_unknown([])

    def test_wildcard_plus_other_entries_not_unknown(self):
        assert not is_scope_unknown(["*", "rtl/foo.sv"])


class TestExpandScopeGlobsUnknown:
    def test_unknown_scope_passes_through(self, tmp_path: Path):
        result = expand_scope_globs(tmp_path, ["*"])
        assert result == ["*"]


# ===========================================================================
# scope_precommit_hook
# ===========================================================================


class TestScopePrecommitHook:
    def _import_hook(self):
        """Import the hook script as a module."""
        import importlib.util

        hook_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "booley"
            / "dev_support"
            / "scope_precommit_hook.py"
        )
        spec = importlib.util.spec_from_file_location("scope_precommit_hook", hook_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_no_scope_file_allows_commit(self, tmp_path, monkeypatch):
        hook = self._import_hook()
        monkeypatch.chdir(tmp_path)
        assert hook.main() == 0

    @patch("subprocess.run")
    def test_wildcard_scope_blocks_commit(self, mock_run, tmp_path, monkeypatch):
        hook = self._import_hook()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".scope.json").write_text('{"scope": ["*"]}')
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="rtl/foo.sv\0",
            stderr="",
        )
        assert hook.main() == 1

    @patch("subprocess.run")
    def test_in_scope_files_pass(self, mock_run, tmp_path, monkeypatch):
        hook = self._import_hook()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".scope.json").write_text('{"scope": ["rtl/foo.sv"]}')
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="rtl/foo.sv\0",
            stderr="",
        )
        assert hook.main() == 0

    @patch("subprocess.run")
    def test_out_of_scope_files_block_commit(self, mock_run, tmp_path, monkeypatch, capsys):
        """Scope is a hard commit boundary."""
        hook = self._import_hook()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".scope.json").write_text('{"scope": ["rtl/foo.sv"]}')
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="rtl/foo.sv\0rtl/rogue.sv\0",
            stderr="",
        )
        assert hook.main() == 1
        err = capsys.readouterr().err
        assert "rtl/rogue.sv" in err
        assert "rtl/foo.sv" not in err.split("Outside it:")[1]

    @patch("subprocess.run")
    def test_harness_owned_files_rejected(self, mock_run, tmp_path, monkeypatch):
        hook = self._import_hook()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".scope.json").write_text('{"scope": ["rtl/foo.sv"]}')
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="rtl/foo.sv\0.booley_project/booley.toml\0",
            stderr="",
        )
        assert hook.main() == 1

    @patch("subprocess.run")
    def test_harness_owned_check_survives_a_missing_scope_file(
        self, mock_run, tmp_path, monkeypatch
    ):
        """The forbidden tier is scope-independent: no .scope.json, still rejected."""
        hook = self._import_hook()
        monkeypatch.chdir(tmp_path)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=".scope.json\0",
            stderr="",
        )
        assert hook.main() == 1

    @patch("subprocess.run")
    def test_stealth_cores_are_not_harness_owned(self, mock_run, tmp_path, monkeypatch):
        hook = self._import_hook()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".scope.json").write_text('{"scope": [".booley_project/cores/dut.core"]}')
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=".booley_project/cores/dut.core\0",
            stderr="",
        )
        assert hook.main() == 0
