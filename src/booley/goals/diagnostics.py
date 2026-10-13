"""Bounded human errors retain repair locations without internal Goal identities."""

from __future__ import annotations

from booley.goals.presentation import UNAVAILABLE_GOAL
from booley.goals.proposal_errors import ProposalError
from booley.goals.state_store import GoalStateError
from booley.goals.store import GoalRecordCorruptError


def unavailable_diagnostic(error: BaseException) -> str:
    """Keep file locations; raw validation reasons can contain opaque evidence keys."""
    paths = (
        tuple(entry.path for entry in error.entries)
        if isinstance(error, GoalRecordCorruptError)
        else (error.path,)
        if isinstance(error, (GoalStateError, ProposalError)) and error.path is not None
        else ()
    )
    location = "; inspect: " + ", ".join(str(path)[:1024] for path in paths[:4]) if paths else ""
    return UNAVAILABLE_GOAL + location


def is_unnamable_error(error: BaseException) -> bool:
    return isinstance(error, (GoalRecordCorruptError, GoalStateError)) or (
        isinstance(error, ProposalError) and error.path is not None
    )
