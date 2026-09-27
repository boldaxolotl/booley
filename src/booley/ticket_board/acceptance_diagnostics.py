"""Shared classification and guidance for accepted participant-head drift."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, order=True)
class ParticipantHeadLocation:
    """Display metadata for one participant in an accepted Ticket."""

    role: str
    ticket_ref: str
    worktree: Path


@dataclass(frozen=True)
class AcceptanceHeadDrift:
    """Immutable frozen-versus-live participant-head disagreement."""

    frozen_heads: tuple[tuple[str, str], ...]
    live_heads: tuple[tuple[str, str], ...]
    participants: tuple[ParticipantHeadLocation, ...]


class StaleAcceptanceError(RuntimeError):
    """An accepted Ticket's live participant heads no longer match its CSR."""

    def __init__(self, drift: AcceptanceHeadDrift) -> None:
        super().__init__("Ticket heads changed after acceptance")
        self.drift = drift


def compare_accepted_heads(
    frozen_heads: Mapping[str, str],
    live_heads: Mapping[str, str],
    participants: Sequence[ParticipantHeadLocation],
) -> AcceptanceHeadDrift | None:
    """Return deterministic drift details, or ``None`` when all heads match."""
    frozen = tuple(sorted(frozen_heads.items()))
    live = tuple(sorted(live_heads.items()))
    if frozen == live:
        return None
    return AcceptanceHeadDrift(
        frozen_heads=frozen,
        live_heads=live,
        participants=tuple(sorted(participants)),
    )


def format_stale_acceptance(
    slug: str,
    drift: AcceptanceHeadDrift,
    *,
    status: str = "review",
) -> str:
    """Render the complete, status-aware stale-acceptance refusal."""
    frozen = dict(drift.frozen_heads)
    live = dict(drift.live_heads)
    lines = [
        "STALE ACCEPTANCE — Ticket heads changed after acceptance.",
        "The Criteria Satisfaction Record is immutable; this acceptance cannot be "
        "regenerated, extended, or honored for the changed heads.",
        "Frozen and live participant heads:",
    ]
    for participant in drift.participants:
        role = participant.role
        lines.extend(
            [
                f"- {role} frozen: {frozen.get(role, '<missing>')}",
                f"  {role} live: {live.get(role, '<missing>')}",
                f"  Ticket ref: {participant.ticket_ref}",
                f"  Worktree: {participant.worktree}",
            ]
        )
    if status == "review":
        lines.extend(
            [
                "Option 1 — restore the exact frozen heads: first preserve every "
                "post-acceptance commit on a separate safety branch, then move each "
                "named Ticket ref and worktree to its listed frozen commit. A new git "
                "revert commit does not restore the accepted head.",
                "Option 2 — start a destructive clean execution with "
                f'`booley board reset {slug} --reason "<why a clean run is required>"`. '
                "Reset discards the reviewed Ticket worktree/branch and may refuse if "
                "another Ticket lifecycle precondition is not satisfied.",
            ]
        )
    elif status == "done":
        lines.append(
            "This Ticket is already done. Inspect the Acceptance Journal and the named "
            "Ticket refs/worktrees before retrying terminal recovery."
        )
    else:
        lines.append(
            "Acceptance publication is incomplete. Inspect the Criteria Satisfaction "
            "Record, Acceptance Journal, and named Ticket refs/worktrees before retrying."
        )
    return "\n".join(lines)
