"""Ticket Board policy for accepting review Tickets with an Acceptance Basis."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_str
from booley.runtime.file_lock import LockContentionError

from .acceptance_basis import AcceptanceBasis, AcceptanceBasisError
from .acceptance_journal import (
    AcceptanceJournalError,
    AcceptanceOperationError,
    AcceptanceOutcome,
    AcceptanceProgress,
    AcceptanceRecoveryBlockedError,
    AcceptanceRequest,
    advance_acceptance,
)
from .validation import retired_ticket_field_errors

CompletionError = AcceptanceOperationError


def _destination_branch(entry: Mapping[str, Any], basis: AcceptanceBasis) -> str:
    try:
        branch = require_str(entry, "branch")
    except BoundaryError as exc:
        raise CompletionError("Ticket has no destination branch") from exc
    outer = next(item for item in basis.participants if item.role == "outer")
    if outer.destination_ref != f"refs/heads/{branch}":
        raise CompletionError(
            f"Ticket destination branch {branch!r} differs from recorded "
            f"destination {outer.destination_ref!r}"
        )
    return branch


def _validate_completion_plan(basis: AcceptanceBasis, *, cleanup: bool) -> None:
    if not cleanup:
        return
    for participant in basis.participants:
        if participant.ticket_ref == participant.destination_ref:
            raise CompletionError(
                f"cannot clean {participant.role} participant because its Ticket ref "
                "is also the destination ref"
            )


def _completion_inputs(
    tio: Any, slug: str, effective_policy: Any
) -> tuple[Mapping[str, Any], AcceptanceBasis] | None:
    if getattr(effective_policy, "merge", None) is not True:
        raise CompletionError("journaled completion requires merge policy to be true")
    if not isinstance(getattr(effective_policy, "cleanup", None), bool):
        raise CompletionError("journaled completion requires cleanup policy to be boolean")
    removal_targets = tuple(getattr(effective_policy, "remove_targets", ()))
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return None
    status = entry.get("status", "")
    if status not in {"review", "done"}:
        print(
            f"Error: cannot complete '{slug}' from status '{status}'; must be in review",
            file=sys.stderr,
        )
        return None
    retired_errors = retired_ticket_field_errors(entry)
    if retired_errors:
        print(f"Error: cannot complete '{slug}': {retired_errors[0]}", file=sys.stderr)
        return None
    try:
        basis = tio.load_basis(slug)
        if removal_targets != basis.removal_targets:
            raise AcceptanceBasisError(
                "on_success.remove_targets changed after Acceptance Basis publication"
            )
        _destination_branch(entry, basis)
        _validate_completion_plan(basis, cleanup=effective_policy.cleanup)
    except (AcceptanceBasisError, CompletionError) as exc:
        print(f"Error: cannot complete '{slug}': {exc}", file=sys.stderr)
        return None
    return entry, basis


def _request(
    tio: Any,
    slug: str,
    entry: Mapping[str, Any],
    basis: AcceptanceBasis,
    *,
    cleanup: bool,
    expected_sources: Mapping[str, str] | None = None,
) -> AcceptanceRequest:
    ticket_name = Path(str(entry["file"])).name
    allowed_board_rename = (
        tio.tickets_dir / "board" / "queue" / ticket_name,
        tio.tickets_dir / str(entry["file"]),
    )
    return AcceptanceRequest(
        root=Path(tio._project_root).resolve(),
        slug=slug,
        basis=basis,
        cleanup=cleanup,
        ticket_status=entry["status"],
        allowed_board_rename=allowed_board_rename,
        expected_sources=expected_sources,
    )


def _approve(tio: Any, slug: str) -> bool:
    return tio.move_and_update(
        slug,
        "done",
        {"step": "complete"},
        transition=(
            "review:summary",
            "done:complete",
            "op-complete",
            "terminal actions",
        ),
        enforce_lifecycle=True,
        expected_status="review",
    )


def _ticket_after_approval(tio: Any, slug: str) -> Mapping[str, Any]:
    current = tio.find_ticket(slug)
    if current is None:
        raise CompletionError(
            f"Ticket Board outcome for {slug!r} is uncertain after approval; inspect and retry"
        )
    status = current.get("status", "")
    if status == "review":
        raise CompletionError(
            "repository publication succeeded but the board transition failed; retry"
        )
    if status != "done":
        raise CompletionError(
            f"repository publication succeeded but Ticket status is {status!r}; inspect and retry"
        )
    return current


def _finish_progress(
    tio: Any,
    slug: str,
    entry: Mapping[str, Any],
    basis: AcceptanceBasis,
    cleanup: bool,
    progress: AcceptanceProgress,
    expected_sources: Mapping[str, str] | None,
) -> AcceptanceProgress:
    if progress.outcome is not AcceptanceOutcome.APPROVAL_REQUIRED:
        return progress
    approval_error: Exception | None = None
    try:
        approved = _approve(tio, slug)
    except Exception as exc:  # noqa: BLE001 - TicketIO is an external adapter boundary.
        approval_error = exc
    else:
        if not approved:
            approval_error = CompletionError("Ticket Board approval returned no durable result")
    try:
        current = _ticket_after_approval(tio, slug)
    except Exception as status_error:
        if approval_error is not None:
            raise status_error from approval_error
        raise
    return advance_acceptance(
        _request(
            tio,
            slug,
            current,
            basis,
            cleanup=cleanup,
            expected_sources=expected_sources,
        )
    )


def _report_failure(tio: Any, slug: str, exc: Exception) -> bool:
    try:
        current = tio.find_ticket(slug)
    except (OSError, ValueError) as status_exc:
        print(
            f"Error: completion outcome for '{slug}' is uncertain: {exc}; "
            f"Ticket status could not be read: {status_exc}",
            file=sys.stderr,
        )
        return False
    status = current.get("status", "") if current else ""
    if status == "done" and "policy changed" in str(exc):
        print(
            f"Error: Ticket '{slug}' is done but acceptance recovery is blocked: {exc}",
            file=sys.stderr,
        )
        return False
    if status == "done" and isinstance(exc, AcceptanceRecoveryBlockedError):
        print(
            f"Error: Ticket '{slug}' is done but acceptance recovery is blocked: {exc}",
            file=sys.stderr,
        )
        return False
    if status == "done":
        print(
            f"Warning: Ticket '{slug}' is done but acceptance recovery is pending "
            f"or unverified: {exc}",
            file=sys.stderr,
        )
        return True
    if status == "review":
        print(f"Error: completion failed for '{slug}': {exc}", file=sys.stderr)
        return False
    print(
        f"Error: completion outcome for '{slug}' is uncertain: {exc}",
        file=sys.stderr,
    )
    return False


def complete_review_ticket(
    tio: Any,
    slug: str,
    effective_policy: Any,
    *,
    expected_sources: Mapping[str, str] | None = None,
) -> bool:
    """Apply Ticket Board policy around recoverable repository acceptance."""
    inputs = _completion_inputs(tio, slug, effective_policy)
    if inputs is None:
        return False
    entry, basis = inputs
    try:
        progress = advance_acceptance(
            _request(
                tio,
                slug,
                entry,
                basis,
                cleanup=effective_policy.cleanup,
                expected_sources=expected_sources,
            )
        )
        progress = _finish_progress(
            tio,
            slug,
            entry,
            basis,
            effective_policy.cleanup,
            progress,
            expected_sources,
        )
    except LockContentionError:
        print("Error: another acceptance is already running", file=sys.stderr)
        return False
    except (
        AcceptanceJournalError,
        AcceptanceOperationError,
        CompletionError,
        OSError,
        ValueError,
    ) as exc:
        return _report_failure(tio, slug, exc)
    if progress.outcome is AcceptanceOutcome.ACCEPTED_PENDING:
        print(
            f"Warning: accepted '{slug}' but cleanup is pending or acceptance "
            f"recovery is incomplete: {progress.detail}",
            file=sys.stderr,
        )
    return True
