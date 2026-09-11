"""Wait for detached endpoint jobs rooted at an explicitly resolved directory."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from booley.runtime import job_records as jobrec
from booley.runtime.pid import is_pid_alive


class JobWaitTimeoutError(RuntimeError):
    """Outstanding jobs did not finish within their recorded budgets."""


def active_jobs(jobs_root: Path) -> list[jobrec.JobRecord]:
    """Return live detached jobs below one caller-resolved records root."""
    return [rec for rec in jobrec.list_records(jobs_root) if jobrec.is_active(rec, is_pid_alive)]


async def wait_for_jobs(
    jobs_root: Path,
    *,
    poll_interval: float = 1.0,
    max_wait_seconds: float | None = None,
) -> list[jobrec.JobRecord]:
    """Wait until jobs are terminal; return the jobs that required waiting."""
    initial = active_jobs(jobs_root)
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
        active = active_jobs(jobs_root)
    if active:
        names = ", ".join(f"{rec.endpoint} ({rec.run_id})" for rec in active)
        raise JobWaitTimeoutError(f"jobs remained active: {names}")
    return initial
