"""Ended Goal Records: the ``goal status`` fallback and their automatic deletion (#1561)."""

from __future__ import annotations

import asyncio
import json
import logging
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from mcp.types import TextContent

from booley.core.file_lock import acquire_file_lock, release_file_lock
from booley.goals import ended_records
from booley.goals.abandon import abandon_goal
from booley.goals.apply import ChangeEnvironment
from booley.goals.checkout import CheckoutError, GoalCheckout
from booley.goals.ended_records import (
    DELETING_PREFIX,
    last_ended_record,
    sweep_ended_records,
)
from booley.goals.entry import EntryEnvironment
from booley.goals.finish import FinishEnvironment, finish_goal
from booley.goals.lifecycle import LifecycleRequest
from booley.goals.model import GoalRecord, GoalState, WorktreeIdentity
from booley.goals.paths import goals_root, locks_dir, record_paths, worktree_lock_file
from booley.goals.store import (
    CorruptRecord,
    GoalRecordCorruptError,
    GoalStore,
    resolve_worktree_identity,
)
from booley.harness.booley import _cmd_goal
from booley.mcp.application import McpDispatchResult
from booley.mcp.goal_tools import dispatch_goal_tool
from tests.goals.conftest import enter_goals, git, write_project_files
from tests.goals.test_status import publish

# Entry, Finish, and Abandon run real Git; keep the established Goal lifecycle budget.
pytestmark = pytest.mark.timeout(120)

GONE_BRANCH = "goal/gone-20261001T000000Z"


@pytest.fixture(autouse=True)
def _frozen_baseline_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("booley.mcp.sim_baseline_jobs.schedule_sim_baselines", lambda *_a: None)


def finish_environment(layout: SimpleNamespace) -> FinishEnvironment:
    return FinishEnvironment(ChangeEnvironment(EntryEnvironment(layout.control)))


def ended_copy(
    layout: SimpleNamespace,
    goal_id: str,
    *,
    state: GoalState = GoalState.ABANDONED,
    worktree_path: Path | None = None,
    branch: str = GONE_BRANCH,
    worktree: str | None = "gone",
    ended_at: str = "2026-10-01T00:00:00Z",
    failure: str | None = None,
) -> GoalRecord:
    """Write a copy of *layout*'s record as Goal Record *goal_id*.

    By default it is abandoned, on a missing branch, and bound to linked
    worktree *worktree*, which Git creates and removes here; ``None`` keeps
    the live worktree's identity. *worktree_path* defaults to where that
    worktree was.
    """
    source = GoalStore(layout.control).load(layout.record.id)
    identity, path = source.worktree, Path(source.worktree_path)
    if worktree is not None:
        identity, path = removed_worktree(layout, worktree)
    record = replace(
        source,
        id=goal_id,
        state=state,
        worktree=identity,
        worktree_path=str(worktree_path or path),
        branch=branch,
        ended_at=None if state in {GoalState.ACTIVE, GoalState.FINISHING} else ended_at,
        failure=failure,
    )
    root = record_paths(layout.control, goal_id).root
    root.mkdir()
    (root / "record.json").write_text(json.dumps(record.to_json()), encoding="utf-8")
    return record


def removed_worktree(layout: SimpleNamespace, name: str) -> tuple[WorktreeIdentity, Path]:
    """The identity and path of linked worktree *name*, added and removed with Git."""
    path = layout.control / "worktrees" / name
    git(layout.main, "worktree", "add", "-q", "--detach", str(path))
    identity = resolve_worktree_identity(path)
    git(layout.main, "worktree", "remove", "--force", str(path))
    assert identity is not None and identity.checkout == f"worktrees/{name}"
    return identity, path


def remove_worktree(layout: SimpleNamespace, record: GoalRecord) -> None:
    """Remove *record*'s worktree and Goal Branch, as a human cleaning up would."""
    git(layout.main, "worktree", "remove", "--force", str(layout.worktree))
    git(layout.main, "branch", "-D", record.branch)


def second_worktree(layout: SimpleNamespace, name: str = "other") -> SimpleNamespace:
    """Another linked worktree of *layout*'s Project, without a Goal Mode."""
    worktree = layout.control / "worktrees" / name
    git(layout.main, "worktree", "add", "-q", "-b", name, str(worktree))
    write_project_files(worktree / ".booley")
    return SimpleNamespace(main=layout.main, control=layout.control, worktree=worktree)


def exists(layout: SimpleNamespace, goal_id: str) -> bool:
    return record_paths(layout.control, goal_id).root.exists()


# ---------------------------------------------------------------------------
# goal status
# ---------------------------------------------------------------------------


def cli_status(location: Path, main: Path, monkeypatch, capsys) -> str:
    monkeypatch.chdir(location)
    assert _cmd_goal(Namespace(goal_command="status", short=False, long=False), main) == 0
    return capsys.readouterr().out


def mcp_status(location: Path, control: Path) -> str:
    result = asyncio.run(
        dispatch_goal_tool("goal_status", {"work_dir": str(location)}, booley_dir=control)
    )
    assert isinstance(result, McpDispatchResult) and result.is_error is False
    contents, _structured = cast("tuple[list[TextContent], object]", result.value)
    return contents[0].text


def test_status_shows_the_abandoned_record_with_its_files(goal_mode, monkeypatch, capsys):
    store = GoalStore(goal_mode.control)
    assert abandon_goal(store, goal_mode.worktree, finish_environment(goal_mode))
    paths = record_paths(goal_mode.control, goal_mode.record.id)
    paths.summary_file.write_text("summary\n", encoding="utf-8")
    paths.review_package_file.write_text("{}\n", encoding="utf-8")
    ended = store.load(goal_mode.record.id).ended_at
    assert ended is not None
    text = cli_status(goal_mode.worktree, goal_mode.main, monkeypatch, capsys)
    lines = text.splitlines()
    assert lines[0] == "No active Goal Mode."
    assert lines[2] == f"Last ended Goal Mode in this worktree: {goal_mode.record.id} (abandoned)"
    assert lines[3].startswith("Ended: ") and lines[3].endswith(ended[:4])
    assert f"Session Summary: {paths.summary_file}" in lines
    assert f"Review Package: {paths.review_package_file}" in lines
    assert mcp_status(goal_mode.worktree, goal_mode.control) + "\n" == text


def test_status_shows_the_latest_ended_record_and_its_failure(goal_mode, monkeypatch, capsys):
    store = GoalStore(goal_mode.control)
    assert abandon_goal(store, goal_mode.worktree, finish_environment(goal_mode))
    ended_copy(goal_mode, "old-20200101T000000Z", worktree=None, ended_at="2020-01-01T00:00:00Z")
    ended_copy(
        goal_mode,
        "late-20990101T000000Z",
        state=GoalState.FAILED,
        worktree=None,
        ended_at="2099-01-01T00:00:00Z",
        failure="entry failed: boom",
    )
    latest = last_ended_record(store, goal_mode.worktree)
    assert latest is not None and latest.id == "late-20990101T000000Z"
    text = cli_status(goal_mode.worktree, goal_mode.main, monkeypatch, capsys)
    assert "late-20990101T000000Z (failed)" in text
    assert "Failure: entry failed: boom" in text
    assert "Session Summary" not in text and "Review Package" not in text
    assert mcp_status(goal_mode.worktree, goal_mode.control) + "\n" == text


def test_status_of_an_active_or_recordless_worktree_is_unchanged(goal_mode, monkeypatch, capsys):
    # An ended record of another worktree is not this worktree's fallback.
    ended_copy(goal_mode, "old-20200101T000000Z")
    text = cli_status(goal_mode.worktree, goal_mode.main, monkeypatch, capsys)
    assert goal_mode.record.id in text and "Last ended" not in text
    other = second_worktree(goal_mode)
    assert cli_status(other.worktree, other.main, monkeypatch, capsys).startswith(
        "No Goal Mode is active here."
    )
    assert "Last ended" not in mcp_status(other.worktree, other.control)


# ---------------------------------------------------------------------------
# Deletion after entry, finish, and abandonment
# ---------------------------------------------------------------------------


def test_entry_deletes_a_record_whose_worktree_and_branch_are_gone(goal_mode, monkeypatch):
    store = GoalStore(goal_mode.control)
    abandon_goal(store, goal_mode.worktree, finish_environment(goal_mode))
    record = store.load(goal_mode.record.id)
    lock = worktree_lock_file(goal_mode.control, record.worktree)
    assert lock.is_file()
    remove_worktree(goal_mode, record)
    other = second_worktree(goal_mode)
    monkeypatch.setattr("booley.goals.entry.compact_utc_now", lambda: "20261007T120000Z")
    entered = enter_goals(other)
    assert not exists(goal_mode, record.id)
    assert not lock.exists()
    assert exists(goal_mode, entered.id)
    assert worktree_lock_file(goal_mode.control, entered.worktree).is_file()


def test_finish_deletes_a_record_whose_worktree_and_branch_are_gone(layout, monkeypatch, capsys):
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    publish(layout)
    git(layout.main, "config", "user.name", "Goal Test")
    git(layout.main, "config", "user.email", "goal@test.invalid")
    stale = ended_copy(layout, "old-20200101T000000Z")
    request = LifecycleRequest(
        layout.worktree, layout.record.id, str(uuid4()), summary="Lint is clean."
    )
    assert finish_goal(request, finish_environment(layout))["status"] == "finished"
    assert not exists(layout, stale.id)
    assert exists(layout, layout.record.id)
    text = cli_status(layout.worktree, layout.main, monkeypatch, capsys)
    paths = record_paths(layout.control, layout.record.id)
    assert f"{layout.record.id} (finished)" in text
    ended_at = GoalStore(layout.control).load(layout.record.id).ended_at
    from booley.runtime.timefmt import format_human_datetime_safe

    assert ended_at is not None
    assert f"Ended: {format_human_datetime_safe(ended_at)}" in text
    assert f"Session Summary: {paths.summary_file}" in text
    assert f"Review Package: {paths.review_package_file}" in text
    assert mcp_status(layout.worktree, layout.control) + "\n" == text


def test_abandon_deletes_a_record_whose_worktree_and_branch_are_gone(goal_mode):
    stale = ended_copy(goal_mode, "old-20200101T000000Z")
    store = GoalStore(goal_mode.control)
    result = abandon_goal(store, goal_mode.worktree, finish_environment(goal_mode))
    assert result["status"] == "abandoned"
    assert not exists(goal_mode, stale.id)
    assert store.load(goal_mode.record.id).state is GoalState.ABANDONED


def test_cleanup_failure_does_not_fail_abandonment(goal_mode, monkeypatch, caplog):
    stale = ended_copy(goal_mode, "old-20200101T000000Z")

    def broken(*_args):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ended_records, "_sweep", broken)
    store = GoalStore(goal_mode.control)
    with caplog.at_level(logging.WARNING, logger=ended_records.__name__):
        result = abandon_goal(store, goal_mode.worktree, finish_environment(goal_mode))
    assert result["status"] == "abandoned"
    assert exists(goal_mode, stale.id)
    assert "disk on fire" in caplog.text


# ---------------------------------------------------------------------------
# What the sweep keeps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["active", "finishing", "branch exists", "branch unknown"])
def test_sweep_keeps_records_that_may_still_matter(goal_mode, monkeypatch, case):
    options: dict = {}
    if case in {"active", "finishing"}:
        options["state"] = GoalState(case)
    elif case == "branch exists":
        options["branch"] = "work"
    else:

        def unknown(_self, branch, **_kwargs):
            raise CheckoutError(f"cannot tell whether branch {branch} exists")

        monkeypatch.setattr(GoalCheckout, "has_branch", unknown)
    stale = ended_copy(goal_mode, "old-20200101T000000Z", **options)
    lock = worktree_lock_file(goal_mode.control, stale.worktree)
    lock.touch()
    assert sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree) == ()
    assert exists(goal_mode, stale.id) and lock.exists()


def test_worktree_known_to_git_keeps_its_record_whatever_its_path(goal_mode, tmp_path):
    # The Sandbox mounts every checkout at one path: the identity decides, not the path.
    stale = ended_copy(
        goal_mode, "old-20200101T000000Z", worktree=None, worktree_path=tmp_path / "nowhere"
    )
    assert sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree) == ()
    assert exists(goal_mode, stale.id)


def test_removed_worktree_is_gone_even_when_its_path_exists(goal_mode):
    # Like ``/work`` in the Sandbox: the recorded path exists, the worktree does not.
    stale = ended_copy(goal_mode, "old-20200101T000000Z", worktree_path=goal_mode.worktree)
    assert sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree) == (stale.id,)


def test_primary_checkout_record_is_kept(goal_mode):
    stale = ended_copy(goal_mode, "old-20200101T000000Z", worktree=None)
    main = replace(stale, worktree=WorktreeIdentity(stale.worktree.repository, "main"))
    record_paths(goal_mode.control, stale.id).record_file.write_text(
        json.dumps(main.to_json()), encoding="utf-8"
    )
    assert sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree) == ()


def test_sweep_keeps_a_record_whose_lock_is_held(goal_mode):
    stale = ended_copy(goal_mode, "old-20200101T000000Z")
    with record_paths(goal_mode.control, stale.id).lock_file.open("a+") as handle:
        acquire_file_lock(handle)
        try:
            assert sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree) == ()
        finally:
            release_file_lock(handle)
    assert exists(goal_mode, stale.id)
    assert sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree) == (stale.id,)


def test_corrupt_record_is_kept_and_keeps_every_worktree_lock(goal_mode):
    stale = ended_copy(goal_mode, "old-20200101T000000Z")
    lock = worktree_lock_file(goal_mode.control, stale.worktree)
    lock.touch()
    corrupt = record_paths(goal_mode.control, "bad-20200101T000000Z").root
    corrupt.mkdir()
    (corrupt / "record.json").write_bytes(b"{bad")
    assert sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree) == (stale.id,)
    assert corrupt.is_dir() and lock.exists()


@pytest.mark.parametrize("after_release", [False, True], ids=["posix", "windows-order"])
def test_worktree_lock_is_removed_only_when_unshared(goal_mode, monkeypatch, after_release):
    monkeypatch.setattr(ended_records, "_AFTER_RELEASE", after_release)
    unshared = ended_copy(goal_mode, "gone-20200101T000000Z")
    shared = ended_copy(goal_mode, "shared-20200101T000000Z", worktree="shared")
    # A second record of the removed "shared" worktree is kept: its branch still exists.
    kept = ended_copy(goal_mode, "kept-20200101T000000Z", worktree="shared", branch="work")
    assert kept.worktree == shared.worktree
    unshared_lock = worktree_lock_file(goal_mode.control, unshared.worktree)
    shared_lock = worktree_lock_file(goal_mode.control, shared.worktree)
    unshared_lock.touch()
    shared_lock.touch()
    deleted = sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree)
    assert set(deleted) == {unshared.id, shared.id}
    assert exists(goal_mode, kept.id)
    assert not unshared_lock.exists()
    assert shared_lock.is_file()


def test_sweep_leaves_history_and_other_files_and_removes_leftovers(goal_mode):
    history = goals_root(goal_mode.control) / "history" / "old-20200101T000000Z.md"
    history.parent.mkdir()
    history.write_text("committed summary\n", encoding="utf-8")
    other_lock = locks_dir(goal_mode.control) / "unrelated.lock"
    other_lock.touch()
    interrupted = ended_copy(goal_mode, "interrupted-20200101T000000Z")
    root = record_paths(goal_mode.control, interrupted.id).root
    leftover = root.with_name(f"{DELETING_PREFIX}{interrupted.id}")
    root.rename(leftover)
    ended_copy(goal_mode, "old-20200101T000000Z")
    sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree)
    assert history.is_file() and other_lock.is_file()
    assert not leftover.exists()
    assert not any(
        entry.name.startswith(DELETING_PREFIX) for entry in goals_root(goal_mode.control).iterdir()
    )


@pytest.mark.parametrize("error", ["not found", "corrupt"])
def test_listing_skips_a_record_deleted_while_it_is_read(goal_mode, monkeypatch, error):
    stale = ended_copy(goal_mode, "old-20200101T000000Z")
    load = GoalStore.load

    def vanishing(self, goal_id):
        if goal_id != stale.id:
            return load(self, goal_id)
        root = record_paths(goal_mode.control, goal_id).root
        root.rename(root.with_name(f"{DELETING_PREFIX}{goal_id}"))
        if error == "corrupt":  # the directory went between the existence check and the read
            raise GoalRecordCorruptError([CorruptRecord(root / "record.json", "no such file")])
        return load(self, goal_id)

    monkeypatch.setattr(GoalStore, "load", vanishing)
    scan = GoalStore(goal_mode.control).list_records()
    assert [record.id for record in scan.records] == [goal_mode.record.id]
    assert scan.corrupt == ()


def test_has_branch_tells_present_absent_and_unknown(goal_mode, tmp_path):
    checkout = GoalCheckout(goal_mode.worktree)
    assert checkout.has_branch("work") is True
    assert checkout.has_branch("goal/missing") is False
    with pytest.raises(CheckoutError, match="cannot tell"):
        GoalCheckout(tmp_path).has_branch("work")


@pytest.mark.parametrize("interruption", ["crash", "busy lock"])
def test_sweep_retries_interrupted_worktree_lock_cleanup(goal_mode, monkeypatch, interruption):
    stale = ended_copy(goal_mode, "old-20200101T000000Z")
    store = GoalStore(goal_mode.control)
    lock = worktree_lock_file(goal_mode.control, stale.worktree)
    lock.touch()
    hidden = goals_root(goal_mode.control) / f"{DELETING_PREFIX}{stale.id}"
    if interruption == "crash":

        def interrupted(*_args):
            raise RuntimeError("interrupted after rename")

        with monkeypatch.context() as patch:
            patch.setattr(ended_records, "_remove_worktree_lock", interrupted)
            sweep_ended_records(store, goal_mode.worktree)
    else:
        with store.worktree_lock(stale.worktree):
            sweep_ended_records(store, goal_mode.worktree)
    assert lock.exists() and hidden.is_dir()
    assert not exists(goal_mode, stale.id)
    sweep_ended_records(store, goal_mode.worktree)
    assert not lock.exists() and not hidden.exists()


def test_sweep_bounds_git_waits_across_records(goal_mode, monkeypatch):
    first = ended_copy(goal_mode, "first-20200101T000000Z")
    second = ended_copy(goal_mode, "second-20200101T000000Z")
    elapsed = 0.0
    waits = []

    def stalled_git(_self, *_args, timeout_s, **_kwargs):
        nonlocal elapsed
        waits.append(timeout_s)
        elapsed += timeout_s
        raise CheckoutError("git timed out")

    monkeypatch.setattr(ended_records, "monotonic", lambda: elapsed)
    monkeypatch.setattr(GoalCheckout, "_git", stalled_git)
    assert sweep_ended_records(GoalStore(goal_mode.control), goal_mode.worktree) == ()
    assert len(waits) == 1 and 0 < sum(waits) <= 1.0
    assert exists(goal_mode, first.id) and exists(goal_mode, second.id)
