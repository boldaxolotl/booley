"""The locked public Goal proposal lifecycle, independent of MCP/SDK transport."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from booley.goals.apply import ChangeEnvironment, decide, recover
from booley.goals.apply_barrier import require_no_apply
from booley.goals.binding import protected_drift
from booley.goals.change_policy import conflict, prepare_params, translate_after, validate_kind
from booley.goals.changes import Approval, ChangeKind
from booley.goals.checkout import GoalCheckout, branch_ref
from booley.goals.model import GoalRecord, GoalState
from booley.goals.paths import record_paths
from booley.goals.proposals import (
    Decision,
    Proposal,
    ProposalError,
    ProposalView,
    load_proposal,
    new_proposal,
    save_new,
    text,
)
from booley.goals.protected_inputs import ProtectedInputRoots, snapshot_protected_inputs
from booley.goals.state_store import load_goal_state
from booley.runtime.timefmt import utc_now_rfc3339


def resolve_record(env: ChangeEnvironment, work_dir: Path) -> GoalRecord:
    """An explicit absolute worktree selects a record; sessions never select it."""
    if not work_dir.is_absolute():
        raise ProposalError("work_dir must be an absolute worktree path")
    record = env.store.active_for_worktree(work_dir)
    if record is None:
        raise ProposalError("this worktree has no active Goal Mode")
    if record.state is not GoalState.ACTIVE:
        raise ProposalError(f"Goal Mode {record.id} is {record.state.value}")
    return record


def create_proposal(
    env: ChangeEnvironment,
    record_id: str,
    arguments: Mapping[str, Any],
    *,
    session_key: str | None,
) -> ProposalView:
    """Recover first, then prepare/display one exact immutable request under lock."""
    store = env.store
    recover(store, record_id, env)
    with store.record_lock(record_id) as lock:
        record = store.load(record_id)
        require_no_apply(lock.record_dir)
        _require_inputs(record, env)
        proposal = _prepare_proposal(env, record, arguments, session_key)
        reason = conflict(proposal, record)
        if reason:
            raise ProposalError(reason)
        return save_new(lock, proposal)


def _prepare_proposal(
    env: ChangeEnvironment,
    record: GoalRecord,
    arguments: Mapping[str, Any],
    session_key: str | None,
) -> Proposal:
    kind = ChangeKind(arguments.get("kind"))
    key = str(arguments.get("goal_key") or "")
    before = next((goal.spec for goal in record.goals if goal.spec.key == key), None)
    waiver = None
    after_arg = None
    if kind is ChangeKind.WAIVER:
        if before is None or env.waivers is None:
            raise ProposalError(
                "waiver needs an existing coverage goal_key and Goal waiver service"
            )
        after = before
        waiver = env.waivers.bind(record, key, text(arguments.get("candidate_id"), "candidate_id"))
        state = load_goal_state(env.store, record)
        params = dict(state.criteria[key].params)
    else:
        raw = arguments.get("after")
        if not isinstance(raw, Mapping):
            raise ProposalError("after must be a typed Goal argument")
        after_arg = dict(cast("Mapping[str, Any]", raw))
        after = translate_after(after_arg)
        if kind is ChangeKind.ADD:
            key, before = after.key, None
        validate_kind(kind, before, after)
        params = prepare_params(
            record, after, env.entry, preserve=kind is ChangeKind.RELAX, old_key=key
        )
    return new_proposal(
        record,
        kind=kind,
        goal_key=key,
        before=before,
        after=after,
        after_arg=after_arg,
        runtime_params=params,
        rationale=text(arguments.get("rationale"), "rationale"),
        session_key=session_key,
        waiver=waiver,
    )


def resume_proposal(env: ChangeEnvironment, record_id: str, proposal_id: str) -> ProposalView:
    """Recover a durable apply, then look up the same saved proposal after restart."""
    store = env.store
    recover(store, record_id, env)
    with store.record_lock(record_id):
        view = load_proposal(record_paths(env.entry.project_dir, record_id).root, proposal_id)
        record = store.load(record_id)
        if view.proposal.record_id != record_id or view.proposal.worktree != record.worktree:
            raise ProposalError("proposal does not belong to this record/worktree")
        return view


def record_decision(
    env: ChangeEnvironment,
    record_id: str,
    proposal_id: str,
    *,
    answer: Literal["approve", "reject"],
    reason: str,
    source: Approval,
    quote: str | None,
    session_key: str | None,
    peer_process: str | None = None,
) -> ProposalView:
    """Exact-ID agent-recorded and elicited decisions share the same locked policy."""
    store = env.store
    recover(store, record_id, env)
    with store.record_lock(record_id) as lock:
        record = store.load(record_id)
        view = load_proposal(lock.record_dir, proposal_id)
        if view.proposal.record_id != record_id or view.proposal.worktree != record.worktree:
            raise ProposalError("proposal belongs to another record/worktree")
        if answer == "approve":
            _require_inputs(record, env)
        decision = Decision(
            answer,
            text(reason, "reason"),
            source,
            None if quote is None else text(quote, "approval_quote"),
            utc_now_rfc3339(),
            view.proposal.payload_digest,
            session_key,
            peer_process=peer_process,
        )
        return decide(lock, view, decision, env)


def _require_inputs(record: GoalRecord, env: ChangeEnvironment) -> None:
    root = Path(record.worktree_path)
    if record.state is not GoalState.ACTIVE or GoalCheckout(root).head_ref() != branch_ref(
        record.branch
    ):
        raise ProposalError("Goal Mode must be active on its recorded Goal Branch")
    if env.store.identify_worktree(root) != record.worktree:
        raise ProposalError("the recorded Worktree Identity changed")
    reason = protected_drift(
        record, snapshot_protected_inputs(ProtectedInputRoots(root, env.entry.project_dir))
    )
    if reason:
        raise ProposalError(reason)
