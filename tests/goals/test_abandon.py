"""Shared abandonment state matrix, exact operations and interrupted proposal authority."""

from dataclasses import replace
from uuid import uuid4

import pytest

from booley.goals.abandon import abandon_goal
from booley.goals.apply import recover
from booley.goals.changes import read_change_log
from booley.goals.finish import FinishEnvironment, finish_goal
from booley.goals.lifecycle import LifecycleError, LifecycleRequest
from booley.goals.model import GoalState
from booley.goals.paths import record_paths
from booley.goals.proposals import load_proposal
from booley.goals.store import GoalStore
from tests.goals.conftest import git
from tests.goals.test_proposals import interrupted_transaction, proposal, setup_cycle

# These multi-step Git/Reviewer cases retain the established Goal lifecycle budget.
# The measured Windows Reviewer baseline is 27.768s (windows-test-timings.json);
# its 3x/30s-rounded minimum is 90s, above CI's unannotated 60s default.
pytestmark = pytest.mark.timeout(120)


def test_active_abandon_keeps_dirty_wrong_branch_and_end_quote(layout):
    env = setup_cycle(layout)
    (layout.worktree / "rtl.v").write_bytes(b"dirty work\n")
    git(layout.worktree, "checkout", "-qb", "different")
    before = git(layout.worktree, "rev-parse", "HEAD")
    request = LifecycleRequest(
        layout.worktree,
        layout.record.id,
        str(uuid4()),
        abandon=True,
        instruction_quote="Stop this work and retain it",
    )
    result = finish_goal(request, FinishEnvironment(env))
    record = env.store.load(layout.record.id)
    assert record.state is GoalState.ABANDONED and record.ended_at
    assert record.end_instruction_quote == request.instruction_quote
    assert record.publication_floor > layout.record.revision
    assert git(layout.worktree, "rev-parse", "HEAD") == before
    assert (layout.worktree / "rtl.v").read_bytes() == b"dirty work\n"
    assert finish_goal(request, FinishEnvironment(env)) == result
    with pytest.raises(LifecycleError, match="immutable payload"):
        finish_goal(replace(request, instruction_quote="Different"), FinishEnvironment(env))


@pytest.mark.parametrize(
    "boundary, applied",
    [
        (None, False),
        ("approved", False),
        ("intent", True),
        ("effect:record:0", True),
        ("applied", True),
    ],
)
def test_abandon_closes_preintent_without_fake_reject_and_completes_committed_intent(
    layout, boundary, applied
):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    if boundary is not None:
        interrupted_transaction(layout, env, view, boundary=boundary)
    root = record_paths(layout.control, layout.record.id).root
    before = load_proposal(root, view.proposal.id)
    result = abandon_goal(env.store, layout.worktree, FinishEnvironment(env))
    after = load_proposal(root, view.proposal.id)
    assert result["status"] == "abandoned"
    assert after.decision == before.decision
    assert after.transaction_digest == before.transaction_digest
    assert bool(read_change_log(root / "changes.jsonl").applied) is applied
    if applied:
        assert after.state == "applied" and after.closed_by_abandonment is None
    else:
        assert after.state == before.state and after.closed_by_abandonment
        # Ordinary apply recovery cannot resurrect the closed approval.
        recover(env.store, layout.record.id, env)
        assert load_proposal(root, view.proposal.id) == after


def test_rejected_unresolved_effect_recovery_survives_abandonment(layout):
    env = setup_cycle(layout)
    view = proposal(layout, env)
    interrupted_transaction(layout, env, view, answer="reject")
    result = abandon_goal(env.store, layout.worktree, FinishEnvironment(env))
    after = load_proposal(record_paths(layout.control, layout.record.id).root, view.proposal.id)
    assert result["status"] == "abandoned"
    assert after.state == "rejected" and after.resolved_at
    assert after.decision.decision == "reject"
    assert not read_change_log(record_paths(layout.control, layout.record.id).changes_file).applied


def test_quote_is_required_before_abandon_mutation(layout):
    env = setup_cycle(layout)
    with pytest.raises(LifecycleError, match="nonblank"):
        abandon_goal(env.store, layout.worktree, FinishEnvironment(env), instruction_quote=" ")
    assert env.store.load(layout.record.id).state is GoalState.ACTIVE


@pytest.mark.parametrize(
    "boundary",
    ["created", "branch_created", "checked_out", "protected_saved", "pinned", "state_saved"],
)
def test_interrupted_entering_is_classified_and_retained(layout, boundary):
    from booley.goals.apply import ChangeEnvironment
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args
    from tests.goals.test_entry import _Crash

    def stop(name):
        if name == boundary:
            raise _Crash()

    with pytest.raises(_Crash):
        enter_goal_mode(
            EntryRequest(
                layout.worktree, "enter", parse_goal_args([{"family": "lint", "target": "top"}])
            ),
            EntryEnvironment(layout.control, on_boundary=stop),
        )
    store = GoalStore(layout.control)
    record = store.active_for_worktree(layout.worktree)
    branches = git(layout.worktree, "branch", "--format=%(refname)")
    result = abandon_goal(
        store,
        layout.worktree,
        FinishEnvironment(ChangeEnvironment(EntryEnvironment(layout.control))),
    )
    assert result["status"] == "abandoned" and result["notes"]
    assert git(layout.worktree, "branch", "--format=%(refname)") == branches
    assert store.load(record.id).state is GoalState.ABANDONED


@pytest.mark.parametrize(
    "boundary, outcome",
    [
        ("finishing", "finished"),
        ("ref-published", "finished"),
        ("index-synchronized", "finished"),
        ("finished", "finished"),
    ],
)
def test_abandon_recovers_finishing_through_original_exact_publication(layout, boundary, outcome):
    from tests.goals.test_finish import Crash, environment, publication_layout, request

    complete = publication_layout(layout)
    call = request(complete)

    def stop(name):
        if name == boundary:
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    abandoned = LifecycleRequest(
        complete.worktree,
        complete.record.id,
        str(uuid4()),
        abandon=True,
        instruction_quote="Stop unless finish already completed",
    )
    if boundary == "finished":
        # The record is terminal; only the original finish retry can recover a lost response.
        with pytest.raises(LifecycleError, match="occupant"):
            finish_goal(abandoned, environment(complete))
        assert finish_goal(call, environment(complete))["status"] == outcome
    else:
        assert finish_goal(abandoned, environment(complete))["status"] == outcome
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.FINISHED


def test_abandon_finishing_after_participant_move_and_external_conflict(layout):
    from tests.goals.test_finish import Crash, environment, publication_layout, request

    complete = publication_layout(layout)
    call = request(complete)

    def stop(name):
        if name == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    destination = complete.worktree / f".booley_project/goals/history/{complete.record.id}.md"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"other writer")
    abandoned = LifecycleRequest(
        complete.worktree,
        complete.record.id,
        str(uuid4()),
        abandon=True,
        instruction_quote="Stop this attempt",
    )
    with pytest.raises(LifecycleError, match="conflicts"):
        finish_goal(abandoned, environment(complete))
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.FINISHING
    destination.unlink()
    (complete.worktree / "ordinary.txt").write_bytes(b"ordinary change")
    git(complete.worktree, "add", "ordinary.txt")
    git(complete.worktree, "commit", "-qm", "participant moved")
    assert finish_goal(abandoned, environment(complete))["status"] == "abandoned"
    assert finish_goal(call, environment(complete))["status"] == "revalidation_required"


def test_abandon_lost_terminal_response_retains_entering_classification(layout):
    from booley.goals.apply import ChangeEnvironment
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args
    from tests.goals.test_finish import Crash

    def entering(name):
        if name == "branch_created":
            raise Crash()

    with pytest.raises(Crash):
        enter_goal_mode(
            EntryRequest(
                layout.worktree,
                "interrupted",
                parse_goal_args([{"family": "lint", "target": "top"}]),
            ),
            EntryEnvironment(layout.control, on_boundary=entering),
        )
    store = GoalStore(layout.control)
    record = store.active_for_worktree(layout.worktree)
    call = LifecycleRequest(
        layout.worktree,
        record.id,
        str(uuid4()),
        abandon=True,
        instruction_quote="Stop interrupted entry",
    )

    def stop(name):
        if name == "abandoned":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(
            call,
            FinishEnvironment(
                ChangeEnvironment(EntryEnvironment(layout.control)), on_boundary=stop
            ),
        )
    result = finish_goal(
        call, FinishEnvironment(ChangeEnvironment(EntryEnvironment(layout.control)))
    )
    assert result["notes"] and "retained" in result["notes"][0]
    assert (
        finish_goal(call, FinishEnvironment(ChangeEnvironment(EntryEnvironment(layout.control))))
        == result
    )
