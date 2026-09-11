"""Ticket-owned adaptation of generic detached-job mechanics."""

from __future__ import annotations

from pathlib import Path

from booley.runtime import job_records as jobrec
from booley.runtime.job_wait import active_jobs, wait_for_jobs

from .paths import ticket_runtime_dir


def jobs_root(log_dir: Path) -> Path:
    """Resolve the detached-job records owned by one Ticket log directory."""
    return ticket_runtime_dir(log_dir) / "jobs"


def active_ticket_jobs(log_dir: Path, *, lease_id: str | None = None) -> list[jobrec.JobRecord]:
    """Return active detached jobs belonging to one Ticket."""
    records = active_jobs(jobs_root(log_dir))
    if lease_id is None:
        return records
    return [record for record in records if record.lease_id == lease_id]


async def wait_for_ticket_jobs(
    log_dir: Path,
    *,
    poll_interval: float = 1.0,
    max_wait_seconds: float | None = None,
) -> list[jobrec.JobRecord]:
    """Wait for one Ticket's detached jobs using generic Runtime mechanics."""
    return await wait_for_jobs(
        jobs_root(log_dir),
        poll_interval=poll_interval,
        max_wait_seconds=max_wait_seconds,
    )
