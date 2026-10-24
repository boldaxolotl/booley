"""Goals that name Test Runs across entry, edits, approval, status and Finish (ADR 0072)."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.frozen_test_runs import resolve_goal_test_runs
from booley.flows.test_runs import build_test_run
from booley.goals.apply import ChangeEnvironment, recover
from booley.goals.change_service import create_proposal
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.goals.paths import record_paths
from booley.goals.status import GoalStatus, build_status, status_views
from booley.goals.store import GoalStore
from booley.goals.test_run_evidence import flow_test_runs
from booley.goals.test_run_freezing import ensure_test_run_resolutions
from tests.goals.conftest import (
    SIM_KEY,
    bind,
    enter_goals,
    entry_environment,
    fake_test_plan,
    git,
    run_row,
    write_tests_toml,
)
from tests.goals.test_finish import environment, request
from tests.goals.test_proposals import approve
from tests.goals.test_test_run_evidence import TWO_TESTS, A, B, _record, _substitute_contributor

pytestmark = pytest.mark.timeout(120)

VARIANTS = (
    b'[top]\ntests = ["smoke", "stress"]\n'
    b'[top.variants.stress]\ndefault = "short"\nshort = ["+n=1"]\nsoak = ["+n=9"]\n'
)
SEEDS_KEY = "sim_pass_top_run_737472657373_x"


def _mode(layout: SimpleNamespace, goals: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
    layout.record = enter_goals(layout, goals, **kwargs)
    git(layout.main, "config", "user.name", "Goal Test")
    git(layout.main, "config", "user.email", "goal@test.invalid")
    return layout


def _status(layout: SimpleNamespace, key: str) -> GoalStatus:
    store = GoalStore(layout.control)
    view = build_status(store, store.load(layout.record.id), work_dir=layout.worktree)
    return next(goal for goal in view.goals if goal.key == key)


def _names(layout: SimpleNamespace, key: str) -> list[str]:
    resolution = GoalStore(layout.control).load(layout.record.id).test_run_resolution(key)
    assert resolution is not None
    return [run.name for run in resolution.test_runs]


# --- Finish ----------------------------------------------------------------------


def test_finish_refuses_a_goal_whose_target_does_not_exist_yet(layout: SimpleNamespace) -> None:
    goals = [{"family": "SIM", "target": "top"}, {"family": "SIM", "target": "soon"}]
    mode = _mode(layout, goals, tests_toml=TWO_TESTS)
    _record(mode, "run-1", [run_row(A), run_row(B)])
    assert _status(mode, SIM_KEY).status == "met"
    assert _status(mode, "sim_pass_soon").reason.startswith("goal-test-run-pending: ")
    with pytest.raises(LifecycleError, match=r"must be met and fresh before finish: SIM:soon \("):
        finish_goal(request(mode), environment(mode))


def test_an_unresolved_goal_and_an_all_skipped_suite_are_unmet_for_their_own_reasons(
    layout: SimpleNamespace,
) -> None:
    mode = _mode(
        layout,
        [{"family": "SIM", "target": "top"}, {"family": "SIM", "target": "base"}],
        tests_toml=b'[top]\ntests = ["a"]\nskip = ["a"]\n[base]\ntests = ["a"]\n',
    )
    # ``top``: resolved, and it requires nothing. ``base``: as a record written before
    # Goals named Test Runs, with no resolution at all.
    raw = mode.record.to_json()
    del raw["test_run_resolutions"]["sim_pass_base"]
    record_paths(mode.control, mode.record.id).record_file.write_text(
        json.dumps(raw), encoding="utf-8"
    )
    assert _status(mode, SIM_KEY).reason.startswith("goal-simulation-suite-empty: ")
    assert _status(mode, "sim_pass_base").reason.startswith("goal-test-run-pending: ")
    with pytest.raises(LifecycleError, match=r"SIM:top \(all selected tests pass\), SIM:base \("):
        finish_goal(request(mode), environment(mode))
    # The unresolved Goal resolves on first use; the empty suite stays empty.
    ensure_test_run_resolutions(GoalStore(mode.control), mode.record.id, mode.worktree)
    assert _names(mode, "sim_pass_base") == ["a@1"]
    assert _names(mode, SIM_KEY) == []


@pytest.mark.timeout(150)
def test_finish_refuses_an_accumulated_goal_whose_contributor_was_substituted(
    layout: SimpleNamespace,
) -> None:
    from booley.criteria.evidence_ledger import AcceptanceLedgerError

    mode = _mode(layout, [{"family": "SIM", "target": "top"}], tests_toml=TWO_TESTS)
    _substitute_contributor(mode)
    with pytest.raises(
        AcceptanceLedgerError, match="transaction manifest does not bind its record"
    ):
        finish_goal(request(mode), environment(mode))


# --- Entry and frozen seeds ------------------------------------------------------


def test_goals_that_resolve_to_one_multi_seed_goal_merge_before_any_seed_is_drawn(
    layout: SimpleNamespace,
) -> None:
    """Through entry with the real plan: one draw of the larger N, never two draws."""
    goals = [
        {"family": "SIM", "target": "top", "test": "stress@x2", "origin": "a"},
        {"family": "SIM", "target": "::top:0#top", "test": "stress+short@x3", "origin": "b"},
    ]
    record = enter_goals(layout, goals, tests_toml=VARIANTS)
    (goal,) = record.goals
    assert goal.spec.origins == ("a", "b") and goal.spec.params["test"].endswith("@x3")
    (resolution,) = record.test_run_resolutions.values()
    # The test seed source counts from 101: two draws would have started at 103.
    assert [run.seed for run in resolution.test_runs] == [101, 102, 103]


def test_binding_runs_and_reading_status_never_redraw_frozen_seeds(
    layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.goals import test_run_freezing

    mode = _mode(
        layout, [{"family": "SIM", "target": "top", "test": "stress@x2"}], tests_toml=VARIANTS
    )
    frozen = _names(mode, SEEDS_KEY)

    def no_draw(count: int, *, exclude: Any = ()) -> tuple[int, ...]:
        raise AssertionError("a frozen Goal drew seeds again")

    monkeypatch.setattr(test_run_freezing, "draw_seeds", no_draw)
    write_tests_toml(mode, VARIANTS.replace(b'default = "short"', b'default = "soak"'))
    for run_id in ("run-1", "run-2"):
        bound = bind(mode, run_id)
        runs = flow_test_runs(
            GoalStore(mode.control).load(mode.record.id), dict(bound.spec_revisions)
        )
        assert runs.seeds({"top"}, "stress", "short", 2) == (101, 102)
    status_views(GoalStore(mode.control), mode.worktree)
    assert _names(mode, SEEDS_KEY) == frozen == ["stress+short@101", "stress+short@102"]


def test_an_unpinned_goal_stays_on_its_frozen_runs_after_default_edits(
    layout: SimpleNamespace,
) -> None:
    """A real ``tests.toml`` edit of ``default`` and ``default_seed``: status and the Flow."""
    mode = _mode(layout, [{"family": "SIM", "target": "top"}], tests_toml=VARIANTS)
    frozen = ["smoke@1", "stress+short@1"]
    assert _names(mode, SIM_KEY) == frozen
    edited = VARIANTS.replace(b'default = "short"', b'default = "soak"').replace(
        b"[top]\n", b"[top]\ndefault_seed = 5\n"
    )
    write_tests_toml(mode, edited)

    (view,) = status_views(GoalStore(mode.control), mode.worktree)
    (goal,) = view.goals
    assert goal.test_runs is not None and [run.name for run in goal.test_runs.runs] == frozen
    assert goal.reason == "no Test Run of the Goal has been recorded"  # not changed, not missing
    # The Flow runs the frozen runs for the Target, not the new defaults.
    record = GoalStore(mode.control).load(mode.record.id)
    plan = fake_test_plan(mode.worktree, "top")
    assert [run.name for run in plan.default_runs()] == ["smoke@5", "stress+soak@5"]
    runs, fresh = resolve_goal_test_runs(plan, None, flow_test_runs(record, {SIM_KEY: 1}), {"top"})
    assert ([run.name for run in runs], fresh) == (frozen, ())
    # Evidence of the frozen runs meets the Goal; the new defaults are diagnostic.
    stray = [run_row(build_test_run("smoke", seed=5))]
    assert _record(mode, "run-1", stray)["test_runs"] == []
    _record(
        mode, "run-2", [run_row(run) for run in record.test_run_resolutions[SIM_KEY].test_runs]
    )
    assert _status(mode, SIM_KEY).status == "met"


# --- An interrupted apply --------------------------------------------------------


def test_nothing_is_resolved_over_a_really_interrupted_apply(layout: SimpleNamespace) -> None:
    class _StopError(Exception):
        pass

    def stop(boundary: str) -> None:
        if boundary == "approved":
            raise _StopError(boundary)

    goals = [
        {"family": "SIM", "target": "top"},
        {"family": "SIM", "target": "top", "test": "later"},
    ]
    mode = _mode(layout, goals)
    later = "sim_pass_top_run_6c61746572_d"
    env = ChangeEnvironment(entry_environment(mode))
    arguments = {
        "kind": "add",
        "after": {"family": "SIM", "target": "top", "test": "smoke@7"},
        "rationale": "pin one seed",
    }
    view = create_proposal(env, mode.record.id, arguments, session_key="test-session")
    with pytest.raises(_StopError):
        approve(mode, replace(env, on_boundary=stop), view.proposal.id)

    # The decision is saved and its effects are not applied: the Test exists now, but
    # resolving would save over the apply.
    write_tests_toml(mode, b'[top]\ntests = ["smoke", "later"]\n')
    store = GoalStore(mode.control)
    before = store.load(mode.record.id)
    assert ensure_test_run_resolutions(store, mode.record.id, mode.worktree) == before
    assert before.test_run_resolution(later).pending == ("later",)

    recover(store, mode.record.id, env)
    resolved = ensure_test_run_resolutions(store, mode.record.id, mode.worktree)
    assert [run.name for run in resolved.test_run_resolution(later).test_runs] == ["later@1"]
    assert "sim_pass_top_run_736d6f6b65_s7" in resolved.test_run_resolutions


def test_a_target_removed_after_entry_reports_its_runs_as_gone(
    layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status of a Goal whose Target vanished says the runs are gone; it must not crash."""
    from tests.goals import conftest

    mode = _mode(layout, [{"family": "SIM", "target": "top", "test": "a"}], tests_toml=TWO_TESTS)
    monkeypatch.setattr(conftest, "KNOWN_TARGETS", frozenset({"base"}))
    reason = _status(mode, "sim_pass_top_run_61_d").reason  # key of Test ``a``
    assert reason.startswith("goal-test-run-missing: ") and "no longer exist" in reason
