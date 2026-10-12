"""Approved changes of Goals that name Test Runs (ADR 0072)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.test_runs import build_test_run
from booley.goals.apply import ChangeEnvironment
from booley.goals.change_policy import translate_after, validate_kind
from booley.goals.change_service import create_proposal
from booley.goals.changes import ChangeKind
from booley.goals.entry import EntryEnvironment
from booley.goals.model import GoalRecord
from booley.goals.proposals import ProposalError
from booley.goals.status import build_status
from booley.goals.test_run_evidence import LEDGER_DETAIL_KEY
from tests.goals.conftest import SIM_KEY, enter_goals, run_row, write_tests_toml
from tests.goals.test_proposals import approve
from tests.goals.test_test_run_evidence import _record

SEEDS_KEY = "sim_pass_top_run_736d6f6b65_x"
TARGET = {"family": "sim", "target": "top"}


def _goal(test: str | None = None) -> dict[str, Any]:
    return TARGET if test is None else {**TARGET, "test": test}


def _mode(layout: SimpleNamespace, *goals: dict[str, Any], **kwargs: Any) -> ChangeEnvironment:
    layout.record = enter_goals(layout, list(goals), **kwargs)
    return ChangeEnvironment(EntryEnvironment(layout.control))


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
    lint = translate_after({"family": "lint", "target": "top"})
    with pytest.raises(ProposalError, match="does not change the Goal"):
        validate_kind(ChangeKind.RETARGET, lint, lint)


# --- Applied changes -------------------------------------------------------------


def test_lowering_n_keeps_the_first_seeds_and_raising_n_draws_the_difference(
    layout: SimpleNamespace,
) -> None:
    env = _mode(layout, _goal("smoke@x3"))
    frozen = _seeds(layout.record, SEEDS_KEY)
    assert len(set(frozen)) == 3

    lowered = _change(layout, env, "relax", SEEDS_KEY, _goal("smoke@x2"))
    assert _seeds(lowered, SEEDS_KEY) == frozen[:2]
    goal = next(goal for goal in lowered.goals if goal.spec.key == SEEDS_KEY)
    assert goal.spec.params == {"test": "smoke@x2"} and goal.spec_revision > 1

    raised = _change(layout, env, "retarget", SEEDS_KEY, _goal("smoke@x4"))
    seeds = _seeds(raised, SEEDS_KEY)
    assert seeds[:2] == frozen[:2] and len(set(seeds)) == 4
    resolution = raised.test_run_resolution(SEEDS_KEY)
    assert {item.resolved_by for item in resolution.runs} == {"change"}


def test_an_approved_n_change_restarts_the_outcomes(layout: SimpleNamespace) -> None:
    env = _mode(layout, _goal("smoke@x2"))
    first, second = _seeds(layout.record, SEEDS_KEY)
    rows = [run_row(build_test_run("smoke", seed=seed)) for seed in (first, second)]
    _record(layout, "run-1", [{**rows[0], "grade": "fail"}, rows[1]], key=SEEDS_KEY, met=False)

    _change(layout, env, "retarget", SEEDS_KEY, _goal("smoke@x3"))
    layout.record = env.store.load(layout.record.id)
    detail = _record(layout, "run-2", rows, key=SEEDS_KEY)
    # The failure was recorded under the previous revision: it is not folded.
    outcomes = [run["outcome"] for run in detail[LEDGER_DETAIL_KEY]["runs"]]
    assert outcomes == ["passed", "passed", "missing"]
    assert detail[LEDGER_DETAIL_KEY]["source_evidence"] == []


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
    from booley.goals.test_run_resolving import ensure_test_run_resolutions

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
        lambda work_dir, target: loading.load_target_test_plan(work_dir, target, adapter="icarus"),
    )
    view = create_proposal(
        env,
        layout.record.id,
        {"kind": "add", "after": _goal("smoke@x2"), "rationale": "more seeds"},
        session_key="test-session",
    )
    with pytest.raises(ProposalError, match="seed"):
        approve(layout, env, view.proposal.id)
