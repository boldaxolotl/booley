"""Binding a run to the worktree's active Goal Mode (ADR 0067 Phase 3, B3)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.binding import GoalBindingError, GoalRunBinding, bind_run
from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
from booley.goals.model import GoalState, parse_goal_args
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from tests.goals.conftest import LINT_KEY, SIM_KEY, git


class _Crash(BaseException):
    """Stops entry at a boundary the way a killed process would (no rollback)."""


def _store(layout: SimpleNamespace) -> GoalStore:
    return GoalStore(layout.control)


def _set_state(layout: SimpleNamespace, state: GoalState) -> None:
    store = _store(layout)
    record = store.active_for_worktree(layout.worktree)
    assert record is not None
    with store.record_lock(record.id) as lock:
        store.save(lock, replace(record, state=state))


def test_binds_an_active_goal_mode(goal_mode: SimpleNamespace) -> None:
    record = goal_mode.record

    binding = bind_run(_store(goal_mode), goal_mode.worktree / "docs", "run-1")

    assert binding.record_id == record.id
    assert binding.record_revision == record.revision
    assert binding.worktree == record.worktree
    assert binding.worktree_root == goal_mode.worktree.resolve()
    assert binding.goal_branch == record.branch
    assert binding.invocation_id == "run-1"
    assert binding.spec_revisions == ((LINT_KEY, 1), (SIM_KEY, 1))
    assert binding.spec_revision(LINT_KEY) == 1
    assert binding.spec_revision("lint_clean_other") is None
    assert binding.protected_paths == record.protected_paths
    assert binding.start_digest == record.protected_digest
    assert binding.start_head_digest == record.protected_head_digest
    assert binding.eligible is True
    assert binding.ineligible_reason == ""


def test_binding_round_trips_through_json(goal_mode: SimpleNamespace) -> None:
    binding = bind_run(_store(goal_mode), goal_mode.worktree, "run-1")

    assert GoalRunBinding.from_json(binding.to_json()) == binding


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "booley.goal-run-binding/v0"),
        ("record_revision", 0),
        ("record_id", "not a goal id"),
        ("spec_revisions", {"lint_clean_top": True}),
        ("eligible", "yes"),
        ("unknown", 1),
    ],
)
def test_binding_json_is_parsed_strictly(
    goal_mode: SimpleNamespace, field: str, value: object
) -> None:
    raw = bind_run(_store(goal_mode), goal_mode.worktree, "run-1").to_json()
    raw[field] = value

    with pytest.raises(GoalBindingError, match="invalid Goal run binding"):
        GoalRunBinding.from_json(raw)


def test_protected_input_changed_before_the_run_binds_ineligible(
    goal_mode: SimpleNamespace,
) -> None:
    (goal_mode.control / "booley.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

    binding = bind_run(_store(goal_mode), goal_mode.worktree, "run-1")

    assert binding.eligible is False
    assert "differs from its state at entry" in binding.ineligible_reason


def test_wrong_branch_is_refused(goal_mode: SimpleNamespace) -> None:
    git(goal_mode.worktree, "checkout", "-q", "-b", "elsewhere")

    with pytest.raises(GoalBindingError, match="not Goal Branch"):
        bind_run(_store(goal_mode), goal_mode.worktree, "run-1")


def test_recreated_worktree_inherits_the_record_by_name_but_is_refused(
    goal_mode: SimpleNamespace,
) -> None:
    git(goal_mode.main, "worktree", "remove", "--force", str(goal_mode.worktree))
    git(goal_mode.main, "worktree", "add", "-q", "-b", "fresh", str(goal_mode.worktree))

    store = _store(goal_mode)
    assert store.active_for_worktree(goal_mode.worktree) == goal_mode.record
    with pytest.raises(GoalBindingError, match="not Goal Branch"):
        bind_run(store, goal_mode.worktree, "run-1")


def test_worktree_without_goal_mode_is_refused(layout: SimpleNamespace, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(layout.main, "worktree", "add", "-q", "-b", "other", str(other))

    with pytest.raises(GoalBindingError, match="hosts no Goal Mode"):
        bind_run(_store(layout), other, "run-1")


@pytest.mark.parametrize("state", [GoalState.FINISHING, GoalState.ABANDONED])
def test_non_active_record_is_refused(goal_mode: SimpleNamespace, state: GoalState) -> None:
    _set_state(goal_mode, state)

    message = "is finishing" if state is GoalState.FINISHING else "hosts no Goal Mode"
    with pytest.raises(GoalBindingError, match=message):
        bind_run(_store(goal_mode), goal_mode.worktree, "run-1")


def test_entering_record_is_refused(layout: SimpleNamespace) -> None:
    def crash(boundary: str) -> None:
        if boundary == "protected_saved":
            raise _Crash(boundary)

    request = EntryRequest(
        work_dir=layout.worktree,
        slug="evidence",
        goals=parse_goal_args([{"family": "lint", "target": "top"}]),
    )
    with pytest.raises(_Crash):
        enter_goal_mode(request, EntryEnvironment(project_dir=layout.control, on_boundary=crash))

    with pytest.raises(GoalBindingError, match="is entering"):
        bind_run(_store(layout), layout.worktree, "run-1")


def test_corrupt_record_is_refused(goal_mode: SimpleNamespace) -> None:
    paths = record_paths(goal_mode.control, goal_mode.record.id)
    paths.record_file.write_text("{not json", encoding="utf-8")

    with pytest.raises(GoalBindingError, match="corrupt Goal Record"):
        bind_run(_store(goal_mode), goal_mode.worktree, "run-1")


def test_missing_invocation_id_is_refused(goal_mode: SimpleNamespace) -> None:
    with pytest.raises(GoalBindingError, match="invocation id"):
        bind_run(_store(goal_mode), goal_mode.worktree, "")


@pytest.mark.parametrize("captured", [None, "digest"])
def test_unresolvable_publication_surface_is_never_unchanged(goal_mode, captured):
    from booley.criteria.state import DevelopmentState
    from booley.flows.execution_persistence import EvidenceDiscarded
    from booley.goals.freshness import GoalFreshnessResolvers
    from booley.goals.recorder import GoalEvidenceRecorder
    from tests.goals.conftest import bind

    binding = replace(bind(goal_mode), start_surfaces=(("top", captured),))
    recorder = GoalEvidenceRecorder(
        binding,
        resolvers=GoalFreshnessResolvers(
            target_surface=lambda root, target: {"error": "unresolvable"}
        ),
    )
    state = DevelopmentState.load(
        record_paths(goal_mode.control, goal_mode.record.id).state_file,
        recorder.state_persistence(),
    )
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    with pytest.raises(EvidenceDiscarded, match="changed during the run"):
        recorder.record_changes(state, changes, invocation_id="test", producer="lint")
