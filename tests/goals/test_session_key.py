"""Session audit fallback identifies a worktree rather than its spelling."""

from types import SimpleNamespace

from booley.goals.session_key import session_key
from booley.goals.store import GoalStore


def test_spelling_independent_key(goal_mode: SimpleNamespace) -> None:
    store = GoalStore(goal_mode.control)
    assert session_key(store, goal_mode.worktree) == session_key(
        store, goal_mode.worktree / "docs"
    )
    assert session_key(store, goal_mode.worktree) == goal_mode.record.session_key
    assert session_key(store, goal_mode.main) != goal_mode.record.session_key
