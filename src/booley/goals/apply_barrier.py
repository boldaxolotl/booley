"""Read-only pending-apply barrier shared by every normal evidence path."""

from pathlib import Path

from booley.goals.changes import read_change_log
from booley.goals.proposals import ProposalError, list_proposals


def pending_applies(root: Path) -> tuple[str, ...]:
    """Observational barrier: include approved-before-intent and finalization gaps."""
    log = read_change_log(root / "changes.jsonl")
    approved = {view.proposal.id for view in list_proposals(root) if view.state == "approved"}
    return tuple(sorted(approved | {entry.id for entry in log.interrupted}))


def require_no_apply(root: Path) -> None:
    """Admission/publication/replay callers hold the record lock before checking."""
    pending = pending_applies(root)
    if pending:
        raise ProposalError(
            f"Goal apply interrupted ({', '.join(pending)}); call goal_propose_change to recover before running or publishing evidence"
        )
