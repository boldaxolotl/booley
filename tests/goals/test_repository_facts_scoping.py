"""Where the repository-fact memo must not reach, and a prefetch edge (#2160)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from booley.goals import entry, finish
from booley.goals.protected_inputs import _disk_tree, _HeadView
from tests.goals.conftest import git


@pytest.mark.parametrize(
    "recheck",
    [
        finish.finish_goal,
        finish._freeze,
        finish._finish_terminal,
        entry._drive_entry,
        entry.rollback_entering,
    ],
)
def test_deliberate_rechecks_never_run_inside_a_fact_scope(recheck) -> None:
    """Goal Mode re-reads repository facts after side effects; a scope here would hide a swap."""
    assert not hasattr(recheck, "__wrapped__"), recheck.__qualname__


@pytest.mark.skipif(sys.platform == "win32", reason="':' is not valid in a Windows file name")
def test_head_view_prefetch_leaves_pathspec_magic_names_to_their_own_listing(
    goal_mode: SimpleNamespace,
) -> None:
    worktree = goal_mode.worktree
    (worktree / "odd").write_text("plain\n", encoding="utf-8")
    (worktree / ":odd").write_text("magic\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "magic names")
    locations = [worktree / ":odd", worktree / "odd"]

    def trees(prefetch: bool) -> list[dict]:
        view = _HeadView(include_mode=True)
        if prefetch:
            view.prefetch(locations)
        return [view.committed_tree(path, _disk_tree(path), frozenset()) for path in locations]

    assert trees(prefetch=True) == trees(prefetch=False)
