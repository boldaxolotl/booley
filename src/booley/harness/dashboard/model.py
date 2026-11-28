"""Who is in a worktree, for the web Run form's review."""

from __future__ import annotations

from pathlib import Path
from time import time as wall_clock

from booley.harness.dashboard.jobs import FINISHED_JOB_STATES, JobReader, JobSnapshot
from booley.harness.dashboard.review import Occupants


def worktree_occupants(snapshot: JobSnapshot, worktree: Path) -> Occupants:
    """The unfinished Jobs in *worktree*, as *snapshot* showed them."""
    here = worktree.resolve()
    # Oldest first: the Job records are listed in the file system's order, which varies by host.
    unfinished = tuple(
        job.handle or job.record.run_id
        for job in sorted(
            snapshot.jobs, key=lambda row: (row.record.started_at, row.record.run_id)
        )
        if job.state not in FINISHED_JOB_STATES
        and job.checkout
        and Path(job.checkout).resolve() == here
    )
    return Occupants(unfinished)


def read_occupants(jobs: JobReader, worktree: Path) -> Occupants:
    """One read of the Jobs, then who is in *worktree*."""
    return worktree_occupants(jobs.read(now=wall_clock()), worktree)
