"""Thin facade over TicketOps — preserves all existing import paths.

Previously each function spawned ``python -m ticket_board`` as a
subprocess.  Now they delegate to :func:`get_ticket_ops` which returns
a :class:`DirectTicketOps` (in-process) by default, or a test mock
injected via :func:`set_ticket_ops`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._ticket_ops import (
    CreateTicketParams,
    DirectTicketOps,
    TicketCLIError,
    TicketOps,
    get_ticket_ops,
    set_ticket_ops,
)

__all__ = [
    "DirectTicketOps",
    "TicketCLIError",
    "TicketOps",
    "activate",
    "block",
    "claim",
    "classify",
    "collect_evidence",
    "complete",
    "create_ticket_file",
    "enqueue",
    "fail",
    "generate_slug",
    "get_ticket_ops",
    "handoff",
    "init_ticket",
    "log_incident",
    "next_step",
    "parse_ticket",
    "promote_waiting",
    "resume",
    "set_ticket_ops",
    "ticket_status",
    "timing",
    "unblock",
    "update_board",
    "validate_logs",
    "validate_ticket",
]


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------


def classify(project_root: Path) -> dict[str, Any]:
    """Classify tickets into executable/blocked/waiting/review/orphaned."""
    return get_ticket_ops().classify(project_root)


def parse_ticket(project_root: Path, path: str) -> dict[str, Any]:
    """Parse ticket YAML frontmatter -> JSON."""
    return get_ticket_ops().parse_ticket(project_root, path)


def validate_ticket(project_root: Path, path: str, *, check_git: bool = False) -> dict[str, Any]:
    """Validate ticket fields. Returns {"errors": [...], "valid": bool}."""
    return get_ticket_ops().validate_ticket(project_root, path, check_git=check_git)


def resume(project_root: Path, slug: str) -> dict[str, Any]:
    """Detect resume state for a ticket."""
    return get_ticket_ops().resume(project_root, slug)


def next_step(project_root: Path, type_or_slug: str, current: str, skip: str = "") -> str | None:
    """Get next Booley Flow or Specialist. Returns step name or None."""
    return get_ticket_ops().next_step(project_root, type_or_slug, current, skip)


def collect_evidence(project_root: Path, slug: str) -> dict[str, Any]:
    """Collect tamper-resistant evidence bundle."""
    return get_ticket_ops().collect_evidence(project_root, slug)


# ---------------------------------------------------------------------------
# State-changing commands
# ---------------------------------------------------------------------------


def activate(
    project_root: Path,
    slug: str,
    *,
    owner_pid: int | None = None,
    execution_id: str | None = None,
) -> bool:
    """Activate a ticket for execution (move to active/).

    Returns False if another live runner already owns the ticket.
    """
    if execution_id is None:
        return get_ticket_ops().activate(project_root, slug, owner_pid=owner_pid)
    return get_ticket_ops().activate(
        project_root, slug, owner_pid=owner_pid, execution_id=execution_id
    )


def claim(project_root: Path, slug: str) -> bool:
    """Atomically claim a queued ticket. Returns True on success."""
    return get_ticket_ops().claim(project_root, slug)


def init_ticket(
    project_root: Path,
    ticket_path: str,
    *,
    execution_id: str = "",
    owner_pid: int | None = None,
) -> dict[str, Any]:
    """Initialize a fresh ticket for execution."""
    if not execution_id and owner_pid is None:
        return get_ticket_ops().init_ticket(project_root, ticket_path)
    return get_ticket_ops().init_ticket(
        project_root, ticket_path, execution_id=execution_id, owner_pid=owner_pid
    )


def update_board(
    project_root: Path,
    slug: str,
    *,
    set_fields: dict[str, str] | None = None,
    append_step: str = "",
    reset_steps: bool = False,
    reset_steps_from: str = "",
    log: bool = False,
) -> None:
    """Update ticket frontmatter. Enforces step gates on --append-step."""
    get_ticket_ops().update_board(
        project_root,
        slug,
        set_fields=set_fields,
        append_step=append_step,
        reset_steps=reset_steps,
        reset_steps_from=reset_steps_from,
        log=log,
    )


def log_incident(
    project_root: Path,
    slug: str,
    *,
    incident_type: str,
    step: str,
    description: str,
    resolution: str = "unresolved",
) -> None:
    """Append an incident to incidents.md."""
    get_ticket_ops().log_incident(
        project_root,
        slug,
        incident_type=incident_type,
        step=step,
        description=description,
        resolution=resolution,
    )


def block(
    project_root: Path,
    slug: str,
    *,
    reason: str,
    step: str,
    expected_execution_id: str | None = None,
) -> None:
    """Block a ticket with reason and step."""
    if expected_execution_id is None:
        get_ticket_ops().block(project_root, slug, reason=reason, step=step)
        return
    get_ticket_ops().block(
        project_root, slug, reason=reason, step=step, expected_execution_id=expected_execution_id
    )


def fail(project_root: Path, slug: str, *, error: str, step: str) -> None:
    """Fail a ticket with error and step."""
    get_ticket_ops().fail(project_root, slug, error=error, step=step)


def handoff(
    project_root: Path,
    slug: str,
    *,
    expected_execution_id: str | None = None,
) -> None:
    """Hand off ticket to review."""
    if expected_execution_id is None:
        get_ticket_ops().handoff(project_root, slug)
        return
    get_ticket_ops().handoff(
        project_root, slug, expected_execution_id=expected_execution_id
    )


def unblock(
    project_root: Path,
    slug: str,
    *,
    feedback: str = "",
    actor: str = "ticket-triage",
    detail: str = "user answered questions",
    feedback_heading: str = "Human Response",
) -> bool:
    """Move a blocked ticket back to the queue. False if it is not blocked."""
    return get_ticket_ops().unblock(
        project_root,
        slug,
        feedback=feedback,
        actor=actor,
        detail=detail,
        feedback_heading=feedback_heading,
    )


def ticket_status(project_root: Path, slug: str) -> str:
    """Current board status of a ticket, or "" when it is not on the board."""
    return get_ticket_ops().ticket_status(project_root, slug)


def promote_waiting(project_root: Path) -> str:
    """Check waiting tickets and return newly executable ones."""
    return get_ticket_ops().promote_waiting(project_root)


def validate_logs(project_root: Path, slug: str) -> tuple[bool, str]:
    """Validate log artifacts. Returns (valid, report)."""
    return get_ticket_ops().validate_logs(project_root, slug)


def timing(project_root: Path, slug: str, *, save: bool = False) -> str:
    """Generate timing report. Returns markdown."""
    return get_ticket_ops().timing(project_root, slug, save=save)


def generate_slug(project_root: Path, summary: str) -> str:
    """Generate a ticket slug from a summary string."""
    return get_ticket_ops().generate_slug(project_root, summary)


def create_ticket_file(project_root: Path, slug: str, params: CreateTicketParams) -> Path:
    """Create a ticket file via ticket_board create-file."""
    return get_ticket_ops().create_ticket_file(project_root, slug, params)


def enqueue(
    project_root: Path,
    slug: str,
    *,
    summary: str | None = None,
    ticket_type: str | None = None,
    branch: str | None = None,
    on_success: dict | None = None,
    integration_base: str = "",
) -> None:
    """Enqueue a ticket via ticket_board enqueue."""
    get_ticket_ops().enqueue(
        project_root,
        slug,
        summary=summary,
        ticket_type=ticket_type,
        branch=branch,
        on_success=on_success,
        integration_base=integration_base,
    )


def complete(project_root: Path, slug: str) -> None:
    """Complete a ticket: approve, merge/cleanup per on_success from frontmatter."""
    get_ticket_ops().complete(project_root, slug)
