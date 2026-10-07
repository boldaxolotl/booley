"""Typed change-kind policy and entry-quality preparation at the original base."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.criteria.thresholds import describe_threshold, has_relative_threshold
from booley.evidence.acceptance import PairedProjectBaseline
from booley.flows.baseline_worktree import baseline_worktree
from booley.goals.changes import ChangeKind
from booley.goals.checkout import GoalCheckout
from booley.goals.entry import EntryEnvironment, check_goal_targets, pin_goal_baselines
from booley.goals.model import GoalFamily, GoalRecord, GoalSpec, parse_goal_arg
from booley.goals.proposals import Proposal, ProposalError
from booley.goals.state_store import goal_criterion_params, load_goal_state
from booley.goals.store import GoalStore
from booley.goals.translate import stricter_threshold, translate_goals


def translate_after(raw: Mapping[str, Any]) -> GoalSpec:
    """One typed public GoalArg, never an arbitrary internal specification."""
    return translate_goals((parse_goal_arg(raw),)).goals[0]


def conflict(proposal: Proposal, record: GoalRecord) -> str:
    """An observational conflict explanation, usable by status and decisions."""
    if proposal.record_id != record.id or proposal.worktree != record.worktree:
        return "proposal belongs to another Goal record/worktree"
    current = {goal.spec.key: goal.spec for goal in record.goals}
    if proposal.before is not None and any(
        goal.spec.key == proposal.goal_key and goal.spec_revision > proposal.record_revision
        for goal in record.goals
    ):
        return "the Goal specification revision changed since this proposal"
    if proposal.before is not None and current.get(proposal.goal_key) != proposal.before:
        return "the Goal changed since this proposal; reject it and create a new proposal"
    if proposal.kind is ChangeKind.ADD and proposal.after.key in current:
        return "the added Goal's destination is occupied"
    others = [
        goal
        for key, goal in current.items()
        if key != proposal.goal_key or proposal.kind is ChangeKind.ADD
    ]
    if any(same_slot(proposal.after, other) for other in others):
        return "the destination conflicts with an existing Goal (including review clean/done)"
    return ""


def same_slot(left: GoalSpec, right: GoalSpec) -> bool:
    """Review clean/done share one slot even though their Criterion keys differ."""
    return left.key == right.key or (
        left.family is GoalFamily.REVIEW
        and right.family is GoalFamily.REVIEW
        and left.key.rsplit("_", 1)[0] == right.key.rsplit("_", 1)[0]
    )


def validate_kind(kind: ChangeKind, before: GoalSpec | None, after: GoalSpec) -> None:
    """Reject incomparable substitutions disguised as a relaxation or retarget."""
    if kind is ChangeKind.ADD:
        if before is not None:
            raise ProposalError("add must not name an existing Goal")
        return
    if before is None:
        raise ProposalError("this change needs an existing goal_key")
    if before.family != after.family:
        raise ProposalError("a change cannot replace the Goal family")
    if kind is ChangeKind.WAIVER:
        if before != after or before.family is not GoalFamily.COVERAGE:
            raise ProposalError("a waiver must bind an existing coverage Goal")
        return
    if kind is ChangeKind.RELAX:
        _validate_relax(before, after)
    else:
        _validate_retarget(before, after)
    if before == after:
        raise ProposalError("the proposal does not change the Goal")


def _ordered_bounds(goal: GoalSpec) -> dict[str, Any]:
    if goal.family is GoalFamily.COVERAGE:
        return {key: value["min_pct"] for key, value in goal.params["metrics"].items()}
    if goal.family is GoalFamily.MUTATION:
        # The producer's omitted fixed count is 10; its omitted floor is that count.
        default = None if goal.params.get("auto") else goal.params.get("total", 10)
        return {"min_detected": goal.params.get("min_detected", default)}
    return {
        key: value
        for key, value in goal.params.items()
        if describe_threshold(key.rpartition(".")[2]) is not None
    }


def _fixed(goal: GoalSpec) -> dict[str, Any]:
    bounds = _ordered_bounds(goal)
    return {
        key: value for key, value in goal.params.items() if key not in bounds and key != "metrics"
    }


def _validate_relax(before: GoalSpec, after: GoalSpec) -> None:
    if before.target != after.target or _fixed(before) != _fixed(after):
        raise ProposalError(
            "relaxation must preserve Target, tests, baseline, recipe, spec and mutation scope"
        )
    if before.family is GoalFamily.REVIEW:
        if (
            before.key.endswith("_clean")
            and after.key == before.key.removesuffix("_clean") + "_done"
        ):
            return
        raise ProposalError("review relaxation is only clean -> done for the same review/spec")
    if before.key != after.key:
        raise ProposalError("numeric relaxation must keep the same Goal key")
    old, new = _ordered_bounds(before), _ordered_bounds(after)
    if set(old) != set(new):
        raise ProposalError("relaxation must keep the same ordered bound names")
    if not old or any(
        _stricter(before.family, key, new[key], value) for key, value in old.items()
    ):
        raise ProposalError("relaxation must only weaken ordered numeric bounds")


def _stricter(family: GoalFamily, key: str, candidate: Any, current: Any) -> bool:
    if family is GoalFamily.MUTATION and (candidate is None or current is None):
        raise ProposalError(
            "relaxation cannot order an implicit auto mutation floor; use explicit detection floors"
        )
    if family in {GoalFamily.COVERAGE, GoalFamily.MUTATION}:
        return bool(candidate > current)
    return stricter_threshold(key, candidate, current)


def _validate_retarget(before: GoalSpec, after: GoalSpec) -> None:
    if _ordered_bounds(before) != _ordered_bounds(after):
        raise ProposalError("retarget must preserve acceptance bounds")
    if before.family is GoalFamily.REVIEW:
        if before.key != after.key or not before.params.get("spec"):
            raise ProposalError(
                "retarget must preserve review kind and verdict; only spec review has a binding"
            )
        return
    fixed_before, fixed_after = _fixed(before), _fixed(after)
    for key in ("target", "test", "tests", "baseline_target", "_baseline_target"):
        fixed_before.pop(key, None)
        fixed_after.pop(key, None)
    if fixed_before != fixed_after:
        raise ProposalError("retarget only changes Target and coupled test/baseline selection")


def prepare_params(
    record: GoalRecord, after: GoalSpec, env: EntryEnvironment, *, preserve: bool, old_key: str
) -> dict[str, Any]:
    """Prepare new bindings against entry's original commits; retain old runtime pins on relax."""
    params = goal_criterion_params((after,))
    if preserve:
        state = load_goal_state(GoalStore(env.project_dir), record)
        entry = state.criteria.get(old_key)
        if entry is None:
            raise ProposalError(
                "the existing Goal runtime params are missing; cannot preserve its pins"
            )
        return {**entry.params, **after.params}
    root = Path(record.worktree_path)
    relative = has_relative_threshold(after.params)
    if relative:
        paired = (
            None
            if record.paired_project_base_sha is None
            else PairedProjectBaseline.entry_pinned(record.paired_project_base_sha)
        )
        with baseline_worktree(root, record.base_sha, paired_project=paired) as baseline:
            check_goal_targets(root, (after,), baseline_root=baseline)
    else:
        check_goal_targets(root, (after,))
    pin_goal_baselines(record, params, env, GoalCheckout(root))
    return params[after.key]
