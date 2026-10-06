"""The Goal Record store: worktree identity, locks, and revisioned writes (D3, D15)."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from booley.goals.model import (
    GoalFamily,
    GoalRecord,
    GoalSpec,
    GoalState,
    RecordedGoal,
    WorktreeIdentity,
)
from booley.goals.paths import record_paths
from booley.goals.store import (
    STAGING_MAX_AGE_S,
    GoalLockTimeoutError,
    GoalRecordCorruptError,
    GoalRecordNotFoundError,
    GoalStore,
    GoalStoreError,
    InvalidRecordUpdateError,
    LockOrderError,
    RecordLock,
    StaleRevisionError,
    WorktreeIdentityError,
    WorktreeOccupiedError,
    resolve_worktree_identity,
)
from booley.runtime.project_repositories import GitDirectoryInspectionError, git_directories
from tests.conftest import symlink_or_skip

SHA = "a" * 40
GIT_TIMEOUT_S = 30
RACE_TIMEOUT_S = 20


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Goal Test", "-c", "user.email=goal@test.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_TIMEOUT_S,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    """A repository with one linked worktree and a Project dir."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "commit", "-q", "--allow-empty", "-m", "base")
    linked = tmp_path / "wt" / "uart"
    _git(main, "worktree", "add", "-q", str(linked), "-b", "work")
    project = tmp_path / "project" / ".booley_project"
    project.mkdir(parents=True)
    return {"main": main, "linked": linked, "project": project}


@pytest.fixture
def alias(repo: dict[str, Path], tmp_path: Path) -> Path:
    """A second spelling of the linked worktree (skipped where symlinks are unavailable)."""
    link = tmp_path / "alias"
    symlink_or_skip(link, repo["linked"], target_is_directory=True)
    return link


def _record(identity: WorktreeIdentity, goal_id: str, path: Path) -> GoalRecord:
    return GoalRecord(
        id=goal_id,
        state=GoalState.ENTERING,
        worktree=identity,
        worktree_path=str(path),
        branch=f"goal/{goal_id}",
        original_ref="refs/heads/work",
        base_sha=SHA,
        entered_at="2026-10-06T10:15:00Z",
    )


def _enter(store: GoalStore, work_dir: Path, goal_id: str) -> GoalRecord:
    identity = store.identify_worktree(work_dir, create=True)
    assert identity is not None
    with store.worktree_lock(identity) as lock:
        return store.create(lock, _record(identity, goal_id, work_dir))


def _save(store: GoalStore, record: GoalRecord, **changes: Any) -> GoalRecord:
    with store.record_lock(record.id) as lock:
        return store.save(lock, replace(record, **changes))


def _corrupt(project: Path, goal_id: str, content: bytes = b"{not json") -> Path:
    path = record_paths(project, goal_id).record_file
    path.write_bytes(content)
    return path


# --- Worktree identity ------------------------------------------------------------


def test_identity_names_primary_and_linked_checkouts(repo: dict[str, Path]) -> None:
    main = resolve_worktree_identity(repo["main"], create=True)
    linked = resolve_worktree_identity(repo["linked"])
    assert main is not None and linked is not None
    assert main.checkout == "main"
    assert linked.checkout == "worktrees/uart"
    assert main.repository == linked.repository


def test_two_spellings_of_one_worktree_have_one_identity(
    repo: dict[str, Path], alias: Path
) -> None:
    direct = resolve_worktree_identity(repo["linked"], create=True)
    assert direct == resolve_worktree_identity(alias)
    assert direct == resolve_worktree_identity(alias / ".")


def test_identity_is_absent_until_a_goal_mode_creates_it(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    assert store.identify_worktree(repo["linked"]) is None
    created = store.identify_worktree(repo["linked"], create=True)
    assert created is not None
    assert store.identify_worktree(repo["linked"], create=True) == created


def test_a_directory_outside_git_has_no_identity(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    with pytest.raises(WorktreeIdentityError, match="not a Git checkout"):
        resolve_worktree_identity(outside)
    with pytest.raises(GitDirectoryInspectionError):
        git_directories(outside)


def test_git_directories_names_both_metadata_directories(repo: dict[str, Path]) -> None:
    directories = git_directories(repo["linked"])
    common = (repo["main"] / ".git").resolve()
    assert directories.common_dir == common
    assert directories.git_dir == common / "worktrees" / "uart"


def test_a_damaged_repository_id_is_reported(repo: dict[str, Path]) -> None:
    resolve_worktree_identity(repo["main"], create=True)
    (repo["main"] / ".git" / "booley" / "repository-id").write_text("garbage\n")
    with pytest.raises(WorktreeIdentityError, match="unreadable repository identity"):
        resolve_worktree_identity(repo["linked"])


def test_a_deleted_repository_id_never_lets_a_second_goal_mode_enter(
    repo: dict[str, Path],
) -> None:
    store = GoalStore(repo["project"])
    _enter(store, repo["linked"], "a-20261006T101500Z")
    (repo["main"] / ".git" / "booley" / "repository-id").unlink()

    with pytest.raises(WorktreeIdentityError, match="missing while Goal Records"):
        store.active_for_worktree(repo["linked"])
    with pytest.raises(WorktreeIdentityError, match="missing while Goal Records"):
        _enter(store, repo["linked"], "b-20261006T101600Z")
    assert not (repo["main"] / ".git" / "booley" / "repository-id").exists()


# --- Create, load, occupancy --------------------------------------------------------


def test_create_publishes_revision_one_and_load_reads_it(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    created = _enter(store, repo["linked"], "fix-uart-20261006T101500Z")

    assert created.revision == 1
    assert store.load(created.id) == created
    assert store.active_for_worktree(repo["linked"]) == created
    assert store.list_active().records == (created,)
    assert store.active_for_worktree(repo["main"]) is None


def test_second_occupying_record_for_one_worktree_is_refused(
    repo: dict[str, Path], alias: Path
) -> None:
    store = GoalStore(repo["project"])
    first = _enter(store, repo["linked"], "fix-uart-20261006T101500Z")

    with pytest.raises(WorktreeOccupiedError) as caught:
        _enter(store, alias, "other-20261006T101600Z")  # same worktree, other spelling
    assert caught.value.occupant == first
    assert [r.id for r in store.list_records().records] == [first.id]


def test_a_different_worktree_may_enter_alongside(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    _enter(store, repo["linked"], "fix-uart-20261006T101500Z")
    _enter(store, repo["main"], "other-20261006T101600Z")
    assert len(store.list_active().records) == 2


def test_failed_record_is_replaceable_but_terminal(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    failed = _save(
        store, _enter(store, repo["linked"], "a-20261006T101500Z"), state=GoalState.FAILED
    )
    assert store.active_for_worktree(repo["linked"]) is None

    replacement = _enter(store, repo["linked"], "b-20261006T101600Z")
    assert store.active_for_worktree(repo["linked"]) == replacement
    with pytest.raises(InvalidRecordUpdateError, match="cannot move from failed to active"):
        _save(store, failed, state=GoalState.ACTIVE)


@pytest.mark.parametrize("state", [GoalState.ACTIVE, GoalState.FINISHING])
def test_every_occupying_state_blocks_a_second_entry(
    repo: dict[str, Path], state: GoalState
) -> None:
    store = GoalStore(repo["project"])
    record = _save(
        store, _enter(store, repo["linked"], "a-20261006T101500Z"), state=GoalState.ACTIVE
    )
    if state is GoalState.FINISHING:
        _save(store, record, state=state)
    with pytest.raises(WorktreeOccupiedError):
        _enter(store, repo["linked"], "b-20261006T101600Z")


def test_create_requires_a_new_entering_record_under_its_own_lock(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    identity = store.identify_worktree(repo["linked"], create=True)
    other = store.identify_worktree(repo["main"])
    assert identity is not None and other is not None
    record = _record(identity, "a-20261006T101500Z", repo["linked"])
    with (
        store.worktree_lock(other) as lock,
        pytest.raises(GoalStoreError, match="does not belong"),
    ):
        store.create(lock, record)
    with store.worktree_lock(identity) as lock, pytest.raises(InvalidRecordUpdateError):
        store.create(lock, replace(record, state=GoalState.ACTIVE))
    with store.worktree_lock(identity) as lock:
        pass
    with pytest.raises(GoalStoreError, match="held"):
        store.create(lock, record)


def test_same_goal_id_from_another_worktree_is_a_store_error(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    _enter(store, repo["linked"], "a-20261006T101500Z")
    # Another worktree publishing the same id in the same second: the rename fails.
    with pytest.raises(GoalStoreError, match="cannot publish"):
        _enter(store, repo["main"], "a-20261006T101500Z")
    assert not [p for p in (repo["project"] / "goals").iterdir() if p.name.startswith(".staging-")]


def test_create_sweeps_only_stale_staging_directories(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    goals = repo["project"] / "goals"
    stale, fresh = goals / ".staging-old-1", goals / ".staging-new-2"
    for directory in (stale, fresh):
        directory.mkdir(parents=True)
    old = time.time() - STAGING_MAX_AGE_S - 60
    os.utime(stale, (old, old))

    _enter(store, repo["linked"], "a-20261006T101500Z")
    assert not stale.exists()
    assert fresh.exists()


def _race_create(project: str, work_dir: str, goal_id: str, start: Any, results: Any) -> None:
    try:
        store = GoalStore(Path(project))
        start.wait(RACE_TIMEOUT_S)
        _enter(store, Path(work_dir), goal_id)
        results.put(("won", goal_id))
    except WorktreeOccupiedError:
        results.put(("refused", goal_id))
    except Exception as exc:  # noqa: BLE001 — report any child failure to the parent
        results.put(("error", repr(exc)))


def test_concurrent_entries_from_two_processes_yield_one_winner(repo: dict[str, Path]) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_race_create,
            args=(
                str(repo["project"]),
                str(repo["linked"]),
                f"race{i}-20261006T101500Z",
                start,
                results,
            ),
        )
        for i in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = sorted(results.get(timeout=RACE_TIMEOUT_S) for _ in workers)
    for worker in workers:
        worker.join(timeout=RACE_TIMEOUT_S)

    assert [outcome for outcome, _ in outcomes] == ["refused", "won"], outcomes
    assert len(GoalStore(repo["project"]).list_active().records) == 1


# --- Save --------------------------------------------------------------------------


def test_save_is_compare_and_swap_on_revision(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    read = _enter(store, repo["linked"], "a-20261006T101500Z")
    saved = _save(store, read, state=GoalState.ACTIVE)
    assert saved.revision == 2

    with pytest.raises(StaleRevisionError, match="revision 2, not the revision 1"):
        _save(store, read, state=GoalState.ABANDONED)
    assert store.load(read.id) == saved


@pytest.mark.parametrize(
    "changes",
    [
        {"worktree_change": True},
        {"base_sha": "b" * 40},
        {"original_ref": "refs/heads/other"},
        {"entered_at": "2026-10-07T00:00:00Z"},
        {"branch": "goal/renamed"},
    ],
)
def test_save_refuses_changes_to_creation_fields(
    repo: dict[str, Path], changes: dict[str, Any]
) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    if changes.pop("worktree_change", False):
        changes = {"worktree": WorktreeIdentity(record.worktree.repository, "main")}
    with pytest.raises(InvalidRecordUpdateError, match="cannot change"):
        _save(store, record, **changes)


def test_save_refuses_a_record_that_would_not_load_back(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    future = RecordedGoal(GoalSpec("lint_clean_a", GoalFamily.LINT, "a", {}, ("x",)), 6)
    with pytest.raises(InvalidRecordUpdateError, match="would not load back"):
        _save(store, record, goals=(future,))
    with pytest.raises(InvalidRecordUpdateError, match="would not load back"):
        _save(store, record, protected_digest="md5:x")
    assert store.load(record.id) == record


def test_save_refuses_an_unheld_lock(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    with store.record_lock(record.id) as lock:
        pass
    with pytest.raises(GoalStoreError, match="no longer held"):
        store.save(lock, record)


# --- Locks -------------------------------------------------------------------------


def test_lock_order_is_worktree_then_record(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    with store.worktree_lock(record.worktree), store.record_lock(record.id):
        pass
    with (
        store.record_lock(record.id),
        pytest.raises(LockOrderError),
        store.worktree_lock(record.worktree),
    ):
        pass


def test_record_lock_is_reentrant_within_one_context(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"], lock_timeout_s=0.2)
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    with store.record_lock(record.id) as outer:
        started = time.monotonic()
        with store.record_lock(record.id) as inner:
            assert inner is outer
            saved = store.save(inner, replace(record, state=GoalState.ACTIVE))
        assert time.monotonic() - started < 0.1
        assert outer.held  # leaving the nested block keeps the outer hold
        store.save(outer, replace(saved, state=GoalState.ABANDONED))
        # Re-entry does not relax the lock order.
        with pytest.raises(LockOrderError), store.worktree_lock(record.worktree):
            pass


def test_a_held_lock_times_out_in_another_thread(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    impatient = GoalStore(repo["project"], lock_timeout_s=0.2)
    errors: list[BaseException] = []

    def contend() -> None:
        try:
            with impatient.record_lock(record.id):
                pass
        except GoalLockTimeoutError as exc:
            errors.append(exc)

    with store.record_lock(record.id):
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=GIT_TIMEOUT_S)
    assert len(errors) == 1


def test_record_lock_never_creates_a_removed_record_directory(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    root = record_paths(repo["project"], record.id).root
    shutil.rmtree(root)
    with pytest.raises(GoalRecordNotFoundError), store.record_lock(record.id):
        pass
    assert not root.exists()


# --- Corruption --------------------------------------------------------------------


@pytest.mark.parametrize("content", [b"{not json", b"", b"\xff\xfe", b'{"schema": 1}', b"[]"])
def test_corrupt_record_is_reported_not_treated_as_absent(
    repo: dict[str, Path], content: bytes
) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    record_file = _corrupt(repo["project"], record.id, content)

    with pytest.raises(GoalRecordCorruptError) as caught:
        store.load(record.id)
    assert caught.value.path == record_file
    with pytest.raises(GoalRecordCorruptError):
        store.active_for_worktree(repo["linked"])  # the corrupt record may be the occupant
    with pytest.raises(GoalRecordCorruptError):
        _enter(store, repo["linked"], "b-20261006T101600Z")


def test_one_corrupt_record_does_not_hide_a_readable_occupant(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    occupant = _enter(store, repo["linked"], "a-20261006T101500Z")
    other = _enter(store, repo["main"], "b-20261006T101600Z")
    bad = _corrupt(repo["project"], other.id)

    assert store.active_for_worktree(repo["linked"]) == occupant
    scan = store.list_records()
    assert scan.records == (occupant,)
    assert [entry.path for entry in scan.corrupt] == [bad]
    assert store.list_active().corrupt == scan.corrupt
    with pytest.raises(GoalRecordCorruptError, match=str(bad)):
        store.active_for_worktree(repo["main"])  # no readable occupant there
    with pytest.raises(GoalRecordCorruptError):
        _enter(store, repo["main"], "c-20261006T101700Z")  # create refuses while any is corrupt


def test_record_directory_without_record_file_is_corrupt(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    (repo["project"] / "goals" / "lost-20261006T101500Z").mkdir(parents=True)
    corrupt = store.list_records().corrupt
    assert [entry.path.name for entry in corrupt] == ["record.json"]


def test_record_naming_another_id_is_corrupt(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    other = record_paths(repo["project"], "b-20261006T101600Z").root
    record_paths(repo["project"], record.id).root.rename(other)
    with pytest.raises(GoalRecordCorruptError, match="names Goal Record a-20261006T101500Z"):
        store.load("b-20261006T101600Z")


def test_missing_record_and_non_record_directories(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    assert store.list_records().records == ()
    (repo["project"] / "goals" / "locks").mkdir(parents=True)
    (repo["project"] / "goals" / "history").mkdir()
    (repo["project"] / "goals" / ".staging-x").mkdir()
    scan = store.list_records()
    assert (scan.records, scan.corrupt) == ((), ())
    with pytest.raises(GoalRecordNotFoundError):
        store.load("a-20261006T101500Z")


# --- Lock ownership (who may re-enter or use a record lock) ------------------------


def _in_copied_context_thread(work: Any, context: Any = None) -> list[object]:
    """Run *work* in a thread under *context* (default: a copy of this one)."""
    import contextvars

    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(work())
        except Exception as exc:  # noqa: BLE001 — the test inspects the failure
            outcome.append(exc)

    context = context or contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(run,))
    thread.start()
    thread.join(timeout=GIT_TIMEOUT_S)
    return outcome


def test_a_copied_context_thread_contends_instead_of_re_entering(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"], lock_timeout_s=0.2)
    record = _enter(store, repo["linked"], "a-20261006T101500Z")

    def take() -> RecordLock:
        with store.record_lock(record.id) as lock:
            assert lock.held
            return lock

    import contextvars

    with store.record_lock(record.id) as parent:
        captured = contextvars.copy_context()  # taken while the parent holds the lock
        (blocked,) = _in_copied_context_thread(take)
        assert isinstance(blocked, GoalLockTimeoutError)
        (wrong_owner,) = _in_copied_context_thread(
            lambda: store.save(parent, replace(record, state=GoalState.ACTIVE))
        )
        assert isinstance(wrong_owner, GoalStoreError)
        assert "another process, thread, or task" in str(wrong_owner)
        # An unrelated thread holds no record lock, so the lock order allows it.
        identity = record.worktree

        def worktree_lock_in_thread() -> bool:
            with store.worktree_lock(identity):
                return True

        assert _in_copied_context_thread(worktree_lock_in_thread) == [True]
    (acquired,) = _in_copied_context_thread(take)
    assert isinstance(acquired, RecordLock)
    assert acquired is not parent
    # A context captured while the parent held the lock gets a fresh lock, not the stale one.
    (fresh,) = _in_copied_context_thread(take, captured)
    assert isinstance(fresh, RecordLock)
    assert fresh is not parent


def test_an_asyncio_task_does_not_re_enter_its_parents_lock(repo: dict[str, Path]) -> None:
    import asyncio

    store = GoalStore(repo["project"], lock_timeout_s=0.2)
    record = _enter(store, repo["linked"], "a-20261006T101500Z")

    async def child() -> None:
        with store.record_lock(record.id):
            pass

    async def parent() -> BaseException | None:
        with store.record_lock(record.id):
            task = asyncio.ensure_future(child())
            try:
                await task
            except GoalLockTimeoutError as exc:
                return exc
        return None

    assert isinstance(asyncio.run(parent()), GoalLockTimeoutError)


def test_a_released_lock_is_never_honoured_again(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    with store.record_lock(record.id) as first:
        pass
    with store.record_lock(record.id) as second:
        assert second is not first
        assert second.held and not first.held
        with pytest.raises(GoalStoreError, match="no longer held"):
            store.save(first, replace(record, state=GoalState.ACTIVE))
        store.save(second, replace(record, state=GoalState.ACTIVE))


def test_a_second_project_spelling_re_enters_the_same_lock(
    repo: dict[str, Path], tmp_path: Path
) -> None:
    store = GoalStore(repo["project"], lock_timeout_s=0.2)
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    spelling = tmp_path / "project-alias"
    symlink_or_skip(spelling, repo["project"], target_is_directory=True)
    aliased = GoalStore(spelling, lock_timeout_s=0.2)

    with store.record_lock(record.id) as outer:
        started = time.monotonic()
        with aliased.record_lock(record.id) as inner:
            assert inner is outer
            saved = aliased.save(inner, replace(record, state=GoalState.ACTIVE))
        assert time.monotonic() - started < 0.1
    assert store.load(record.id) == saved


# --- Staging sweep and missing repository id with corrupt records ------------------


def test_a_staging_directory_vanishing_during_the_sweep_does_not_abort_create(
    repo: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GoalStore(repo["project"])
    racing = repo["project"] / "goals" / ".staging-other-20261006T101500Z-abc"
    racing.mkdir(parents=True)
    real_stat = Path.stat

    def stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if self == racing:
            raise FileNotFoundError(self)  # renamed by its creator after iterdir
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    created = _enter(store, repo["linked"], "a-20261006T101500Z")
    assert created.revision == 1


def test_a_missing_repository_id_with_a_corrupt_record_is_refused(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    bad = _corrupt(repo["project"], record.id)
    id_file = repo["main"] / ".git" / "booley" / "repository-id"
    id_file.unlink()

    with pytest.raises(GoalRecordCorruptError, match=str(bad)):
        store.active_for_worktree(repo["linked"])
    with pytest.raises(GoalRecordCorruptError, match=str(bad)):
        store.identify_worktree(repo["linked"], create=True)
    assert not id_file.exists()
