"""Worktree audit keys until the Phase 6 process session registry arrives."""

from pathlib import Path

from booley.goals.model import WorktreeIdentity
from booley.goals.store import GoalStore


def worktree_session_key(identity: WorktreeIdentity) -> str:
    """Return the same audit key for every spelling of a Worktree Identity."""
    return f"worktree:{identity.key}"


def session_key(store: GoalStore, work_dir: Path) -> str | None:
    """Read an existing identity without creating repository metadata."""
    identity = store.identify_worktree(work_dir)
    return None if identity is None else worktree_session_key(identity)
