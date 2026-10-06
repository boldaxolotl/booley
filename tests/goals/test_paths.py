"""The Goal Record layout (ADR 0067 D9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.goals.model import WorktreeIdentity
from booley.goals.paths import (
    GoalIdError,
    history_file,
    new_goal_id,
    record_paths,
    validate_goal_id,
    worktree_lock_file,
)

REPOSITORY = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"


def test_goal_id_is_slug_and_compact_utc_timestamp() -> None:
    assert new_goal_id("fix-uart", timestamp="20261006T101500Z") == "fix-uart-20261006T101500Z"
    assert validate_goal_id(new_goal_id("fix-uart")).startswith("fix-uart-2")


@pytest.mark.parametrize("slug", ["", "Fix", "fix_uart", "-fix", "fix--uart", "a" * 65, "../x"])
def test_bad_slugs_are_refused(slug: str) -> None:
    with pytest.raises(GoalIdError):
        new_goal_id(slug, timestamp="20261006T101500Z")


@pytest.mark.parametrize(
    "goal_id", ["locks", "history", "fix-uart", "fix-uart-2026", "../x-20261006T101500Z"]
)
def test_non_goal_ids_are_refused(goal_id: str) -> None:
    with pytest.raises(GoalIdError):
        record_paths(Path("/p"), goal_id)


def test_record_layout_lives_under_the_callers_project_dir(tmp_path: Path) -> None:
    paths = record_paths(tmp_path, "fix-uart-20261006T101500Z")
    root = tmp_path / "goals" / "fix-uart-20261006T101500Z"
    assert paths.root == root
    assert paths.record_file == root / "record.json"
    assert paths.lock_file == root / "lock"
    assert paths.state_file == root / "booley_state.json"
    assert paths.logs_dir == root / "logs"
    assert paths.runtime_dir == root / ".runtime"
    assert paths.jobs_dir == root / ".runtime" / "jobs"
    assert paths.changes_file == root / "changes.jsonl"
    assert paths.summary_file == root / "SUMMARY.md"
    assert paths.review_package_file == root / "review-package.json"


def test_history_file_is_in_the_checkouts_own_project_dir(tmp_path: Path) -> None:
    assert history_file(tmp_path, "fix-uart-20261006T101500Z") == (
        tmp_path / "goals" / "history" / "fix-uart-20261006T101500Z.md"
    )


def test_worktree_lock_is_keyed_by_identity_not_path(tmp_path: Path) -> None:
    linked = worktree_lock_file(tmp_path, WorktreeIdentity(REPOSITORY, "worktrees/a"))
    assert linked.parent == tmp_path / "goals" / "locks"
    assert linked == worktree_lock_file(tmp_path, WorktreeIdentity(REPOSITORY, "worktrees/a"))
    assert linked != worktree_lock_file(tmp_path, WorktreeIdentity(REPOSITORY, "main"))
