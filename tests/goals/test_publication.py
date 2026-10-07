"""The publication gate: what a Goal run may still publish when it completes (B1, D7, D15)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.criteria.state import DevelopmentState
from booley.goals.binding import GoalRunBinding
from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
from booley.goals.model import GoalState, parse_goal_args
from booley.goals.paths import record_paths
from booley.goals.publication import EvidenceDiscarded, PublicationGate
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.state_store import GoalStateError
from tests.goals.conftest import (
    LINT_KEY,
    SIM_KEY,
    bind,
    bump_spec,
    campaign_facts,
    edit_protected,
    git,
    tree_digest,
    update_record,
)


def publish(binding: GoalRunBinding, affected: set[str]) -> None:
    with PublicationGate(binding).publishing(affected):
        pass


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_gate_passes_and_yields_the_current_record(goal_mode: SimpleNamespace) -> None:
    binding = bind(goal_mode)

    with PublicationGate(binding).publishing({LINT_KEY}) as record:
        assert record == goal_mode.record


def test_unrelated_record_revision_change_is_accepted(goal_mode: SimpleNamespace) -> None:
    binding = bind(goal_mode)
    update_record(goal_mode, session_key="work_dir:/elsewhere")

    publish(binding, {LINT_KEY, SIM_KEY})


def test_record_revision_change_of_the_protected_baseline_is_discarded(
    goal_mode: SimpleNamespace,
) -> None:
    binding = bind(goal_mode)
    update_record(goal_mode, protected_digest="sha256:" + "0" * 64)

    with pytest.raises(EvidenceDiscarded, match="protected-input baseline changed"):
        publish(binding, set())


def test_affected_goal_spec_change_is_discarded(goal_mode: SimpleNamespace) -> None:
    binding = bind(goal_mode)
    bump_spec(goal_mode, LINT_KEY)

    with pytest.raises(EvidenceDiscarded, match=f"Goal {LINT_KEY} changed during the run"):
        publish(binding, {LINT_KEY})


def test_unaffected_goal_spec_change_is_accepted(goal_mode: SimpleNamespace) -> None:
    binding = bind(goal_mode)
    bump_spec(goal_mode, SIM_KEY)

    publish(binding, {LINT_KEY})


def test_removed_goal_and_unknown_key_are_discarded(goal_mode: SimpleNamespace) -> None:
    binding = bind(goal_mode)
    record = goal_mode.record
    update_record(goal_mode, goals=tuple(g for g in record.goals if g.spec.key != SIM_KEY))

    with pytest.raises(EvidenceDiscarded, match="removed during the run"):
        publish(binding, {SIM_KEY})
    with pytest.raises(EvidenceDiscarded, match="was not a Goal"):
        publish(binding, {"lint_clean_other"})


@pytest.mark.parametrize("state", [GoalState.FINISHING, GoalState.ABANDONED])
def test_record_no_longer_active_is_discarded(
    goal_mode: SimpleNamespace, state: GoalState
) -> None:
    binding = bind(goal_mode)
    update_record(goal_mode, state=state)

    with pytest.raises(EvidenceDiscarded, match=f"Goal Mode {state.value}"):
        publish(binding, set())


def test_protected_input_changed_before_the_run_is_discarded(goal_mode: SimpleNamespace) -> None:
    edit_protected(goal_mode)
    binding = bind(goal_mode)

    with pytest.raises(EvidenceDiscarded, match="changed before the run"):
        publish(binding, set())


def test_protected_input_changed_at_the_end_is_discarded(goal_mode: SimpleNamespace) -> None:
    binding = bind(goal_mode)
    edit_protected(goal_mode)

    with pytest.raises(EvidenceDiscarded, match="changed during the run"):
        publish(binding, set())


def test_protected_input_changed_before_and_restored_before_completion_is_discarded(
    goal_mode: SimpleNamespace,
) -> None:
    before = edit_protected(goal_mode)
    binding = bind(goal_mode)
    edit_protected(goal_mode, before)

    with pytest.raises(EvidenceDiscarded, match="changed before the run"):
        publish(binding, set())


def test_in_flight_change_and_revert_is_not_caught(goal_mode: SimpleNamespace) -> None:
    """D7's documented gap: both samples match, so the evidence publishes."""
    binding = bind(goal_mode)
    before = edit_protected(goal_mode)
    edit_protected(goal_mode, before)

    publish(binding, {LINT_KEY})


# ---------------------------------------------------------------------------
# Discarded evidence writes nothing
# ---------------------------------------------------------------------------


def _passing_lint(recorder: GoalEvidenceRecorder, state_file: Path) -> DevelopmentState:
    state = DevelopmentState.load(state_file, recorder.state_persistence())
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    recorder.record_changes(state, changes, invocation_id="lint-1", producer="lint")
    return state


def test_discarded_evidence_leaves_state_and_ledger_byte_identical(
    goal_mode: SimpleNamespace,
) -> None:
    paths = record_paths(goal_mode.control, goal_mode.record.id)
    first = GoalEvidenceRecorder(bind(goal_mode, "run-1"))
    _passing_lint(first, paths.state_file).save()
    stale = GoalEvidenceRecorder(bind(goal_mode, "run-2"))
    state = DevelopmentState.load(paths.state_file, stale.state_persistence())
    bump_spec(goal_mode, LINT_KEY)
    record_file = paths.record_file.read_bytes()
    before = tree_digest(paths.root)

    changes = state.set_criterion(LINT_KEY, False, detail={"warnings": 3})
    with pytest.raises(EvidenceDiscarded, match="changed during the run"):
        stale.record_changes(state, changes, invocation_id="lint-2", producer="lint")
    state.record_mcp_tool_run("lint", 1)
    with pytest.raises(EvidenceDiscarded, match="changed during the run"):
        state.save()
    with pytest.raises(EvidenceDiscarded, match="changed during the run"):
        stale.record_or_verify_transaction(
            state, changes, acceptance_facts=campaign_facts(), ticket_identity={}
        )

    assert tree_digest(paths.root) == before
    assert paths.record_file.read_bytes() == record_file


def test_ineligible_run_discards_even_a_timeline_only_save(goal_mode: SimpleNamespace) -> None:
    paths = record_paths(goal_mode.control, goal_mode.record.id)
    before_edit = edit_protected(goal_mode)
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    edit_protected(goal_mode, before_edit)
    state = DevelopmentState.load(paths.state_file, recorder.state_persistence())
    before = tree_digest(paths.root)

    state.record_mcp_tool_run("lint", 0)
    with pytest.raises(EvidenceDiscarded, match="changed before the run"):
        state.save()

    assert tree_digest(paths.root) == before


# ---------------------------------------------------------------------------
# Bindings that outlive their record (B3)
# ---------------------------------------------------------------------------


def test_record_moved_after_binding_is_discarded(goal_mode: SimpleNamespace) -> None:
    binding = bind(goal_mode)
    root = record_paths(goal_mode.control, goal_mode.record.id).root
    root.rename(goal_mode.control / "moved-record")

    with pytest.raises(EvidenceDiscarded, match="no longer exists"):
        publish(binding, {LINT_KEY})


def test_state_file_of_another_record_is_refused(goal_mode: SimpleNamespace) -> None:
    """Loading is the only way a state gets its file and strategy, so it refuses here."""
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    own = record_paths(goal_mode.control, goal_mode.record.id).state_file
    foreign = record_paths(goal_mode.control, "other-20261006T130000Z").state_file
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(own.read_bytes())

    with pytest.raises(GoalStateError, match="does not belong to Goal Mode"):
        DevelopmentState.load(foreign, recorder.state_persistence())


def test_job_completing_after_abandon_and_reentry_is_discarded(
    goal_mode: SimpleNamespace,
) -> None:
    old = GoalEvidenceRecorder(bind(goal_mode, "queued-run"))
    old_state = DevelopmentState.load(
        record_paths(goal_mode.control, goal_mode.record.id).state_file, old.state_persistence()
    )
    update_record(goal_mode, state=GoalState.ABANDONED)
    request = EntryRequest(
        work_dir=goal_mode.worktree,
        slug="again",
        goals=parse_goal_args([{"family": "lint", "target": "top"}]),
    )
    new = enter_goal_mode(request, EntryEnvironment(project_dir=goal_mode.control)).record
    new_paths = record_paths(goal_mode.control, new.id)
    before = tree_digest(new_paths.root)

    changes = old_state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    with pytest.raises(EvidenceDiscarded, match="Goal Mode abandoned"):
        old.record_changes(old_state, changes, invocation_id="queued-run", producer="lint")
    with pytest.raises(EvidenceDiscarded, match="Goal Mode abandoned"):
        old_state.save()

    assert tree_digest(new_paths.root) == before
    assert not new_paths.logs_dir.exists() or not any(new_paths.logs_dir.iterdir())


def test_checkout_switched_after_admission_is_discarded(goal_mode: SimpleNamespace) -> None:
    binding = bind(goal_mode)
    git(goal_mode.worktree, "checkout", "-q", "-b", "elsewhere")

    with pytest.raises(
        EvidenceDiscarded, match=r"checkout changed during the run.*not Goal Branch"
    ):
        publish(binding, {LINT_KEY})
