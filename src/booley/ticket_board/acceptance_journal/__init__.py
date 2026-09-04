"""Recoverable Ticket acceptance journal.

Keep the journal model cheap to import.  Runtime modules import ticket-board
paths while spawning worker processes, and those imports must not pull in the
repository/FuseSoC acceptance machinery unless an acceptance operation is
actually requested.
"""

from typing import TYPE_CHECKING, Any

from ._model import AcceptanceJournalError, JournalState, acceptance_state

if TYPE_CHECKING:
    from ._advance import (
        AcceptanceOperationError,
        AcceptanceOutcome,
        AcceptanceProgress,
        AcceptanceRecoveryBlockedError,
        AcceptanceRequest,
        advance_acceptance,
        cleanup_finished,
    )

_ADVANCE_EXPORTS = frozenset(
    {
        "AcceptanceOperationError",
        "AcceptanceOutcome",
        "AcceptanceProgress",
        "AcceptanceRecoveryBlockedError",
        "AcceptanceRequest",
        "advance_acceptance",
        "cleanup_finished",
    }
)


def __getattr__(name: str) -> Any:
    """Load repository-backed acceptance operations only when requested."""
    if name not in _ADVANCE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import _advance

    value = getattr(_advance, name)
    globals()[name] = value
    return value


__all__ = [
    "AcceptanceJournalError",
    "AcceptanceOperationError",
    "AcceptanceOutcome",
    "AcceptanceProgress",
    "AcceptanceRecoveryBlockedError",
    "AcceptanceRequest",
    "JournalState",
    "acceptance_state",
    "advance_acceptance",
    "cleanup_finished",
]
