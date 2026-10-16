"""Ended Goal Records: the ``goal status`` fallback and their automatic deletion.

A Goal Record in a final state (``finished``, ``abandoned``, or ``failed``)
stays readable through ``goal status`` in its worktree while that worktree has
no active Goal Mode. Once its worktree and its Goal Branch are both gone it
has no further use, so :func:`sweep_ended_records` deletes it after a Goal is
entered, finished, or abandoned.

A worktree is judged by the record's Worktree Identity, never by the path it
was last seen at (inside the Sandbox every checkout is mounted at one path):
the primary checkout never counts as gone, and a linked worktree is gone once
Git's administrative directory for it (``<common dir>/worktrees/<name>``) no
longer exists. A worktree deleted without ``git worktree remove`` or ``prune``
keeps that directory, so its record is kept.

Every uncertain check keeps the record: a Git error, an unreadable or corrupt
record, a held record lock, or a Git directory that cannot be examined. A
record is deleted under its record lock: it is read and checked again, its
directory is renamed to a hidden ``.deleting-<goal-id>`` sibling (so no reader
sees half a record), and the renamed directory is removed after the lock is
released. A directory a crash left under the hidden name is removed by the
next sweep. The sweep never raises into its caller.

Windows refuses to rename or delete a path while a file under it is open, so
there the rename (and a worktree lock's removal) follows the lock release: a
process that took the lock since holds its file open, the step fails, and the
record or lock is kept.
"""

from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.goals.checkout import CheckoutError, GoalCheckout
from booley.goals.model import GoalRecord, GoalState, WorktreeIdentity
from booley.goals.paths import goals_root, record_paths, worktree_lock_file
from booley.goals.store import (
    MAIN_CHECKOUT,
    GoalStore,
    GoalStoreError,
    WorktreeIdentityError,
    resolve_worktree_identity,
)
from booley.runtime.project_repositories import git_directories
from booley.runtime.timefmt import format_human_datetime_safe

logger = logging.getLogger(__name__)

ENDED_STATES = frozenset({GoalState.FINISHED, GoalState.ABANDONED, GoalState.FAILED})
# A record directory being deleted is renamed under this prefix first.
DELETING_PREFIX = ".deleting-"
# Whether the destructive step follows the lock release (see the module docstring).
_AFTER_RELEASE = sys.platform == "win32"


# ---------------------------------------------------------------------------
# The goal status fallback
# ---------------------------------------------------------------------------


def last_ended_record(store: GoalStore, work_dir: Path) -> GoalRecord | None:
    """The most recently ended Goal Record of the worktree containing *work_dir*, if any."""
    try:
        identity = resolve_worktree_identity(work_dir)
    except WorktreeIdentityError:
        return None  # not a Git checkout: it has no Goal Records
    if identity is None:
        return None
    ended = [
        record
        for record in store.list_records().records
        if record.worktree == identity and record.state in ENDED_STATES
    ]
    return max(
        ended, key=lambda record: (record.ended_at or record.entered_at, record.id), default=None
    )


def with_last_ended(text: str, store: GoalStore, work_dir: Path, *, project_wide: bool) -> str:
    """Append the worktree's last ended Goal Record to status *text*.

    Only when the worktree hosts no Goal Mode (*project_wide*); *text* is
    returned unchanged when it has no ended record either.
    """
    record = last_ended_record(store, work_dir) if project_wide else None
    return text if record is None else f"{text}\n\n{render_last_ended(store, record)}"


def render_last_ended(store: GoalStore, record: GoalRecord) -> str:
    """Id, state, end time, failure, and the Session Summary and Review Package that exist."""
    paths = record_paths(store.booley_dir, record.id)
    lines = [f"Last ended Goal Mode in this worktree: {record.id} ({record.state.value})"]
    if record.ended_at:
        lines.append(f"Ended: {format_human_datetime_safe(record.ended_at)}")
    if record.failure:
        lines.append(f"Failure: {record.failure}")
    if paths.summary_file.is_file():
        lines.append(f"Session Summary: {paths.summary_file}")
    if paths.review_package_file.is_file():
        lines.append(f"Review Package: {paths.review_package_file}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Automatic deletion
# ---------------------------------------------------------------------------


def sweep_ended_records(store: GoalStore, repo_dir: Path) -> tuple[str, ...]:
    """Delete ended Goal Records whose worktree and Goal Branch are gone; return their ids.

    *repo_dir* is any checkout of the Project repository: Goal Branches are
    looked up there, and records bound to another repository are kept.
    Never raises: a failure is logged and the remaining records are kept.
    """
    try:
        return _sweep(store, repo_dir)
    except Exception as exc:  # noqa: BLE001 — cleanup must never fail the lifecycle command that ran it
        logger.warning("Ended Goal Records were not cleaned up: %s", exc)
        return ()


@dataclass(frozen=True)
class _Repository:
    """The Project repository records are judged against: its id, Git common dir, and refs."""

    id: str
    common_dir: Path
    checkout: GoalCheckout


def _sweep(store: GoalStore, repo_dir: Path) -> tuple[str, ...]:
    _remove_leftovers(goals_root(store.booley_dir))
    identity = resolve_worktree_identity(repo_dir)
    if identity is None:
        return ()  # the repository has no id yet, so no record can be bound to it
    repository = _Repository(
        identity.repository, git_directories(repo_dir).common_dir, GoalCheckout(repo_dir)
    )
    # Lock waits of zero: a busy lock means someone is using the record or worktree.
    quick = GoalStore(store.booley_dir, lock_timeout_s=0.0)
    deleted = [
        record
        for record in quick.list_records().records
        if _deletable(record, repository) and _delete(quick, record.id, repository)
    ]
    for worktree in {record.worktree for record in deleted}:
        _remove_worktree_lock(quick, worktree)
    return tuple(record.id for record in deleted)


def _deletable(record: GoalRecord, repository: _Repository) -> bool:
    """Whether *record* has ended and its worktree and Goal Branch are definitely gone."""
    if record.state not in ENDED_STATES or record.worktree.repository != repository.id:
        return False
    if not _worktree_gone(record.worktree, repository.common_dir):
        return False
    try:
        return not repository.checkout.has_branch(record.branch)
    except CheckoutError as exc:
        logger.warning("Kept ended Goal Record %s: %s", record.id, exc)
        return False


def _worktree_gone(identity: WorktreeIdentity, common_dir: Path) -> bool:
    """True only when Git definitely no longer knows *identity*'s linked worktree."""
    if identity.checkout == MAIN_CHECKOUT:
        return False  # the primary checkout is the repository itself
    admin = common_dir / identity.checkout  # ``worktrees/<name>``
    try:
        admin.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError as exc:
        logger.debug("Cannot examine Git worktree directory %s: %s", admin, exc)
    return False


def _delete(store: GoalStore, goal_id: str, repository: _Repository) -> bool:
    """Re-check *goal_id* under its record lock, hide its directory, then remove it."""
    root = record_paths(store.booley_dir, goal_id).root
    hidden = root.with_name(f"{DELETING_PREFIX}{goal_id}")
    try:
        with store.record_lock(goal_id):
            if not _deletable(store.load(goal_id), repository):
                return False
            if not _AFTER_RELEASE:
                root.rename(hidden)
        if _AFTER_RELEASE:
            root.rename(hidden)
    except (GoalStoreError, OSError) as exc:
        logger.debug("Kept ended Goal Record %s: %s", goal_id, exc)
        return False
    shutil.rmtree(hidden, ignore_errors=True)
    return True


def _remove_worktree_lock(store: GoalStore, identity: WorktreeIdentity) -> None:
    """Remove *identity*'s worktree lock unless a remaining record may still bind it."""
    path = worktree_lock_file(store.booley_dir, identity)
    if not path.exists():
        return
    try:
        # Records are created only under this lock, so none can appear meanwhile.
        with store.worktree_lock(identity):
            scan = store.list_records()
            if scan.corrupt or any(record.worktree == identity for record in scan.records):
                return
            if not _AFTER_RELEASE:
                path.unlink()
        if _AFTER_RELEASE:
            path.unlink()
    except (GoalStoreError, OSError) as exc:
        logger.debug("Kept Goal worktree lock %s: %s", path, exc)


def _remove_leftovers(root: Path) -> None:
    """Remove record directories an interrupted deletion left under the hidden prefix."""
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.name.startswith(DELETING_PREFIX):
            shutil.rmtree(entry, ignore_errors=True)
