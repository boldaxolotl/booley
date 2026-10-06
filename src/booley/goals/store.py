"""The Goal Record store: locked, revisioned ``record.json`` files (ADR 0067 D9, D15).

Two locks guard every write, both ``flock`` locks from
:mod:`booley.core.file_lock`, released by the kernel when their holder dies:

- the *worktree lock*, one per :class:`~booley.goals.model.WorktreeIdentity`,
  is held while a new record is created, so "no occupying record for this
  worktree" is checked and the new record published under one lock;
- the *record lock*, one per record, is held around every save.

Lock order is always worktree lock, then record lock: taking a worktree lock
while this context holds a record lock raises :class:`LockOrderError`.

Every save is a compare-and-swap on the record's ``revision``: it succeeds
only if the record on disk still has the revision the caller read, and it
writes the next revision. A record that cannot be read or parsed raises
:class:`GoalRecordCorruptError` naming the file; it is never treated as
absent, because an unreadable record may be the one occupying a worktree.

A worktree is occupied by a record in ``entering``, ``active``, or
``finishing`` (:data:`~booley.goals.model.OCCUPYING_STATES`). Records in a
terminal state never become occupying again, so only creation needs the
uniqueness check.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import shutil
import time
import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from booley.core.boundary import BoundaryError, require_uuid4
from booley.core.file_lock import LockTimeoutError, release_file_lock, wait_for_file_lock
from booley.goals.model import (
    ALLOWED_TRANSITIONS,
    OCCUPYING_STATES,
    GoalRecord,
    GoalRecordFormatError,
    GoalState,
    WorktreeIdentity,
)
from booley.goals.paths import (
    GOAL_ID_PATTERN,
    RECORD_FILE,
    STAGING_PREFIX,
    goals_root,
    record_paths,
    validate_goal_id,
    worktree_lock_file,
)
from booley.runtime.atomic_files import (
    WriteOnceConflictError,
    atomic_replace_bytes,
    atomic_write_once,
    fsync_directory,
)
from booley.runtime.project_repositories import GitDirectoryInspectionError, git_directories

# The repository identity file inside Git's common directory (D3).
REPOSITORY_ID_FILE = Path("booley") / "repository-id"
MAIN_CHECKOUT = "main"
DEFAULT_LOCK_TIMEOUT_S = 30.0
# A staging directory older than this belongs to a create that died; any live
# create renames its staging directory within moments of making it.
STAGING_MAX_AGE_S = 3600.0

# Record locks this context holds, keyed by (Project dir, Goal id): a nested
# ``record_lock`` for a held record re-enters it, and the D15 lock order is
# checked against it. Each change sets a new mapping, so copied contexts (for
# example a thread started with ``copy_context``) never share holds.
_HELD_RECORD_LOCKS: contextvars.ContextVar[Mapping[tuple[Path, str], RecordLock]] = (
    contextvars.ContextVar("booley_goal_record_locks", default=MappingProxyType({}))
)


class GoalStoreError(RuntimeError):
    """A Goal Record operation was refused or could not complete."""


class GoalRecordNotFoundError(GoalStoreError):
    """No Goal Record exists for the requested id."""


@dataclass(frozen=True)
class CorruptRecord:
    """A Goal Record directory whose ``record.json`` cannot be read as a Goal Record."""

    path: Path
    reason: str


class GoalRecordCorruptError(GoalStoreError):
    """One or more Goal Records exist but cannot be read; each is named."""

    def __init__(self, entries: Sequence[CorruptRecord]) -> None:
        details = "; ".join(f"{entry.path}: {entry.reason}" for entry in entries)
        super().__init__(f"corrupt Goal Record {details}")
        self.entries = tuple(entries)
        self.path = entries[0].path


class WorktreeOccupiedError(GoalStoreError):
    """The worktree already hosts a Goal Mode that occupies it."""

    def __init__(self, occupant: GoalRecord) -> None:
        super().__init__(
            f"worktree {occupant.worktree_path} already hosts Goal Mode "
            f"{occupant.id} ({occupant.state.value})"
        )
        self.occupant = occupant


class StaleRevisionError(GoalStoreError):
    """The record changed on disk since the caller read it."""


class InvalidRecordUpdateError(GoalStoreError):
    """A save would make a transition or field change the lifecycle forbids."""


class LockOrderError(GoalStoreError):
    """A worktree lock was requested while a record lock is held."""


class GoalLockTimeoutError(GoalStoreError):
    """A Goal lock stayed busy for the whole allowed wait."""


class WorktreeIdentityError(GoalStoreError):
    """A path is not a Git checkout Booley can identify."""


# ---------------------------------------------------------------------------
# Worktree identity (D3)
# ---------------------------------------------------------------------------


def resolve_worktree_identity(work_dir: Path, *, create: bool = False) -> WorktreeIdentity | None:
    """Identify the Git worktree containing *work_dir*, the same from every spelling.

    The repository half is a UUID stored in Git's common directory. Without
    *create*, a repository that has none yet returns ``None``. With *create*,
    the first caller writes it atomically and concurrent callers all read the
    winner's value. Goal Mode callers use :meth:`GoalStore.identify_worktree`,
    which refuses a missing id while Goal Records occupy worktrees.
    """
    try:
        directories = git_directories(work_dir)
    except GitDirectoryInspectionError as exc:
        raise WorktreeIdentityError(str(exc)) from exc
    checkout = _checkout_name(directories.git_dir, directories.common_dir)
    repository = _repository_id(directories.common_dir, create=create)
    return None if repository is None else WorktreeIdentity(repository, checkout)


def _checkout_name(git_dir: Path, common_dir: Path) -> str:
    """``main`` for the primary checkout, ``worktrees/<admin>`` for a linked one."""
    if git_dir == common_dir:
        return MAIN_CHECKOUT
    if git_dir.parent == common_dir / "worktrees":
        return f"worktrees/{git_dir.name}"
    raise WorktreeIdentityError(f"Git directory {git_dir} is not a worktree of {common_dir}")


def _repository_id(common_dir: Path, *, create: bool) -> str | None:
    path = common_dir / REPOSITORY_ID_FILE
    if not path.exists():
        if not create:
            return None
        # A concurrent caller may win the creation; everyone reads the winner's id.
        with contextlib.suppress(WriteOnceConflictError):
            atomic_write_once(path, f"{uuid.uuid4()}\n".encode(), mode=0o644)
    try:
        return require_uuid4(path.read_text(encoding="utf-8").strip(), field=str(path))
    except (OSError, UnicodeDecodeError, BoundaryError) as exc:
        raise WorktreeIdentityError(f"unreadable repository identity {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


class WorktreeLock:
    """Proof that the caller holds the worktree lock of :attr:`identity`."""

    def __init__(self, project_dir: Path, identity: WorktreeIdentity) -> None:
        self.project_dir = project_dir
        self.identity = identity
        self.held = True


class RecordLock:
    """Proof that the caller holds the record lock of Goal Record :attr:`goal_id`."""

    def __init__(self, project_dir: Path, goal_id: str) -> None:
        self.project_dir = project_dir
        self.goal_id = goal_id
        self.held = True

    @property
    def record_dir(self) -> Path:
        """The locked record's directory."""
        return record_paths(self.project_dir, self.goal_id).root


class _LockDirectoryMissingError(GoalStoreError):
    """The directory of a lock file vanished before the lock was opened."""


@contextmanager
def _flock(path: Path, timeout_s: float) -> Generator[None]:
    """Hold an exclusive ``flock`` on *path*, whose directory must exist."""
    try:
        opened = path.open("a+", encoding="utf-8")
    except FileNotFoundError as exc:
        raise _LockDirectoryMissingError(f"{path.parent} does not exist") from exc
    with opened as handle:
        try:
            wait_for_file_lock(handle, timeout_s=timeout_s)
        except LockTimeoutError as exc:
            raise GoalLockTimeoutError(f"Goal lock {path} stayed busy: {exc}") from exc
        try:
            yield
        finally:
            release_file_lock(handle)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordScan:
    """Every Goal Record directory of a Project: readable records and corrupt ones."""

    records: tuple[GoalRecord, ...]
    corrupt: tuple[CorruptRecord, ...]


# Fields fixed when a record is created; a save may not change them.
_CREATION_FIELDS = ("id", "worktree", "branch", "original_ref", "base_sha", "entered_at")


@dataclass(frozen=True)
class GoalStore:
    """Goal Records of one Project directory (resolved by the caller)."""

    project_dir: Path
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S

    def identify_worktree(
        self, work_dir: Path, *, create: bool = False
    ) -> WorktreeIdentity | None:
        """The Worktree Identity of *work_dir*, creating the repository id with *create*.

        A missing repository id while any Goal Record occupies a worktree means
        the id was deleted; minting a new one would orphan those records and
        let a second Goal Mode enter, so it raises :class:`WorktreeIdentityError`.
        """
        identity = resolve_worktree_identity(work_dir)
        if identity is not None:
            return identity
        occupying = self.list_active().records
        if occupying:
            ids = ", ".join(record.id for record in occupying)
            raise WorktreeIdentityError(
                f"the repository id of {work_dir} is missing while Goal Records {ids} "
                "occupy worktrees; restore it rather than entering again"
            )
        return resolve_worktree_identity(work_dir, create=True) if create else None

    @contextmanager
    def worktree_lock(self, identity: WorktreeIdentity) -> Generator[WorktreeLock]:
        """Hold the worktree lock of *identity* (taken before any record lock)."""
        if _HELD_RECORD_LOCKS.get():
            raise LockOrderError("a worktree lock must be taken before any record lock")
        lock = WorktreeLock(self.project_dir, identity)
        path = worktree_lock_file(self.project_dir, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _flock(path, self.lock_timeout_s):
            try:
                yield lock
            finally:
                lock.held = False

    @contextmanager
    def record_lock(self, goal_id: str) -> Generator[RecordLock]:
        """Hold the record lock of an existing Goal Record; re-entrant in one context.

        The record directory is never created here: a record removed after
        the existence check makes the lock open fail and raise
        :class:`GoalRecordNotFoundError` instead of leaving a lock-only
        directory behind.
        """
        key = (self.project_dir, goal_id)
        held = _HELD_RECORD_LOCKS.get()
        if key in held:
            yield held[key]
            return
        paths = record_paths(self.project_dir, goal_id)
        if not paths.root.is_dir():
            raise GoalRecordNotFoundError(f"no Goal Record {goal_id} in {self.project_dir}")
        lock = RecordLock(self.project_dir, goal_id)
        token = _HELD_RECORD_LOCKS.set(MappingProxyType({**held, key: lock}))
        try:
            with _flock(paths.lock_file, self.lock_timeout_s):
                try:
                    yield lock
                finally:
                    lock.held = False
        except _LockDirectoryMissingError as exc:
            raise GoalRecordNotFoundError(f"Goal Record {goal_id} disappeared") from exc
        finally:
            _HELD_RECORD_LOCKS.reset(token)

    def create(self, lock: WorktreeLock, record: GoalRecord) -> GoalRecord:
        """Publish a new ``entering`` record at revision 1 under *lock*.

        Refuses with :class:`WorktreeOccupiedError` when another record
        occupies the worktree, and with :class:`GoalRecordCorruptError` while
        any record is unreadable, since that record may be the occupant. The
        record directory appears complete or not at all: it is built under a
        staging name and renamed into place.
        """
        self._check_create(lock, record)
        scan = self.list_records()
        if scan.corrupt:
            raise GoalRecordCorruptError(scan.corrupt)
        occupant = _occupant(scan.records, record.worktree)
        if occupant is not None:
            raise WorktreeOccupiedError(occupant)
        self._sweep_stale_staging()
        return self._publish(replace(record, revision=1))

    def _publish(self, created: GoalRecord) -> GoalRecord:
        target = record_paths(self.project_dir, created.id).root
        staging = goals_root(self.project_dir) / f"{STAGING_PREFIX}{created.id}-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            atomic_replace_bytes(staging / RECORD_FILE, _encode(created))
            staging.rename(target)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise GoalStoreError(f"cannot publish Goal Record {created.id}: {exc}") from exc
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        fsync_directory(target.parent)
        return created

    def _sweep_stale_staging(self) -> None:
        """Remove staging directories left by creates that died mid-publication."""
        root = goals_root(self.project_dir)
        if not root.is_dir():
            return
        cutoff = time.time() - STAGING_MAX_AGE_S
        for entry in root.iterdir():
            if entry.name.startswith(STAGING_PREFIX) and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)

    def _check_create(self, lock: WorktreeLock, record: GoalRecord) -> None:
        if not lock.held or lock.project_dir != self.project_dir:
            raise GoalStoreError("create needs this store's worktree lock, held")
        if lock.identity != record.worktree:
            raise GoalStoreError("the worktree lock does not belong to the record's worktree")
        if record.state is not GoalState.ENTERING or record.revision != 0:
            raise InvalidRecordUpdateError("a new Goal Record starts as 'entering' at revision 0")
        validate_goal_id(record.id)
        _validate_encodable(replace(record, revision=1))

    def load(self, goal_id: str) -> GoalRecord:
        """Read Goal Record *goal_id*; corrupt content raises, never returns ``None``."""
        paths = record_paths(self.project_dir, goal_id)
        if not paths.root.is_dir():
            raise GoalRecordNotFoundError(f"no Goal Record {goal_id} in {self.project_dir}")
        try:
            raw: Any = json.loads(paths.record_file.read_bytes().decode("utf-8"))
            record = GoalRecord.from_json(raw)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise GoalRecordCorruptError([CorruptRecord(paths.record_file, str(exc))]) from exc
        if record.id != goal_id:
            reason = f"it names Goal Record {record.id}"
            raise GoalRecordCorruptError([CorruptRecord(paths.record_file, reason)])
        return record

    def save(self, lock: RecordLock, record: GoalRecord) -> GoalRecord:
        """Write *record* if the disk still holds ``record.revision``; return the new record.

        Raises :class:`StaleRevisionError` when another writer saved first,
        and :class:`InvalidRecordUpdateError` for a forbidden state
        transition, a change to a field fixed at creation, or content that
        would not load back.
        """
        if not lock.held or lock.goal_id != record.id or lock.project_dir != self.project_dir:
            raise GoalStoreError(f"save needs the held record lock of {record.id}")
        current = self.load(record.id)
        if current.revision != record.revision:
            raise StaleRevisionError(
                f"Goal Record {record.id} is at revision {current.revision}, "
                f"not the revision {record.revision} this update read"
            )
        if record.state not in ALLOWED_TRANSITIONS[current.state]:
            raise InvalidRecordUpdateError(
                f"Goal Record {record.id} cannot move from {current.state.value} "
                f"to {record.state.value}"
            )
        changed = [
            name for name in _CREATION_FIELDS if getattr(record, name) != getattr(current, name)
        ]
        if changed:
            raise InvalidRecordUpdateError(f"Goal Record {record.id} cannot change {changed}")
        saved = replace(record, revision=current.revision + 1)
        atomic_replace_bytes(
            record_paths(self.project_dir, record.id).record_file, _validate_encodable(saved)
        )
        return saved

    def list_records(self) -> RecordScan:
        """Every Goal Record in the Project, ordered by id, with the corrupt ones named."""
        root = goals_root(self.project_dir)
        if not root.is_dir():
            return RecordScan((), ())
        ids = sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir() and GOAL_ID_PATTERN.fullmatch(entry.name)
        )
        records: list[GoalRecord] = []
        corrupt: list[CorruptRecord] = []
        for goal_id in ids:
            try:
                records.append(self.load(goal_id))
            except GoalRecordCorruptError as exc:
                corrupt.extend(exc.entries)
        return RecordScan(tuple(records), tuple(corrupt))

    def list_active(self) -> RecordScan:
        """Records that occupy their worktree (entering, active, finishing), and corrupt ones."""
        scan = self.list_records()
        active = tuple(record for record in scan.records if record.state in OCCUPYING_STATES)
        return RecordScan(active, scan.corrupt)

    def active_for_identity(self, identity: WorktreeIdentity) -> GoalRecord | None:
        """The record occupying *identity*'s worktree, or ``None``.

        A readable occupant is returned even while other records are corrupt.
        With no readable occupant, a corrupt record raises
        :class:`GoalRecordCorruptError`: it may be the occupant.
        """
        scan = self.list_records()
        occupant = _occupant(scan.records, identity)
        if occupant is None and scan.corrupt:
            raise GoalRecordCorruptError(scan.corrupt)
        return occupant

    def active_for_worktree(self, work_dir: Path) -> GoalRecord | None:
        """The record occupying the worktree that contains *work_dir*, or ``None``."""
        identity = self.identify_worktree(work_dir)
        return None if identity is None else self.active_for_identity(identity)


def _occupant(records: Sequence[GoalRecord], identity: WorktreeIdentity) -> GoalRecord | None:
    matches = [r for r in records if r.state in OCCUPYING_STATES and r.worktree == identity]
    if len(matches) > 1:
        ids = ", ".join(record.id for record in matches)
        raise GoalStoreError(f"several Goal Records occupy one worktree: {ids}")
    return matches[0] if matches else None


def _encode(record: GoalRecord) -> bytes:
    return (json.dumps(record.to_json(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def _validate_encodable(record: GoalRecord) -> bytes:
    """Encode *record*, refusing content :meth:`GoalStore.load` would reject."""
    encoded = _encode(record)
    try:
        GoalRecord.from_json(json.loads(encoded))
    except GoalRecordFormatError as exc:
        raise InvalidRecordUpdateError(
            f"Goal Record {record.id} would not load back: {exc}"
        ) from exc
    return encoded
