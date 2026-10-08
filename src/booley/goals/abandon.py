"""Shared abandonment authority: preserve work, recover committed intents, fence Jobs."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from booley.core.boundary import require_dict, require_list, require_str_value
from booley.goals.apply import recover_locked
from booley.goals.checkout import GoalCheckout, branch_ref
from booley.goals.lifecycle import (
    LifecycleError,
    LifecycleOperation,
    LifecycleRequest,
    bind_operation,
)
from booley.goals.model import GoalRecord, GoalState
from booley.goals.proposals import (
    ProposalView,
    close_by_abandonment,
    list_proposals,
    load_proposal,
)
from booley.goals.store import GoalStore
from booley.runtime.timefmt import utc_now_rfc3339

if TYPE_CHECKING:
    from booley.goals.finish import FinishEnvironment


def abandon_locked(operation: LifecycleOperation, env: FinishEnvironment) -> dict[str, Any]:
    """MCP/CLI share this exact state matrix under worktree then record locks."""
    if not operation.request.instruction_quote.strip():
        raise LifecycleError("abandon requires the human's nonblank instruction_quote")
    from booley.goals.finish import recover_linked_operation

    recovered = recover_linked_operation(operation, env)
    if recovered is not None:
        return recovered
    record = operation.store.load(operation.request.record_id)
    if record.state is GoalState.ABANDONED:
        if record.abandon_operation not in {None, operation.request.operation_id}:
            raise LifecycleError("Goal was abandoned by a different immutable operation")
        _complete_closures(operation, record, env)
        return operation.save_result(_result(operation, record, _saved_notes(operation)))
    if record.state is GoalState.FINISHING:
        from booley.goals.finish import recover_pending_finish

        result = recover_pending_finish(operation, record, env)
        if result["status"] == "finished":
            return operation.save_recovered_result(result)
        record = operation.store.load(record.id)
    notes = _entering_effects(record, operation.root) if record.state is GoalState.ENTERING else []
    if record.state not in {GoalState.ACTIVE, GoalState.ENTERING}:
        raise LifecycleError(f"Goal Mode {record.id} is {record.state.value}; cannot abandon")
    recover_locked(operation.lock, env.change_environment(record), abandoning=True)
    if not _freeze_closures(operation, notes):
        return _abandon_revalidation(operation)
    record = operation.store.load(record.id)
    closed = operation.store.save(
        operation.lock,
        replace(
            record,
            state=GoalState.ABANDONED,
            ended_at=utc_now_rfc3339(),
            publication_floor=record.revision + 1,
            end_instruction_quote=operation.request.instruction_quote,
            abandon_operation=operation.request.operation_id,
        ),
    )
    env.checkpoint("abandoned")
    _complete_closures(operation, closed, env)
    return operation.save_result(_result(operation, closed, notes))


def _freeze_closures(operation: LifecycleOperation, notes: list[str]) -> bool:
    current = {
        "notes": notes,
        "closures": [
            {
                "proposal_id": view.proposal.id,
                "metadata": view.metadata(),
                "payload_digest": view.proposal.payload_digest,
            }
            for view in list_proposals(operation.lock.record_dir)
            if not view.closed_by_abandonment and view.state in {"pending", "approved"}
        ],
    }
    if (operation.directory / "abandonment.authority.json").exists():
        if operation.read_sealed("abandonment") != current:
            return False
    else:
        operation.write_sealed("abandonment", current)
    _closure_views(operation)
    return True


def _abandon_revalidation(operation: LifecycleOperation) -> dict[str, Any]:
    """An old closure snapshot cannot silently adopt later proposal decisions."""
    return operation.save_result(
        {
            "status": "revalidation_required",
            "record_id": operation.request.record_id,
            "operation_id": operation.request.operation_id,
            "reason": "proposals changed after abandonment was captured; retained closure authority",
            "attempt": str(operation.directory),
            "message": "Retry abandonment with a fresh operation_id to audit the current proposals.",
        }
    )


def _closure_views(operation: LifecycleOperation) -> list[ProposalView]:
    value = operation.read_sealed("abandonment")
    if (
        set(value) != {"notes", "closures"}
        or not isinstance(value["closures"], list)
        or not isinstance(value["notes"], list)
        or not all(
            isinstance(item, str)
            for item in require_list(value["notes"], field="abandonment notes")
        )
    ):
        raise LifecycleError("invalid abandonment closure authority")
    views: list[ProposalView] = []
    for raw_row in require_list(value["closures"], field="closures"):
        row = require_dict(raw_row, field="closure")
        if set(row) != {"proposal_id", "metadata", "payload_digest"}:
            raise LifecycleError("invalid abandonment proposal association")
        view = load_proposal(operation.lock.record_dir, row["proposal_id"])
        metadata = {**view.metadata(), "closed_by_abandonment": None}
        if metadata != row["metadata"] or view.proposal.payload_digest != row["payload_digest"]:
            raise LifecycleError("abandonment proposal differs from its immutable association")
        views.append(view)
    return views


def _complete_closures(
    operation: LifecycleOperation, record: GoalRecord, env: FinishEnvironment
) -> None:
    """Closure follows the terminal commit; a failed preterminal call closes nothing."""
    path = operation.directory / "abandonment.authority.json"
    if not path.exists():
        return  # Legacy abandonment recorded no recoverable closure list.
    for view in _closure_views(operation):
        close_by_abandonment(operation.lock, view, closed_at=record.ended_at)
        env.checkpoint("proposal-closed")


def _saved_notes(operation: LifecycleOperation) -> list[str]:
    if (operation.directory / "abandonment.authority.json").exists():
        value = operation.read_sealed("abandonment")
    else:
        # Legacy terminal requests recorded public notes without modern closure authority.
        value = require_dict(
            json.loads((operation.directory / "abandonment.json").read_bytes()),
            field="legacy abandonment",
        )
    return [
        require_str_value(note, field="abandonment note")
        for note in require_list(value.get("notes"), field="abandonment notes")
    ]


def abandon_goal(
    store: GoalStore,
    work_dir: Path,
    env: FinishEnvironment,
    *,
    record_id: str | None = None,
    operation_id: str | None = None,
    instruction_quote: str = "Human invoked booley goal abandon",
    on_bound: Callable[[LifecycleRequest], None] | None = None,
) -> dict[str, Any]:
    """Direct human selection/mint occurs once under the same worktree lock."""
    found = GoalCheckout(work_dir).containing_repository()
    if found is None:
        raise LifecycleError("abandon must run inside a Goal worktree")
    root = found[0]
    identity = store.identify_worktree(root)
    if identity is None:
        raise LifecycleError("worktree hosts no Goal Mode")
    if record_id is not None and operation_id is not None:
        with bind_operation(
            store,
            LifecycleRequest(
                root, record_id, operation_id, abandon=True, instruction_quote=instruction_quote
            ),
        ) as operation:
            if on_bound is not None:
                on_bound(operation.request)
            return operation.saved_result() or abandon_locked(operation, env)
    with store.worktree_lock(identity):
        record = store.active_for_worktree(root)
        if record is None:
            raise LifecycleError(
                "worktree hosts no Goal Mode; supply exact retry IDs for a closed operation"
            )
        if record_id is not None and record_id != record.id:
            raise LifecycleError("expected record is not the active occupant")
        request = LifecycleRequest(
            root,
            record.id,
            operation_id or str(uuid4()),
            abandon=True,
            instruction_quote=instruction_quote,
        )
        from booley.goals.lifecycle import bind_locked

        with store.record_lock(record.id) as lock:
            operation = bind_locked(store, lock, request, root, identity)
            if on_bound is not None:
                on_bound(request)
            return operation.saved_result() or abandon_locked(operation, env)


def _entering_effects(record: GoalRecord, root: Path) -> list[str]:
    """Classify with entry's exact ownership checks; abandonment retains all effects."""
    checkout = GoalCheckout(root)
    tip = checkout.branch_tip(record.branch)
    if tip is None:
        return ["Interrupted entry did not leave a Goal Branch."]
    if not record.branch_created:
        return [
            f"Ambiguous interrupted entry branch {record.branch} retained (ownership was not saved)."
        ]
    if tip != record.base_sha:
        return [f"Goal Branch {record.branch} moved beyond entry; retained."]
    if checkout.head_ref() == branch_ref(record.branch) and checkout.dirty_paths():
        return [f"Interrupted entry has uncommitted work on {record.branch}; retained."]
    return [f"Entry-owned Goal Branch {record.branch} retained at its original base."]


def _result(operation: LifecycleOperation, record: GoalRecord, notes: list[str]) -> dict[str, Any]:
    return {
        "status": "abandoned",
        "record_id": record.id,
        "operation_id": operation.request.operation_id,
        "instruction_quote": operation.request.instruction_quote,
        "notes": notes,
        "message": f"Goal Mode {record.id} abandoned. Branch and files retained."
        + ("\n" + "\n".join(notes) if notes else ""),
    }
