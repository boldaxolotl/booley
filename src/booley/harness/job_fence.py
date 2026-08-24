"""Wait for detached ticket MCP endpoints before final-state consumers run."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from booley.runtime import job_records as jobrec
from booley.runtime.pid import is_pid_alive
from booley.ticket_board.paths import ticket_runtime_dir


class TicketJobFenceTimeoutError(RuntimeError):
    """Outstanding ticket jobs did not finish within their recorded budgets."""


def active_ticket_jobs(log_dir: Path) -> list[jobrec.JobRecord]:
    """Return live detached jobs belonging to one ticket log directory."""
    root = ticket_runtime_dir(log_dir) / "jobs"
    return [rec for rec in jobrec.list_records(root) if jobrec.is_active(rec, is_pid_alive)]


async def wait_for_ticket_jobs(
    log_dir: Path,
    *,
    poll_interval: float = 1.0,
    max_wait_seconds: float | None = None,
) -> list[jobrec.JobRecord]:
    """Wait until ticket jobs are terminal; return the jobs that required waiting."""
    initial = active_ticket_jobs(log_dir)
    if not initial:
        return []
    budget = max_wait_seconds
    if budget is None:
        budget = max(float(rec.timeout_s) for rec in initial) + jobrec.DEADLINE_SLACK_SECONDS
    deadline = time.monotonic() + max(0.0, budget)
    active = initial
    while active and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(poll_interval, max(0.0, remaining)))
        active = active_ticket_jobs(log_dir)
    if active:
        names = ", ".join(f"{rec.endpoint} ({rec.run_id})" for rec in active)
        raise TicketJobFenceTimeoutError(f"ticket jobs remained active: {names}")
    return initial
