"""Goal-aware warning prefixes; session keys remain audit facts only."""

from collections.abc import Sequence
from pathlib import Path

from booley.goals.model import GoalRecord
from booley.goals.protected_inputs import (
    ProtectedInputError,
    ProtectedInputRoots,
    protected_input_violations,
)


def protected_warnings(record: GoalRecord, project_dir: Path, work_dir: Path) -> tuple[str, ...]:
    """Compare resolver paths, working contents, then tracked HEAD contents."""
    try:
        return tuple(
            protected_input_violations(
                record.protected_paths,
                record.protected_digest or "",
                record.protected_head_digest,
                ProtectedInputRoots(work_dir, project_dir),
            )
        )
    except (ProtectedInputError, OSError, ValueError) as exc:
        return (f"protected inputs cannot be inspected: {exc}",)


def goal_warnings(
    record: GoalRecord,
    project_dir: Path,
    *,
    work_dir: Path | None,
    session_key: str | None = None,
    other_sessions: Sequence[str] = (),
) -> str:
    """Render warnings shared by Flow, Specialist, and status responses."""
    warnings = list(
        protected_warnings(record, project_dir, work_dir or Path(record.worktree_path))
    )
    if any(key != session_key for key in other_sessions):
        warnings.append("another session shares this Goal worktree")
    if work_dir is None:
        warnings.append("pass work_dir on every Booley call while Goal Mode is active")
    return "\n".join(f"WARNING: {reason}" for reason in warnings)
