"""Recoverable Ticket acceptance journal.

Keep the journal model cheap to import.  Runtime modules import ticket-board
paths while spawning worker processes, and those imports must not pull in the
repository/FuseSoC acceptance machinery unless an acceptance operation is
actually requested.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._model import AcceptanceJournalError, JournalState, acceptance_state, initial_journal

if TYPE_CHECKING:
    from ..acceptance_basis import AcceptanceBasis
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


def completion_basis_sources(
    root: Path,
    slug: str,
    basis: AcceptanceBasis,
    *,
    expected_sources: Mapping[str, str],
) -> dict[str, str] | None:
    """Return journal-pinned sources after validating recorded destinations."""
    from ..acceptance_basis import validate_destination_refs
    from ._model import AcceptanceJournalError, load_persisted_journal
    from ._store import journal_path

    path = journal_path(root.resolve(), slug)
    if not path.exists():
        return None
    journal = load_persisted_journal(path)
    if not journal.sources:
        return None
    if dict(journal.sources) != expected_sources:
        raise AcceptanceJournalError(
            "Acceptance Journal sources differ from the accepted Ticket heads"
        )
    destinations = {
        participant.role: participant.destination_sha for participant in basis.participants
    }
    for role in journal.published:
        finalized = journal.candidates[role].finalized_sha
        if finalized is None:
            raise AcceptanceJournalError(f"Acceptance Journal has no finalized {role} destination")
        destinations[role] = finalized
    validate_destination_refs(root, basis, destinations)
    return dict(journal.sources)


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
    "completion_basis_sources",
    "initial_journal",
]
