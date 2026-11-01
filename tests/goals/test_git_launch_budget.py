"""Goal Mode asks Git each repository fact once per read-only computation (#2160).

The budget tests count real ``git`` launches of one read-only call on a real
repository; the guard tests pin that the saving never hides a change: HEAD is
read exactly as before, and a re-check outside the scope still asks Git again.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from booley.goals.input_view import select_inputs
from booley.goals.protected_inputs import (
    ProtectedInputError,
    ProtectedInputRoots,
    _disk_tree,
    _HeadView,
    snapshot_protected_inputs,
)
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from tests.goals.conftest import git

IDENTITY_OPTIONS = {
    "--git-common-dir",
    "--git-dir",
    "--show-toplevel",
    "--show-prefix",
    "--show-object-format",
    "--path-format=absolute",
}


@contextmanager
def git_launches(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[tuple[str, ...], str]]]:
    """Record ``(argv, cwd)`` of every ``git`` process launched inside the block."""
    launched: list[tuple[tuple[str, ...], str]] = []
    original = subprocess.Popen.__init__

    def recording(self, args, *positional, **keywords):
        if args and args[0] == "git":
            launched.append((tuple(args), str(keywords.get("cwd"))))
        original(self, args, *positional, **keywords)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess.Popen, "__init__", recording)
        yield launched


def repeated_identity_queries(launched: list[tuple[tuple[str, ...], str]]) -> list[str]:
    """Identity ``rev-parse`` queries asked more than once with the same cwd and argv."""
    counts = Counter(
        (argv, cwd)
        for argv, cwd in launched
        if argv[1] == "rev-parse" and set(argv[2:]) <= IDENTITY_OPTIONS
    )
    return [
        f"{count}x git {' '.join(argv[1:])} in {cwd}"
        for (argv, cwd), count in counts.items()
        if count > 1
    ]


def split_git_directory_queries(launched: list[tuple[tuple[str, ...], str]]) -> list[str]:
    """Checkouts whose Git directory and common directory took more than one launch."""
    counts = Counter(
        cwd
        for argv, cwd in launched
        if argv[1] == "rev-parse" and {"--git-dir", "--git-common-dir"} & set(argv[2:])
    )
    return [
        f"{count}x git rev-parse --git-dir/--git-common-dir in {cwd}"
        for cwd, count in counts.items()
        if count > 1
    ]


def test_protected_snapshot_asks_each_repository_fact_once(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = ProtectedInputRoots(goal_mode.worktree, goal_mode.control)
    with git_launches(monkeypatch) as launched:
        snapshot_protected_inputs(roots)
    listings = Counter(cwd for argv, cwd in launched if argv[1] == "ls-tree" and "HEAD" in argv)
    assert repeated_identity_queries(launched) == []
    assert {cwd: count for cwd, count in listings.items() if count > 1} == {}


def test_goal_status_asks_each_repository_fact_once(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GoalStore(goal_mode.control)
    with git_launches(monkeypatch) as launched:
        build_status(store, store.load(goal_mode.record.id))
    assert repeated_identity_queries(launched) == []


def test_input_selection_asks_each_repository_fact_once(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with git_launches(monkeypatch) as launched:
        select_inputs(goal_mode.record, goal_mode.worktree)
    assert repeated_identity_queries(launched) == []
    assert split_git_directory_queries(launched) == []


def test_reread_outside_a_scope_asks_git_again(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Goal Mode re-checks after side effects on purpose: two computations both ask Git."""
    with git_launches(monkeypatch) as first:
        select_inputs(goal_mode.record, goal_mode.worktree)
    with git_launches(monkeypatch) as second:
        select_inputs(goal_mode.record, goal_mode.worktree)
    assert first
    assert len(second) == len(first)


def test_head_view_prefetch_matches_per_path_listing(goal_mode: SimpleNamespace) -> None:
    worktree = goal_mode.worktree
    (worktree / "hooks").mkdir()
    (worktree / "hooks" / "a.sh").write_text("a\n", encoding="utf-8")
    (worktree / "hooks2").mkdir()
    (worktree / "hooks2" / "b.sh").write_text("b\n", encoding="utf-8")
    (worktree / "ünï.txt").write_text("unicode\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "inputs")
    locations = [
        worktree / name
        for name in ("hooks", "hooks2", "missing", "ünï.txt", "HOOKS", "booley.toml", "docs")
    ]

    def trees(prefetch: bool) -> list[dict]:
        view = _HeadView(include_mode=True)
        if prefetch:
            view.prefetch(locations)
        return [view.committed_tree(path, _disk_tree(path), frozenset()) for path in locations]

    assert trees(prefetch=True) == trees(prefetch=False)


def test_head_view_listing_failure_still_names_the_location(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_ICASE_PATHSPECS", "1")  # ls-tree refuses pathspec magic
    roots = ProtectedInputRoots(goal_mode.worktree, goal_mode.control)
    with pytest.raises(ProtectedInputError, match="cannot list HEAD for"):
        snapshot_protected_inputs(roots)
