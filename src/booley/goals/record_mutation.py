"""Existing-record ownership for bounded writes by admitted Goal producers.

The original binding fixes identity, while the store lock fences pruning and
replacement inodes. Revision advancement does not transfer that ownership.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from booley.goals.binding import GoalRunBinding
from booley.goals.model import GoalState
from booley.goals.paths import GoalRecordPaths, record_paths
from booley.goals.store import GoalStore, GoalStoreError


def _unlinked(path: Path) -> None:
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or getattr(ancestor, "is_junction", lambda: False)():
            raise GoalStoreError(f"linked Goal mutation destination: {ancestor}")


@contextmanager
def bound_record_mutation(
    binding: GoalRunBinding, destination: Path, *, active: bool = False
) -> Iterator[GoalRecordPaths]:
    """Hold the original existing record while mutating its confined destination.

    Admission and advisory publication require ACTIVE; late bookkeeping may
    settle a terminal owner. Missing roots, intents and changed ownership refuse.
    No lock file or record directory is created by this operation.
    """
    paths = record_paths(binding.booley_dir, binding.record_id)
    if (
        not destination.is_absolute()
        or not destination.is_relative_to(paths.root)
        or ".." in destination.parts
    ):
        raise GoalStoreError("Goal mutation destination differs from admitted record")
    _unlinked(destination)
    store = GoalStore(binding.booley_dir)
    with store.record_lock(binding.record_id, existing_only=True):
        _unlinked(destination)
        record = store.load(binding.record_id)
        if (
            record.id != binding.record_id
            or record.worktree != binding.worktree
            or record.branch != binding.goal_branch
            or Path(record.worktree_path) != binding.worktree_root
        ):
            raise GoalStoreError("Goal mutation binding differs from immutable record owner")
        if active and record.state is not GoalState.ACTIVE:
            raise GoalStoreError(f"Goal admission requires ACTIVE; record is {record.state.value}")
        yield paths
