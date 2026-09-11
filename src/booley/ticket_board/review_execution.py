"""Run existing endpoints in an explicit, isolated Ticket review context."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from booley.criteria.state import DevelopmentState
from booley.runtime.execution_lease import (
    ExecutionLeaseAbsentPath,
    ExecutionLeaseEnvironment,
    ExecutionLeaseFile,
)
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.timefmt import rfc3339_from_epoch
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketIO
from booley.ticket_board.ticket_jobs import active_ticket_jobs, jobs_root

from .review_lifecycle import _quiescent, _write
from .review_records import ReviewEntryError, assert_idle, operation_path, read_entry

if TYPE_CHECKING:
    from .review_preparation import ReviewPrepContext

_COMMAND_TIMEOUT_SECONDS = 7200
_LEASE_EXPIRY_SLACK_SECONDS = 120


def _build_execution_lease(
    ctx: ReviewPrepContext,
    *,
    lease_id: str,
    basis_id: str,
    state_path: Path,
    runtime_ticket: Path,
    review_ticket: Path,
) -> ExecutionLeaseEnvironment:
    issued_epoch = time.time()
    return ExecutionLeaseEnvironment(
        schema=1,
        lease_id=lease_id,
        lease_file=operation_path(ctx.log_dir),
        phase="interactive",
        issued_at=rfc3339_from_epoch(issued_epoch),
        expires_at=rfc3339_from_epoch(
            issued_epoch + _COMMAND_TIMEOUT_SECONDS + _LEASE_EXPIRY_SLACK_SECONDS
        ),
        basis_id=basis_id,
        state_file=state_path,
        work_dir=ctx.worktree,
        log_dir=ctx.log_dir,
        runtime_dir=state_path.parent,
        ticket_file=runtime_ticket,
        jobs_root=jobs_root(ctx.log_dir),
        required_files=(
            ExecutionLeaseFile.capture("Ticket Board review Ticket", review_ticket),
            ExecutionLeaseFile.capture("Acceptance Basis runtime Ticket", runtime_ticket),
        ),
        absent_paths=(
            ExecutionLeaseAbsentPath(
                "accepted Criteria Satisfaction Record",
                ctx.log_dir / "acceptance" / "accepted.json",
            ),
        ),
    )


def _child_environment(
    tio: TicketIO,
    ctx: ReviewPrepContext,
    state: DevelopmentState,
    ticket: Path,
    lease: ExecutionLeaseEnvironment,
) -> dict[str, str]:
    return {
        "TICKETS_DIR": str(tio.tickets_dir),
        "BOOLEY_SLUG": ctx.slug,
        "BOOLEY_WORKTREE": str(ctx.worktree),
        "BOOLEY_TICKET_TYPE": state.ticket_type,
        "BOOLEY_EXECUTION_ID": lease.lease_id,
        "BOOLEY_TICKET_FILE": str(ticket),
        "BOOLEY_LOGS_DIR": str(ctx.log_dir),
        "BOOLEY_RUNTIME_DIR": str(lease.runtime_dir),
        "BOOLEY_STATE_FILE": str(lease.state_file),
        "BOOLEY_CONTROL_PROJECT_ROOT": str(ctx.project_root),
        "BOOLEY_PROJECT_DIR": str(resolve_checkout_project_dir(ctx.worktree)),
        "BOOLEY_PAIRED_PROJECT_REPOSITORY": "1" if ctx.project_repository else "",
        "BOOLEY_REVIEW_OPERATION": lease.lease_id,
    }


def _environment(
    tio: TicketIO,
    slug: str,
    *,
    lease_id: str,
    basis_id: str,
    review_ticket: Path,
) -> tuple[Path, dict[str, str], ExecutionLeaseEnvironment]:
    from .review_preparation import _resolve_context

    ctx = _resolve_context(
        tio._project_root,
        slug,
        allow_report_disabled=True,
        inspect_unaccepted=True,
        locked_basis=tio._load_basis_unlocked(slug),
    )
    if ctx.acceptance_basis_id != basis_id:
        raise ReviewEntryError("review inspection Acceptance Basis is no longer current")
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
    lease = _build_execution_lease(
        ctx,
        lease_id=lease_id,
        basis_id=basis_id,
        state_path=state_path,
        runtime_ticket=ticket,
        review_ticket=review_ticket,
    )
    env = _interactive_environment()
    env.update(_child_environment(tio, ctx, state, ticket, lease))
    env.update(lease.to_environment())
    return ctx.worktree, env, lease


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
        board = tio.find_ticket(slug)
        if board is None or board["status"] != "review":
            raise ReviewEntryError("review-exec requires a review ticket")
        cwd, env, lease = _environment(
            tio,
            slug,
            lease_id=uuid.uuid4().hex,
            basis_id=entry["basis_id"],
            review_ticket=tio.tickets_dir / board["file"],
        )
        operation = lease.operation_record(owner_pid=os.getpid())
        _write(operation_path(log_dir), operation)
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        ).returncode
    finally:
        with tio._ticket_lock(slug, review_operation=True):
            if not active_ticket_jobs(log_dir):
                operation_path(log_dir).unlink(missing_ok=True)
