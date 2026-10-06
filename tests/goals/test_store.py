"""The Goal Record store: worktree identity, locks, and revisioned writes (D3, D15)."""

from __future__ import annotations

import multiprocessing
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from booley.goals.model import GoalRecord, GoalState, WorktreeIdentity
from booley.goals.paths import record_paths
from booley.goals.store import (
    GoalLockTimeoutError,
    GoalRecordCorruptError,
    GoalRecordNotFoundError,
    GoalStore,
    GoalStoreError,
    InvalidRecordUpdateError,
    LockOrderError,
    StaleRevisionError,
    WorktreeIdentityError,
    WorktreeOccupiedError,
    resolve_worktree_identity,
)

SHA = "a" * 40
GIT_TIMEOUT_S = 30


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
    """A repository with one linked worktree, a symlink to it, and a Project dir."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "commit", "-q", "--allow-empty", "-m", "base")
    linked = tmp_path / "wt" / "uart"
    _git(main, "worktree", "add", "-q", str(linked), "-b", "work")
    alias = tmp_path / "alias"
    alias.symlink_to(linked, target_is_directory=True)
    project = tmp_path / "project" / ".booley_project"
    project.mkdir(parents=True)
    return {"main": main, "linked": linked, "alias": alias, "project": project}


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
    identity = resolve_worktree_identity(work_dir, create=True)
    assert identity is not None
    with store.worktree_lock(identity) as lock:
        return store.create(lock, _record(identity, goal_id, work_dir))


def _set_state(store: GoalStore, record: GoalRecord, state: GoalState) -> GoalRecord:
    with store.record_lock(record.id) as lock:
        return store.save(lock, replace(record, state=state))


# --- Worktree identity ------------------------------------------------------------


def test_identity_names_primary_and_linked_checkouts(repo: dict[str, Path]) -> None:
    main = resolve_worktree_identity(repo["main"], create=True)
    linked = resolve_worktree_identity(repo["linked"])
    assert main is not None and linked is not None
    assert main.checkout == "main"
    assert linked.checkout == "worktrees/uart"
    assert main.repository == linked.repository


def test_two_spellings_of_one_worktree_have_one_identity(repo: dict[str, Path]) -> None:
    direct = resolve_worktree_identity(repo["linked"], create=True)
    assert direct == resolve_worktree_identity(repo["alias"])
    assert direct == resolve_worktree_identity(repo["alias"] / ".")


def test_identity_is_absent_until_a_goal_mode_creates_it(repo: dict[str, Path]) -> None:
    assert resolve_worktree_identity(repo["linked"]) is None
    created = resolve_worktree_identity(repo["linked"], create=True)
    assert created is not None
    assert resolve_worktree_identity(repo["linked"], create=True) == created


def test_a_directory_outside_git_has_no_identity(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    with pytest.raises(WorktreeIdentityError, match="not a Git checkout"):
        resolve_worktree_identity(outside)


def test_a_damaged_repository_id_is_reported(repo: dict[str, Path]) -> None:
    resolve_worktree_identity(repo["main"], create=True)
    (repo["main"] / ".git" / "booley" / "repository-id").write_text("garbage\n")
    with pytest.raises(WorktreeIdentityError, match="unreadable repository identity"):
        resolve_worktree_identity(repo["linked"])


# --- Create, load, occupancy --------------------------------------------------------


def test_create_publishes_revision_one_and_load_reads_it(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    created = _enter(store, repo["linked"], "fix-uart-20261006T101500Z")

    assert created.revision == 1
    assert store.load(created.id) == created
    assert store.active_for_worktree(repo["linked"]) == created
    assert store.list_active() == (created,)
    assert store.active_for_worktree(repo["main"]) is None


def test_second_occupying_record_for_one_worktree_is_refused(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    first = _enter(store, repo["linked"], "fix-uart-20261006T101500Z")

    with pytest.raises(WorktreeOccupiedError) as caught:
        _enter(store, repo["alias"], "other-20261006T101600Z")  # same worktree, other spelling
    assert caught.value.occupant == first
    assert [r.id for r in store.list_records()] == [first.id]


def test_a_different_worktree_may_enter_alongside(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    _enter(store, repo["linked"], "fix-uart-20261006T101500Z")
    _enter(store, repo["main"], "other-20261006T101600Z")
    assert len(store.list_active()) == 2


def test_failed_record_is_replaceable_but_terminal(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    failed = _set_state(
        store, _enter(store, repo["linked"], "a-20261006T101500Z"), GoalState.FAILED
    )
    assert store.active_for_worktree(repo["linked"]) is None

    replacement = _enter(store, repo["linked"], "b-20261006T101600Z")
    assert store.active_for_worktree(repo["linked"]) == replacement
    with pytest.raises(InvalidRecordUpdateError, match="cannot move from failed to active"):
        _set_state(store, failed, GoalState.ACTIVE)


@pytest.mark.parametrize("state", [GoalState.ACTIVE, GoalState.FINISHING])
def test_every_occupying_state_blocks_a_second_entry(
    repo: dict[str, Path], state: GoalState
) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    record = _set_state(store, record, GoalState.ACTIVE)
    if state is GoalState.FINISHING:
        _set_state(store, record, state)
    with pytest.raises(WorktreeOccupiedError):
        _enter(store, repo["linked"], "b-20261006T101600Z")


def test_create_requires_a_new_entering_record_under_its_own_lock(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    identity = resolve_worktree_identity(repo["linked"], create=True)
    other = resolve_worktree_identity(repo["main"])
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


def _race_create(project: str, work_dir: str, goal_id: str, start: Any, results: Any) -> None:
    store = GoalStore(Path(project))
    start.wait(GIT_TIMEOUT_S)
    try:
        _enter(store, Path(work_dir), goal_id)
        results.put(("won", goal_id))
    except WorktreeOccupiedError:
        results.put(("refused", goal_id))


def test_concurrent_entries_from_two_processes_yield_one_winner(repo: dict[str, Path]) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    spellings = (repo["linked"], repo["alias"])
    workers = [
        context.Process(
            target=_race_create,
            args=(str(repo["project"]), str(path), f"race{i}-20261006T101500Z", start, results),
        )
        for i, path in enumerate(spellings)
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = sorted(results.get(timeout=60)[0] for _ in workers)
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0

    assert outcomes == ["refused", "won"]
    assert len(GoalStore(repo["project"]).list_active()) == 1


# --- Save --------------------------------------------------------------------------


def test_save_is_compare_and_swap_on_revision(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    read = _enter(store, repo["linked"], "a-20261006T101500Z")
    saved = _set_state(store, read, GoalState.ACTIVE)
    assert saved.revision == 2

    with pytest.raises(StaleRevisionError, match="revision 2, not the revision 1"):
        _set_state(store, read, GoalState.ABANDONED)
    assert store.load(read.id) == saved


def test_save_refuses_a_worktree_change_and_an_unheld_lock(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    moved = replace(record, worktree=WorktreeIdentity(record.worktree.repository, "main"))
    with store.record_lock(record.id) as lock, pytest.raises(InvalidRecordUpdateError):
        store.save(lock, moved)
    with store.record_lock(record.id) as lock:
        pass
    with pytest.raises(GoalStoreError, match="held record lock"):
        store.save(lock, record)


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


def test_a_held_lock_times_out_instead_of_waiting_forever(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    impatient = GoalStore(repo["project"], lock_timeout_s=0.2)
    with (
        store.record_lock(record.id),
        pytest.raises(GoalLockTimeoutError),
        impatient.record_lock(record.id),
    ):
        pass


# --- Corruption --------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"", b"\xff\xfe", b'{"schema": 1}', b"[]"],
)
def test_corrupt_record_is_reported_not_treated_as_absent(
    repo: dict[str, Path], content: bytes
) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    record_file = record_paths(repo["project"], record.id).record_file
    record_file.write_bytes(content)

    with pytest.raises(GoalRecordCorruptError) as caught:
        store.load(record.id)
    assert caught.value.path == record_file
    with pytest.raises(GoalRecordCorruptError):
        store.active_for_worktree(repo["linked"])
    with pytest.raises(GoalRecordCorruptError):
        _enter(store, repo["linked"], "b-20261006T101600Z")


def test_record_directory_without_record_file_is_corrupt(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    (repo["project"] / "goals" / "lost-20261006T101500Z").mkdir(parents=True)
    with pytest.raises(GoalRecordCorruptError, match=r"record\.json"):
        store.list_records()


def test_record_naming_another_id_is_corrupt(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    record = _enter(store, repo["linked"], "a-20261006T101500Z")
    other = record_paths(repo["project"], "b-20261006T101600Z").root
    record_paths(repo["project"], record.id).root.rename(other)
    with pytest.raises(GoalRecordCorruptError, match="names Goal Record a-20261006T101500Z"):
        store.load("b-20261006T101600Z")


def test_missing_record_and_non_record_directories(repo: dict[str, Path]) -> None:
    store = GoalStore(repo["project"])
    assert store.list_records() == ()
    (repo["project"] / "goals" / "locks").mkdir(parents=True)
    (repo["project"] / "goals" / "history").mkdir()
    (repo["project"] / "goals" / ".staging-x").mkdir()
    assert store.list_records() == ()
    with pytest.raises(GoalRecordNotFoundError):
        store.load("a-20261006T101500Z")
