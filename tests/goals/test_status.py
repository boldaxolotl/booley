"""Status reads declared Goals, fences identities, and detects fresh edits every time."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from booley.criteria.state import DevelopmentState
from booley.goals.format import render_status
from booley.goals.paths import record_paths
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.state_store import GoalStateError
from booley.goals.status import build_status, status_views
from booley.goals.store import GoalStore
from tests.goals.conftest import LINT_KEY, bind, bump_spec, edit_protected, git


def publish(layout: SimpleNamespace) -> None:
    recorder = GoalEvidenceRecorder(bind(layout))
    paths = record_paths(layout.control, layout.record.id)
    state = DevelopmentState.load(paths.state_file, recorder.state_persistence())
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    recorder.record_changes(state, changes, invocation_id="ignored", producer="lint")
    state.save()


def lint_status(layout: SimpleNamespace):
    store = GoalStore(layout.control)
    view = build_status(store, store.load(layout.record.id))
    return next(goal for goal in view.goals if goal.key == LINT_KEY)


def test_declared_goal_missing_from_state_is_unmet(goal_mode: SimpleNamespace) -> None:
    path = record_paths(goal_mode.control, goal_mode.record.id).state_file
    raw = json.loads(path.read_text())
    raw["criteria"].pop(LINT_KEY)
    path.write_text(json.dumps(raw))
    goal = lint_status(goal_mode)
    assert (goal.status, goal.evidence_summary) == ("unmet", "no evidence")


def test_corrupt_state_is_reported(goal_mode: SimpleNamespace) -> None:
    record_paths(goal_mode.control, goal_mode.record.id).state_file.write_bytes(b"{bad")
    with pytest.raises(GoalStateError, match="corrupt Goal state"):
        lint_status(goal_mode)


def test_edit_twice_and_rerun(goal_mode: SimpleNamespace) -> None:
    publish(goal_mode)
    assert lint_status(goal_mode).status == "met"
    rtl = goal_mode.worktree / "rtl.v"
    rtl.write_bytes(b"module top; wire a; endmodule\n")
    assert lint_status(goal_mode).status == "stale"
    publish(goal_mode)
    assert lint_status(goal_mode).status == "met"
    rtl.write_bytes(b"module top; wire b; endmodule\n")
    assert lint_status(goal_mode).status == "stale"


def test_parameter_only_core_edit_is_stale(goal_mode: SimpleNamespace) -> None:
    publish(goal_mode)
    core = goal_mode.worktree / "top.core"
    core.write_bytes(core.read_bytes() + b"parameters:\n  width: {datatype: int, default: 2}\n")
    assert lint_status(goal_mode).status == "stale"


@pytest.mark.parametrize("drift", ["checkout", "protected", "spec"])
def test_caller_fences(goal_mode: SimpleNamespace, drift: str) -> None:
    publish(goal_mode)
    if drift == "checkout":
        git(goal_mode.worktree, "checkout", "--detach")
    elif drift == "protected":
        edit_protected(goal_mode)
    else:
        bump_spec(goal_mode, LINT_KEY)
    assert lint_status(goal_mode).status == "stale"


def test_status_inside_outside_and_rendering(goal_mode: SimpleNamespace) -> None:
    store = GoalStore(goal_mode.control)
    inside = status_views(store, goal_mode.worktree)
    assert inside == status_views(store, goal_mode.main)
    assert "lint_clean_top" in render_status(inside)
    assert "Pending proposals: 0" in render_status(inside, short=False)
    assert "lint_clean_top" not in render_status(inside, short=True)
    assert store.load(goal_mode.record.id) == goal_mode.record


def test_revision_without_evidence_cannot_become_met(goal_mode: SimpleNamespace) -> None:
    path = record_paths(goal_mode.control, goal_mode.record.id).state_file
    raw = json.loads(path.read_text())
    raw["criteria"][LINT_KEY]["met"] = True
    path.write_text(json.dumps(raw))
    assert lint_status(goal_mode).status == "stale"


def test_candidate_target_appearing_stays_unmet(layout: SimpleNamespace) -> None:
    from tests.goals.conftest import enter_goals

    layout.record = enter_goals(layout, [{"family": "lint", "target": "future"}])
    store = GoalStore(layout.control)
    assert build_status(store, layout.record).goals[0].status == "unmet"
    (layout.worktree / "future.core").write_bytes(
        b"CAPI=2:\nname: ::future:0\ntargets:\n  future: {toplevel: top}\n"
    )
    assert build_status(store, layout.record).goals[0].status == "unmet"


def test_review_done_with_findings_and_clean_unmet(layout, monkeypatch):
    from booley.criteria.freshness import VerificationFreshness
    from tests.goals.conftest import enter_goals

    layout.record = enter_goals(
        layout,
        [
            {"family": "review", "review": "rtl_bugs", "verdict": "done"},
            {"family": "review", "review": "rtl_code_style", "verdict": "clean"},
        ],
    )
    recorder = GoalEvidenceRecorder(bind(layout))
    state = DevelopmentState.load(
        record_paths(layout.control, layout.record.id).state_file, recorder.state_persistence()
    )
    changes = [
        *state.set_criterion("review_rtl_bugs_done", True, detail={"issues": 3}),
        *state.set_criterion("review_rtl_code_style_clean", False, detail={"issues": 1}),
    ]
    recorder.record_changes(state, changes, invocation_id="ignored", producer="reviewer")
    state.save()
    # Receipt/source checking is independently covered by test_freshness;
    # this presentation boundary verifies the two terminal review meanings.
    monkeypatch.setattr(
        "booley.goals.status.evaluate_goal_freshness",
        lambda *a, **kw: VerificationFreshness(False),
    )
    view = build_status(GoalStore(layout.control), layout.record)
    assert [(goal.status, goal.evidence_summary) for goal in view.goals] == [
        ("met", "reviewed, 3 findings"),
        ("unmet", "1 open"),
    ]


def test_status_outside_git_lists_active_without_hiding_missing_identity(goal_mode, tmp_path):
    from booley.goals.store import REPOSITORY_ID_FILE, WorktreeIdentityError

    outside = tmp_path / "outside"
    outside.mkdir()
    assert status_views(GoalStore(goal_mode.control), outside)[0].record.id == goal_mode.record.id
    (goal_mode.main / ".git" / REPOSITORY_ID_FILE).unlink()
    with pytest.raises(WorktreeIdentityError):
        status_views(GoalStore(goal_mode.control), goal_mode.worktree)


@pytest.mark.parametrize("violation", [None, False, 7, [], {}])
def test_unmet_invalid_contract_reason_is_ignored(goal_mode, violation):
    path = record_paths(goal_mode.control, goal_mode.record.id).state_file
    raw = json.loads(path.read_text())
    raw["criteria"][LINT_KEY]["detail"] = {"warnings": 2, "goal_contract_violation": violation}
    path.write_text(json.dumps(raw))
    goal = lint_status(goal_mode)
    assert (goal.status, goal.evidence_summary, goal.reason) == ("unmet", "2 warnings", "")


def test_projection_conflict_reason_precedes_contract_reason(goal_mode):
    publish(goal_mode)
    path = record_paths(goal_mode.control, goal_mode.record.id).state_file
    raw = json.loads(path.read_text())
    raw["criteria"][LINT_KEY]["met"] = False
    raw["criteria"][LINT_KEY]["detail"]["goal_contract_violation"] = "untrusted explanation"
    path.write_text(json.dumps(raw))
    goal = lint_status(goal_mode)
    assert goal.status == "unmet"
    assert goal.reason == "Goal state differs from its selected immutable producer observation"


@pytest.mark.parametrize("state", ["entering", "active", "finishing"])
def test_scoped_selection_retains_occupying_states(goal_mode, tmp_path, state):
    from booley.goals.status import select_status

    path = record_paths(goal_mode.control, goal_mode.record.id).record_file
    raw = json.loads(path.read_text())
    raw["state"] = state
    path.write_text(json.dumps(raw))
    store = GoalStore(goal_mode.control)
    outside = tmp_path / "outside"
    outside.mkdir()
    occupant = select_status(store, goal_mode.worktree)
    assert occupant.project_wide is False
    assert occupant.views == status_views(store, goal_mode.worktree)
    for caller in (goal_mode.main, outside):
        fallback = select_status(store, caller)
        assert fallback.project_wide is True
        assert fallback.views == occupant.views == status_views(store, caller)


def test_scoped_selection_empty_and_errors(layout, tmp_path):
    from booley.goals.status import select_status
    from booley.goals.store import GoalStoreError
    from tests.goals.conftest import git

    store = GoalStore(layout.control)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git(unrelated, "init", "-q")
    for caller in (layout.worktree, layout.main, unrelated, tmp_path):
        result = select_status(store, caller)
        assert result.project_wide is True
        assert result.views == status_views(store, caller) == ()
    with pytest.raises(GoalStoreError, match="missing or unavailable"):
        select_status(store, tmp_path / "absent")


def test_same_path_with_replacement_identity_uses_project_fallback(goal_mode):
    from booley.goals.status import select_status
    from tests.goals.conftest import git

    old_identity = goal_mode.record.worktree
    worktree = goal_mode.worktree
    git(goal_mode.main, "worktree", "remove", "--force", str(worktree))
    replacement = worktree.parent / "replacement"
    git(goal_mode.main, "worktree", "add", "-q", "-b", "replacement", str(replacement))
    git(goal_mode.main, "worktree", "move", str(replacement), str(worktree))
    store = GoalStore(goal_mode.control)
    assert store.identify_worktree(worktree) != old_identity
    selection = select_status(store, worktree)
    assert selection.project_wide is True
    assert [view.record.id for view in selection.views] == [goal_mode.record.id]
    assert selection.views == status_views(store, worktree)
