"""Approved changes of Goals that name Test Runs (ADR 0072)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.test_runs import build_test_run
from booley.goals.apply import ChangeEnvironment
from booley.goals.change_policy import translate_after, validate_kind
from booley.goals.change_service import create_proposal
from booley.goals.changes import ChangeKind
from booley.goals.model import GoalRecord
from booley.goals.paths import record_paths
from booley.goals.proposals import ProposalError
from booley.goals.status import build_status
from tests.goals.conftest import (
    SIM_KEY,
    enter_goals,
    entry_environment,
    run_row,
    write_tests_toml,
)
from tests.goals.test_proposals import approve
from tests.goals.test_test_run_evidence import _contributors, _outcomes, _record

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)

SEEDS_KEY = "sim_pass_top_run_736d6f6b65_x"
TARGET = {"family": "SIM", "target": "top"}


def _goal(test: str | None = None) -> dict[str, Any]:
    return TARGET if test is None else {**TARGET, "test": test}


def _mode(layout: SimpleNamespace, *goals: dict[str, Any], **kwargs: Any) -> ChangeEnvironment:
    layout.record = enter_goals(layout, list(goals), **kwargs)
    return ChangeEnvironment(entry_environment(layout))


def _change(
    layout: SimpleNamespace, env: ChangeEnvironment, kind: str, key: str | None, after: dict
) -> GoalRecord:
    arguments = {"kind": kind, "after": after, "rationale": "the human asked for this change"}
    if key is not None:
        arguments["goal_key"] = key
    view = create_proposal(env, layout.record.id, arguments, session_key="test-session")
    assert approve(layout, env, view.proposal.id).state == "applied"
    return env.store.load(layout.record.id)


def _seeds(record: GoalRecord, key: str) -> list[int | None]:
    resolution = record.test_run_resolution(key)
    assert resolution is not None
    return [run.seed for run in resolution.test_runs]


# --- The policy ------------------------------------------------------------------


def _spec(test: str | None = None) -> Any:
    return translate_after(_goal(test))


@pytest.mark.parametrize(
    ("kind", "before", "after", "refusal"),
    [
        (ChangeKind.RELAX, "a@x5", "a@x3", None),  # fewer seeds
        (ChangeKind.RELAX, "a@x3", "a@x5", "only weaken"),
        (ChangeKind.RELAX, "a@x3", "b@x2", "must preserve"),
        (ChangeKind.RETARGET, "a@x3", "a@x5", None),  # more seeds: stricter
        (ChangeKind.RETARGET, "a@x5", "a@x3", "is a relaxation"),
        (ChangeKind.RETARGET, "a", "a@7", None),  # pinning a seed
        (ChangeKind.RETARGET, "a", "a+soak", None),  # another Test Variant
        (ChangeKind.RETARGET, "a@x3", "a@x3", None),  # freeze the same Goal again
        (ChangeKind.RETARGET, None, None, None),  # freeze the Target Goal again
        (ChangeKind.RELAX, "a@x3", "a@x3", "does not change the Goal"),
        (ChangeKind.RELAX, "a", "a", "only weaken"),
    ],
)
def test_change_kinds_of_test_run_goals(
    kind: ChangeKind, before: str | None, after: str | None, refusal: str | None
) -> None:
    if refusal is None:
        validate_kind(kind, _spec(before), _spec(after))
        return
    with pytest.raises(ProposalError, match=refusal):
        validate_kind(kind, _spec(before), _spec(after))


def test_a_goal_that_names_no_test_runs_still_cannot_be_retargeted_to_itself() -> None:
    lint = translate_after({"family": "LINT", "target": "top"})
    with pytest.raises(ProposalError, match="does not change the Goal"):
        validate_kind(ChangeKind.RETARGET, lint, lint)


# --- Applied changes -------------------------------------------------------------


def test_lowering_n_keeps_the_first_seeds_and_raising_n_draws_the_difference(
    layout: SimpleNamespace,
) -> None:
    env = _mode(layout, _goal("smoke@x3"))
    frozen = _seeds(layout.record, SEEDS_KEY)
    assert frozen == [101, 102, 103]  # the test seed source: nothing here is random

    lowered = _change(layout, env, "relax", SEEDS_KEY, _goal("smoke@x2"))
    assert _seeds(lowered, SEEDS_KEY) == frozen[:2]
    goal = next(goal for goal in lowered.goals if goal.spec.key == SEEDS_KEY)
    assert goal.spec.params == {"test": "smoke@x2"} and goal.spec_revision > 1

    raised = _change(layout, env, "retarget", SEEDS_KEY, _goal("smoke@x4"))
    seeds = _seeds(raised, SEEDS_KEY)
    assert seeds == [101, 102, 103, 104]  # 101 and 102 are taken: only the difference is drawn
    resolution = raised.test_run_resolution(SEEDS_KEY)
    # The kept runs stay as they were frozen; only the difference is new.
    assert [item.resolved_by for item in resolution.runs] == ["entry", "entry", "change", "change"]


def test_an_approved_n_change_restarts_the_outcomes(layout: SimpleNamespace) -> None:
    env = _mode(layout, _goal("smoke@x2"))
    first, second = _seeds(layout.record, SEEDS_KEY)
    rows = [run_row(build_test_run("smoke", seed=seed)) for seed in (first, second)]
    _record(layout, "run-1", [{**rows[0], "grade": "fail"}, rows[1]], key=SEEDS_KEY, met=False)

    _change(layout, env, "retarget", SEEDS_KEY, _goal("smoke@x3"))
    layout.record = env.store.load(layout.record.id)
    _record(layout, "run-2", rows, key=SEEDS_KEY)
    # The failure was recorded under the previous revision: it is not folded.
    outcomes = list(_outcomes(layout, SEEDS_KEY).values())
    assert outcomes == ["passed", "passed", "missing"]
    assert _contributors(layout, SEEDS_KEY) == []


def test_adding_a_test_goal_and_pinning_a_seed_freeze_the_new_goal(
    layout: SimpleNamespace,
) -> None:
    env = _mode(layout, TARGET)
    added = _change(layout, env, "add", None, _goal("smoke"))
    key = "sim_pass_top_run_736d6f6b65_d"
    assert [run.name for run in added.test_run_resolution(key).test_runs] == ["smoke@1"]

    pinned = _change(layout, env, "retarget", key, _goal("smoke@7"))
    pinned_key = "sim_pass_top_run_736d6f6b65_s7"
    assert key not in pinned.test_run_resolutions
    assert [run.name for run in pinned.test_run_resolution(pinned_key).test_runs] == ["smoke@7"]
    # The untouched Target Goal keeps its frozen runs and its revision.
    assert pinned.test_run_resolutions[SIM_KEY] == layout.record.test_run_resolutions[SIM_KEY]


def test_a_changed_frozen_run_needs_an_approved_retarget(layout: SimpleNamespace) -> None:
    variants = b'[top]\ntests = ["smoke"]\n[top.variants.smoke]\ndefault = "v"\nv = ["+n=1"]\n'
    env = _mode(layout, TARGET, tests_toml=variants)
    write_tests_toml(layout, variants.replace(b"+n=1", b"+n=2"))

    def reason() -> str:
        view = build_status(env.store, env.store.load(layout.record.id), work_dir=layout.worktree)
        return view.goals[0].reason

    assert reason().startswith("goal-test-run-changed: ")
    # Rerunning cannot cure it, and neither can deferred resolution.
    from booley.goals.test_run_freezing import ensure_test_run_resolutions

    ensure_test_run_resolutions(env.store, layout.record.id, layout.worktree)
    assert reason().startswith("goal-test-run-changed: ")

    refrozen = _change(layout, env, "retarget", SIM_KEY, TARGET)
    (run,) = refrozen.test_run_resolution(SIM_KEY).test_runs
    assert run.args == ("+n=2",)
    assert not reason().startswith("goal-test-run-changed")


def test_a_seeded_goal_cannot_be_added_to_an_unseeded_target(
    layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows import target_test_plan_loading as loading

    env = _mode(layout, TARGET)
    monkeypatch.setattr(
        loading,
        "load_checkout_target_test_plan",
        lambda work_dir, target: loading.load_target_test_plan(
            work_dir, target, identity=target, adapter="icarus"
        ),
    )
    view = create_proposal(
        env,
        layout.record.id,
        {"kind": "add", "after": _goal("smoke@x2"), "rationale": "more seeds"},
        session_key="test-session",
    )
    with pytest.raises(ProposalError, match="seed"):
        approve(layout, env, view.proposal.id)


def test_nothing_is_frozen_until_the_human_approves_and_an_approved_add_draws_its_seeds(
    layout: SimpleNamespace,
) -> None:
    env = _mode(layout, TARGET)
    record_file = record_paths(layout.control, layout.record.id).record_file
    before = record_file.read_bytes()
    arguments = {"kind": "add", "after": _goal("smoke@x2"), "rationale": "two more seeds"}
    view = create_proposal(env, layout.record.id, arguments, session_key="test-session")
    # A proposal changes nothing: no Goal, no frozen run, no drawn seed.
    assert record_file.read_bytes() == before
    assert SEEDS_KEY not in env.store.load(layout.record.id).test_run_resolutions

    assert approve(layout, env, view.proposal.id).state == "applied"
    added = env.store.load(layout.record.id)
    assert _seeds(added, SEEDS_KEY) == [101, 102]
    assert {item.resolved_by for item in added.test_run_resolution(SEEDS_KEY).runs} == {"change"}
    assert added.test_run_resolutions[SIM_KEY] == layout.record.test_run_resolutions[SIM_KEY]


def test_raising_n_through_approval_is_refused_when_the_frozen_variant_changed(
    layout: SimpleNamespace,
) -> None:
    variants = b'[top]\ntests = ["smoke"]\n[top.variants.smoke]\ndefault = "v"\nv = ["+n=1"]\n'
    env = _mode(layout, _goal("smoke@x2"), tests_toml=variants)
    frozen = env.store.load(layout.record.id).test_run_resolution(SEEDS_KEY)
    write_tests_toml(layout, variants.replace(b"+n=1", b"+n=2"))

    arguments = {"kind": "retarget", "goal_key": SEEDS_KEY, "after": _goal("smoke@x3")}
    view = create_proposal(
        env, layout.record.id, {**arguments, "rationale": "more"}, session_key="test-session"
    )
    with pytest.raises(ProposalError, match="declared arguments or environment"):
        approve(layout, env, view.proposal.id)
    assert env.store.load(layout.record.id).test_run_resolution(SEEDS_KEY) == frozen


def test_an_approved_change_cannot_touch_another_goals_frozen_runs(
    layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record effect is not saved through the store: its frozen-run rule applies anyway."""
    from booley.goals import apply
    from booley.goals.test_run_resolution import TestRunResolution

    env = _mode(layout, TARGET, _goal("smoke@7"))
    real = apply.refreeze_changed_goal

    def losing_a_sibling(goal, before, plans, request, **kwargs):
        # A defect in freezing that drops another Goal's frozen runs.
        other, held = kwargs["siblings"][SIM_KEY]
        kwargs["siblings"][SIM_KEY] = (other, TestRunResolution(held.spec_revision, (), ()))
        return real(goal, before, plans, request, **kwargs)

    monkeypatch.setattr(apply, "refreeze_changed_goal", losing_a_sibling)
    arguments = {"kind": "add", "after": _goal("smoke@8"), "rationale": "one more"}
    view = create_proposal(env, layout.record.id, arguments, session_key="test-session")
    refusal = r"frozen Test Runs of Goal SIM:top \(all selected tests pass\) cannot change"
    with pytest.raises(ProposalError, match=refusal) as refused:
        approve(layout, env, view.proposal.id)
    assert SIM_KEY not in str(refused.value)  # the public Goal name, not its key


def test_a_transaction_prepared_for_a_decision_that_was_never_saved_is_collected(
    layout: SimpleNamespace,
) -> None:
    class _StopError(Exception):
        pass

    def stop(boundary: str) -> None:
        if boundary == "prepared":
            raise _StopError(boundary)

    env = _mode(layout, TARGET)
    applies = record_paths(layout.control, layout.record.id).root / "applies"
    arguments = {"kind": "add", "after": _goal("smoke@x2"), "rationale": "two more seeds"}
    view = create_proposal(env, layout.record.id, arguments, session_key="test-session")
    with pytest.raises(_StopError):
        approve(layout, replace(env, on_boundary=stop), view.proposal.id)
    (orphan,) = applies.glob("*.json")

    assert approve(layout, env, view.proposal.id).state == "applied"
    (kept,) = applies.glob("*.json")
    assert kept != orphan  # the decision that was made owns the only transaction left


def test_approved_change_omits_old_ledger_and_preserves_sibling_history(layout):
    from booley.criteria.state import DevelopmentState
    from booley.goals.recorder import GoalEvidenceRecorder
    from tests.goals.conftest import bind
    from tests.goals.test_proposals import CYCLE_ARG, CYCLE_KEY, proposal
    from tests.goals.test_test_run_evidence import A, B, _evidence_file, _status

    env = _mode(layout, CYCLE_ARG, TARGET, tests_toml=b'[top]\ntests = ["a", "b", "smoke"]\n')
    _record(layout, "first", [run_row(A)])
    recorder = GoalEvidenceRecorder(bind(layout, "group"))
    state = DevelopmentState.load(
        record_paths(layout.control, layout.record.id).state_file, recorder.state_persistence()
    )
    changes = state.set_criterion(
        CYCLE_KEY,
        False,
        detail={
            "cycles": 100,
            "test": "smoke",
            "cycle_observation": "observed",
            "test_runs": [run_row()],
        },
    )
    changes += state.set_criterion(SIM_KEY, True, detail={"test_runs": [run_row(B), run_row()]})
    recorder.record_changes(state, changes, invocation_id="group", producer="sim")
    state.save()
    import json

    from tests.goals.test_proposals import observations

    for row in observations(layout):
        row["detail"]["test_run_ledger"] = "obsolete"
        _evidence_file(layout, row["sequence"]).write_text(json.dumps(row), encoding="utf-8")
    approved = proposal(layout, env)
    approve(layout, env, approved.proposal.id)
    rows = observations(layout)
    assert all(
        "test_run_ledger" not in row["detail"]
        for row in rows
        if "goal_derivation" in row["detail"]
    )
    assert _status(layout).status == "met"
    assert 1 in _contributors(layout)
    approve(layout, env, proposal(layout, env, maximum=300).proposal.id)
    assert 1 in _contributors(layout)
    assert _status(layout).status == "met"


def test_relaxation_does_not_carry_aggregate_across_revisions(layout):
    from tests.goals.test_test_run_evidence import _status

    env = _mode(layout, _goal("smoke@x3"))
    first, second, third = _seeds(layout.record, SEEDS_KEY)
    _record(
        layout,
        "first",
        [run_row(build_test_run("smoke", seed=seed)) for seed in (first, second)],
        key=SEEDS_KEY,
    )
    _record(layout, "last", [run_row(build_test_run("smoke", seed=third))], key=SEEDS_KEY)
    assert _status(layout, SEEDS_KEY).status == "met"
    _change(layout, env, "relax", SEEDS_KEY, _goal("smoke@x2"))
    status = _status(layout, SEEDS_KEY)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-incomplete:")
