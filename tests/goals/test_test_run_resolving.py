"""Deferred resolution of a Goal's Test Runs: one atomic record save (ADR 0072)."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from booley.goals.changes import read_change_log
from booley.goals.model import GoalRecord
from booley.goals.paths import record_paths
from booley.goals.status import select_status
from booley.goals.store import GoalStore
from booley.goals.test_run_freezing import ensure_test_run_resolutions
from booley.goals.test_run_resolution import SUITE_SLOT
from tests.goals.conftest import SIM_KEY, bind, enter_goals, write_tests_toml

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)

LATER_KEY = "sim_pass_top_run_6c61746572_x"
GOALS = (
    {"family": "SIM", "target": "top"},
    {"family": "SIM", "target": "top", "test": "later@x2"},
)
GROWN = b'[top]\ntests = ["smoke", "later"]\n'


class _InterruptedError(Exception):
    """Interrupt a resolution at a checkpoint."""


def _store(layout: SimpleNamespace) -> GoalStore:
    return GoalStore(layout.control)


def _load(layout: SimpleNamespace, record: GoalRecord) -> GoalRecord:
    return _store(layout).load(record.id)


def _names(record: GoalRecord, key: str) -> list[str]:
    resolution = record.test_run_resolution(key)
    assert resolution is not None
    return [run.name for run in resolution.test_runs]


def _draw(*seeds: int) -> Any:
    pending = list(seeds)
    return lambda count, *, exclude=(): tuple(pending.pop(0) for _ in range(count))


@pytest.fixture
def waiting(layout: SimpleNamespace) -> SimpleNamespace:
    """An active Goal Mode whose Test Goal names a Test that does not exist yet."""
    record = enter_goals(layout, GOALS)
    assert record.test_run_resolution(LATER_KEY).pending == ("later@x2",)
    return SimpleNamespace(**vars(layout), record=record)


def test_nothing_resolvable_saves_nothing(waiting: SimpleNamespace) -> None:
    record = ensure_test_run_resolutions(_store(waiting), waiting.record.id, waiting.worktree)
    assert record == waiting.record  # same revision: no save happened


def test_pending_test_resolves_once_with_one_save(waiting: SimpleNamespace) -> None:
    write_tests_toml(waiting, GROWN)
    store = _store(waiting)
    saved = ensure_test_run_resolutions(
        store, waiting.record.id, waiting.worktree, draw=_draw(5, 6)
    )
    assert saved.revision == waiting.record.revision + 1
    assert _names(saved, LATER_KEY) == ["later@5", "later@6"]
    # The Target Goal gained the new Test, and its frozen run did not move.
    assert _names(saved, SIM_KEY) == ["smoke@1", "later@1"]
    deferred = [item.run.name for item in saved.test_run_resolutions[SIM_KEY].deferred]
    assert deferred == ["later@1"]
    # A second resolver finds nothing to do and draws nothing.
    again = ensure_test_run_resolutions(store, saved.id, waiting.worktree, draw=_draw())
    assert again == saved
    # The Change Log is not used: nothing there for recovery or the barrier to read.
    log = read_change_log(record_paths(waiting.control, saved.id).root / "changes.jsonl")
    assert (log.applied, log.interrupted) == ((), ())


@pytest.mark.parametrize("boundary", ["before:save", "after:save"])
def test_a_crash_leaves_the_old_or_the_new_record(waiting: SimpleNamespace, boundary: str) -> None:
    write_tests_toml(waiting, GROWN)
    store = _store(waiting)

    def stop(name: str) -> None:
        if name == boundary:
            raise _InterruptedError

    with pytest.raises(_InterruptedError):
        ensure_test_run_resolutions(
            store, waiting.record.id, waiting.worktree, draw=_draw(5, 6), checkpoint=stop
        )
    crashed = _load(waiting, waiting.record)
    if boundary == "before:save":
        assert crashed == waiting.record  # nothing was observable: the next call draws again
        expected = ["later@8", "later@9"]
    else:
        assert _names(crashed, LATER_KEY) == ["later@5", "later@6"]
        expected = ["later@5", "later@6"]
    final = ensure_test_run_resolutions(store, crashed.id, waiting.worktree, draw=_draw(8, 9))
    assert _names(final, LATER_KEY) == expected


def test_the_loser_of_two_resolvers_keeps_the_winners_seeds(waiting: SimpleNamespace) -> None:
    write_tests_toml(waiting, GROWN)
    store = _store(waiting)
    raced: list[GoalRecord] = []

    def other_resolver_saves_first(name: str) -> None:
        if name == "before:save" and not raced:
            raced.append(
                ensure_test_run_resolutions(
                    store, waiting.record.id, waiting.worktree, draw=_draw(1, 2)
                )
            )

    final = ensure_test_run_resolutions(
        store,
        waiting.record.id,
        waiting.worktree,
        draw=_draw(8, 9),
        checkpoint=other_resolver_saves_first,
    )
    # The loser hit a stale revision, re-read, and found the slots filled.
    assert final == raced[0]
    assert _names(final, LATER_KEY) == ["later@1", "later@2"]


def test_resolution_waits_for_an_interrupted_apply(
    waiting: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.goals import test_run_freezing

    write_tests_toml(waiting, GROWN)
    with monkeypatch.context() as patch:
        patch.setattr(test_run_freezing, "pending_applies", lambda _root: ("proposal-1",))
        record = ensure_test_run_resolutions(_store(waiting), waiting.record.id, waiting.worktree)
    assert record == waiting.record
    resolved = ensure_test_run_resolutions(
        _store(waiting), waiting.record.id, waiting.worktree, draw=_draw(5, 6)
    )
    assert _names(resolved, LATER_KEY) == ["later@5", "later@6"]


def test_pending_target_resolves_when_the_target_first_exists(
    layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.goals import conftest

    record = enter_goals(layout, [*GOALS, {"family": "SIM", "target": "soon"}])
    assert record.test_run_resolution("sim_pass_soon").pending == (SUITE_SLOT,)
    monkeypatch.setattr(conftest, "KNOWN_TARGETS", frozenset({"top", "base", "soon"}))
    write_tests_toml(layout, b'[top]\ntests = ["smoke"]\n[soon]\ntests = ["a", "b"]\n')
    resolved = ensure_test_run_resolutions(_store(layout), record.id, layout.worktree)
    assert _names(resolved, "sim_pass_soon") == ["a@1", "b@1"]
    assert resolved.test_run_resolution("sim_pass_soon").pending == ()


def test_binding_a_run_and_reading_status_resolve_pending_slots(
    waiting: SimpleNamespace,
) -> None:
    write_tests_toml(waiting, GROWN)
    binding = bind(waiting)
    bound = _load(waiting, waiting.record)
    assert binding.record_revision == bound.revision == waiting.record.revision + 1
    assert len(_names(bound, LATER_KEY)) == 2
    write_tests_toml(waiting, b'[top]\ntests = ["smoke", "later", "third"]\n')
    (view,) = select_status(_store(waiting), waiting.worktree).views
    assert _names(view.record, SIM_KEY) == ["smoke@1", "later@1", "third@1"]


def test_a_record_created_before_this_change_resolves_on_first_use(
    goal_mode: SimpleNamespace,
) -> None:
    """An upgraded record has no resolutions: every Goal is pending until first use."""
    store = _store(goal_mode)
    paths = record_paths(goal_mode.control, goal_mode.record.id)
    raw = goal_mode.record.to_json()
    del raw["test_run_resolutions"]
    paths.record_file.write_text(json.dumps(raw), encoding="utf-8")
    upgraded = store.load(goal_mode.record.id)
    assert upgraded.test_run_resolution(SIM_KEY) is None
    resolved = ensure_test_run_resolutions(store, upgraded.id, goal_mode.worktree)
    assert _names(resolved, SIM_KEY) == ["smoke@1"]
    assert [item.resolved_by for item in resolved.test_run_resolutions[SIM_KEY].runs] == [
        "deferred"
    ]


def test_a_goal_its_target_refuses_stays_pending(waiting: SimpleNamespace) -> None:
    write_tests_toml(waiting, b'[top]\ntests = ["smoke", "later", "a+b"]\n')  # malformed
    record = ensure_test_run_resolutions(_store(waiting), waiting.record.id, waiting.worktree)
    assert record.test_run_resolution(LATER_KEY).pending == ("later@x2",)
    assert replace(record, revision=0) == replace(waiting.record, revision=0)


def test_a_refused_goal_says_why_instead_of_waiting_for_its_test(
    waiting: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken ``tests.toml`` is its own reason, not "the Test does not exist yet"."""
    from booley.goals.status import build_status

    def reason() -> str:
        view = build_status(_store(waiting), waiting.record, work_dir=waiting.worktree)
        return next(goal for goal in view.goals if goal.key == LATER_KEY).reason

    assert reason().startswith("goal-test-run-pending: ")
    write_tests_toml(waiting, b'[top]\ntests = ["smoke", "later", "a+b"]\n')  # malformed
    with caplog.at_level("WARNING"):
        ensure_test_run_resolutions(_store(waiting), waiting.record.id, waiting.worktree)
    assert f"Test Runs of Goal {LATER_KEY} on Target top stay pending" in caplog.text
    assert reason().startswith("goal-test-run-unresolvable: the Test Runs of Target 'top'")
    # The Test exists now, but the Target cannot apply the seeds the Goal needs.
    write_tests_toml(waiting, b'[top]\ntests = ["smoke", "later"]\n')
    from booley.flows import target_test_plan_loading

    def unseeded(work_dir: Any, target: str) -> Any:
        return target_test_plan_loading.load_target_test_plan(
            work_dir, target, identity=target, adapter="icarus"
        )

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(target_test_plan_loading, "load_checkout_target_test_plan", unseeded)
        assert reason().startswith("goal-test-run-unresolvable: ")
        assert "seed" in reason()


@pytest.mark.parametrize("error", ["stale", "timeout"])
def test_a_status_read_never_fails_because_its_resolution_could_not_be_saved(
    waiting: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    from booley.goals import status
    from booley.goals.store import GoalLockTimeoutError, StaleRevisionError

    write_tests_toml(waiting, b'[top]\ntests = ["smoke", "later"]\n')
    raised = (
        StaleRevisionError("kept changing") if error == "stale" else GoalLockTimeoutError("busy")
    )

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(status, "ensure_test_run_resolutions", refuse)
    (view,) = status.status_views(_store(waiting), waiting.worktree)
    goal = next(goal for goal in view.goals if goal.key == LATER_KEY)
    assert goal.status == "unmet" and goal.reason.startswith("goal-test-run-pending: ")
    assert goal.test_runs is not None and goal.test_runs.pending == ("later@x2",)
