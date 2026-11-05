"""What Finish's first cleanliness check would refuse, read without finishing."""

from __future__ import annotations

from pathlib import Path

from booley.goals.completion_history import fresh_attempt_retained_outputs
from booley.goals.input_view import UncommittedRepository, select_inputs, uncommitted_participants
from booley.goals.model import GoalState
from booley.goals.store import GoalStore


def uncommitted_for_finish(
    store: GoalStore, record_id: str, root: Path
) -> tuple[UncommittedRepository, ...] | None:
    """The repositories whose uncommitted changes would refuse Finish from *root*.

    Applies the very predicate Finish applies, including its exemptions (the
    Paired Checkout's own entry in the outer repository, pin-matching edits,
    and the Session Summaries of earlier ``revalidation_required`` attempts
    that a fresh ``operation_id`` retains). Returns ``None`` when the record
    is no longer ACTIVE under its lock: a FINISHING record's own files are
    Finish's business, not the agent's uncommitted work.

    Raises whatever input selection and Git raise; the caller reports it.
    """
    with store.record_lock(record_id) as lock:
        record = store.load(record_id)
        if record.state != GoalState.ACTIVE:
            return None
        selection = select_inputs(record, root)
        retained = fresh_attempt_retained_outputs(store, lock, record, root)
        return uncommitted_participants(selection, retained=retained)
