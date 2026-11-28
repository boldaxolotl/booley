"""Tests for Scope expansion and matching (``booley.runtime.git``)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.core.scope_matching import scope_matches_file
from booley.runtime.agent_errors import BlockingError
from booley.runtime.git import commit_scope, expand_scope_globs, scope_matches_dirty_file


@pytest.mark.parametrize("status", ["??", "A ", " M", "M ", "AM", " D", "D ", "MD"])
def test_new_scope_deletion_policy_remains_status_aware(status: str) -> None:
    scope = ["rtl/*.sv [new]"]
    expected = "D" not in status[:2]
    assert scope_matches_dirty_file(scope, "rtl/top.sv", status) is expected
    assert scope_matches_dirty_file(["* [new]"], "rtl/top.sv", status)
    assert scope_matches_dirty_file([*scope, "rtl/top.sv"], "rtl/top.sv", status)
    assert not scope_matches_dirty_file([], "rtl/top.sv", status)


class TestCommitScope:
    # A real index cannot make ``git reset`` fail after ``git add`` succeeded, so this
    # bug-fix test (84d502d8e) keeps its stand-in for the Git command.
    @patch("booley.runtime.platform_paths.run_captured_bounded")
    def test_out_of_scope_unstage_failure_blocks(self, mock_run):
        """Do not commit a mixed index if the outsider cannot be unstaged."""

        def side_effect(args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if cmd == "diff":
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="rtl/foo.sv\0rtl/rogue_file.sv\0", stderr=""
                )
            if "reset" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr="index is locked"
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = side_effect
        with pytest.raises(BlockingError, match="Could not unstage out-of-scope"):
            commit_scope(Path("/wt"), ["rtl/foo.sv"], "fix: stuff")


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (["rtl/foo.sv", "rtl/missing.sv"], ["rtl/foo.sv", "rtl/missing.sv"]),
        (["rtl/*.sv"], ["rtl/bar.sv", "rtl/foo.sv"]),
        (["verif/*.sv"], []),
        (["tb/tb_top.sv", "rtl/*.sv"], ["tb/tb_top.sv", "rtl/bar.sv", "rtl/foo.sv"]),
        (["rtl/foo.sv", "rtl/*.sv"], ["rtl/foo.sv", "rtl/bar.sv"]),
        (["rtl/*"], ["rtl/bar.sv", "rtl/foo.sv", "rtl/readme.txt"]),
        (["rtl/*.sv [new]"], ["rtl/bar.sv", "rtl/foo.sv"]),
        (["*"], ["*"]),
    ],
    ids=["literal", "glob", "no-match", "mixed", "dedupe", "files-only", "new-tag", "unknown"],
)
def test_expand_scope_globs(tmp_path: Path, scope: list[str], expected: list[str]) -> None:
    for name in ("rtl/foo.sv", "rtl/bar.sv", "rtl/readme.txt", "tb/tb_top.sv"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).touch()
    (tmp_path / "rtl" / "sub").mkdir()
    assert expand_scope_globs(tmp_path, scope) == expected


def test_scope_matches_file_checks_every_entry() -> None:
    scope = ["rtl/my_module.sv", "tb/*.sv"]
    assert scope_matches_file(scope, "rtl/my_module.sv")
    assert scope_matches_file(scope, "tb/tb_top.sv")
    assert not scope_matches_file(scope, "rtl/other_module.sv")
