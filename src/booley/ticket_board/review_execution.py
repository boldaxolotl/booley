"""Run existing endpoints in an explicit, isolated Ticket review context."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from booley.criteria.state import DevelopmentState
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketIO
from booley.ticket_board.ticket_jobs import active_ticket_jobs

from .review_lifecycle import _quiescent, _write
from .review_records import ReviewEntryError, assert_idle, operation_path, read_entry


def _environment(
    tio: TicketIO, slug: str, operation: dict[str, Any]
) -> tuple[Path, dict[str, str]]:
    from .review_preparation import _resolve_context

    ctx = _resolve_context(
        tio._project_root,
        slug,
        allow_report_disabled=True,
        inspect_unaccepted=True,
        locked_basis=tio._load_basis_unlocked(slug),
    )
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = DevelopmentState.load(state_path)
    if (
        state.slug != slug
        or not state.strict_criteria
        or Path(state.work_dir).resolve() != ctx.worktree
    ):
        raise ReviewEntryError(
            "interactive verification requires this ticket's strict runtime state"
        )
    ticket = ctx.log_dir / "ticket.md"
    if not ticket.is_file():
        raise ReviewEntryError("frozen runtime ticket is unavailable")
    env = _interactive_environment()
    env.update(
        {
            "TICKETS_DIR": str(tio.tickets_dir),
            "BOOLEY_SLUG": slug,
            "BOOLEY_WORKTREE": str(ctx.worktree),
            "BOOLEY_TICKET_TYPE": state.ticket_type,
            "BOOLEY_EXECUTION_ID": operation["token"],
            "BOOLEY_TICKET_FILE": str(ticket),
            "BOOLEY_LOGS_DIR": str(ctx.log_dir),
            "BOOLEY_RUNTIME_DIR": str(state_path.parent),
            "BOOLEY_STATE_FILE": str(state_path),
            "BOOLEY_CONTROL_PROJECT_ROOT": str(ctx.project_root),
            "BOOLEY_PROJECT_DIR": str(resolve_checkout_project_dir(ctx.worktree)),
            "BOOLEY_PAIRED_PROJECT_REPOSITORY": "1" if ctx.project_repository else "",
            "BOOLEY_REVIEW_OPERATION": operation["token"],
            "BOOLEY_EXECUTION_LEASE_ID": operation["token"],
            "BOOLEY_EXECUTION_LEASE_FILE": str(operation_path(ctx.log_dir)),
            "BOOLEY_EXECUTION_LEASE_PHASE": "interactive",
            "BOOLEY_EXECUTION_LEASE_STATE_FILE": str(state_path),
            "BOOLEY_EXECUTION_LEASE_WORK_DIR": str(ctx.worktree),
        }
    )
    return ctx.worktree, env


def _interactive_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "BOOLEY_AGENT_ROLE",
        "BOOLEY_DEVELOPER_PID",
        "BOOLEY_RUN_ID",
        "BOOLEY_NESTED_AGENT",
    ):
        env.pop(key, None)
    return env


def run_review_command(project_root: Path, slug: str, command: list[str]) -> int:
    """Run one CLI endpoint or isolated MCP server with ticket-bound evidence."""
    tio = TicketIO(tickets_dir_from_project_root(project_root), project_root=project_root)
    board = tio.find_ticket(slug)
    if board is None or board["status"] != "review":
        raise ReviewEntryError("review-exec requires a review ticket")
    slug = Path(board["file"]).stem
    log_dir = tio.logs_dir / slug
    command = command[1:] if command[:1] == ["--"] else command
    if not command:
        raise ReviewEntryError("review-exec requires a command after --")
    with tio._ticket_lock(slug, review_operation=True):
        assert_idle(log_dir)
        _quiescent(tio, slug)
        entry = read_entry(log_dir)
        if entry is None or entry["disposition"] != "unaccepted":
            raise ReviewEntryError("review-exec requires explicitly unaccepted review")
        operation = {
            "pid": os.getpid(),
            "phase": "interactive",
            "token": uuid.uuid4().hex,
            "basis_id": entry["basis_id"],
        }
        cwd, env = _environment(tio, slug, operation)
        _write(operation_path(log_dir), operation)
    try:
        return subprocess.run(command, cwd=cwd, env=env, check=False, timeout=7200).returncode
    finally:
        with tio._ticket_lock(slug, review_operation=True):
            if not active_ticket_jobs(log_dir):
                operation_path(log_dir).unlink(missing_ok=True)
