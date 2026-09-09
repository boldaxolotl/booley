"""Validate ticket-bound endpoint evidence independently of the command runner."""

from __future__ import annotations

import os
from pathlib import Path

from booley.ticket_board.acceptance_ledger import read_acceptance
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketIO

from .entry import ReviewEntryError, operation_path, read_json


def validate_recording(work_dir: Path | str | None = None) -> None:
    """Reject stale or cross-ticket scoped endpoint work before state publication."""
    token = os.environ.get("BOOLEY_REVIEW_OPERATION")
    if not token:
        return
    root = Path(os.environ["BOOLEY_CONTROL_PROJECT_ROOT"])
    slug = os.environ["BOOLEY_SLUG"]
    tio = TicketIO(tickets_dir_from_project_root(root), project_root=root)
    log_dir = tio.logs_dir / slug
    operation = read_json(operation_path(log_dir))
    if (
        operation is None
        or operation.get("token") != token
        or operation.get("phase") != "interactive"
    ):
        raise ReviewEntryError("interactive review execution is no longer current")
    board = tio.find_ticket(slug)
    if board is None or board["status"] != "review":
        raise ReviewEntryError("ticket is no longer in review")
    if str(log_dir.resolve()) != str(Path(os.environ["BOOLEY_LOGS_DIR"]).resolve()):
        raise ReviewEntryError("interactive evidence belongs to a different ticket")
    if str(log_dir / ".runtime" / "booley_state.json") != os.environ.get("BOOLEY_STATE_FILE"):
        raise ReviewEntryError("interactive state path does not belong to this ticket")
    if (
        work_dir is not None
        and Path(work_dir).resolve() != Path(os.environ["BOOLEY_WORKTREE"]).resolve()
    ):
        raise ReviewEntryError(
            "interactive endpoint work directory differs from the ticket worktree"
        )
    basis = tio.load_basis(slug, runtime_ticket_path=os.environ["BOOLEY_TICKET_FILE"])
    if basis.basis_id != operation["basis_id"]:
        raise ReviewEntryError("interactive Acceptance Basis changed")
    if read_acceptance(log_dir).kind != "unavailable":
        raise ReviewEntryError("interactive evidence cannot modify accepted work")
