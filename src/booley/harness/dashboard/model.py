"""Who is in a worktree, for the web Run form's review."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import time as wall_clock

from booley.harness.dashboard.jobs import FINISHED_JOB_STATES, JobReader, JobSnapshot
from booley.harness.dashboard.review import Occupants
from booley.harness.dashboard.sessions import SESSION_MODE
from booley.mcp.session_registry import SessionRegistry, SessionRow, namespace
from booley.runtime.handles import HandleStore


@dataclass(frozen=True)
class DashboardSnapshot:
    """The sessions and Jobs of one read, detached from every acquired lock."""

    sessions: tuple[SessionRow, ...] = ()
    jobs: JobSnapshot = field(default_factory=lambda: JobSnapshot(()))
    session_handles: Mapping[str, str] = field(default_factory=dict)
    #: Display title per session key (``S3 · uart-fix``); read with the handles in one map read.
    session_titles: Mapping[str, str] = field(default_factory=dict)


HANDLE_NUMBER = re.compile(r"S(\d+)")


def session_handle(snapshot: DashboardSnapshot, key: str) -> str:
    """The session's handle, ``S?`` until one is reserved."""
    return snapshot.session_handles.get(key, "S?")


def session_title(snapshot: DashboardSnapshot, key: str) -> str:
    """The handle with its ``--name`` label when the session has one."""
    return snapshot.session_titles.get(key) or session_handle(snapshot, key)


def session_checkouts(snapshot: DashboardSnapshot) -> list[tuple[Path, str]]:
    """Each connected session's checkout and ``S3 · uart-fix Interactive`` label, by handle number.

    The label starts with the session title (the bare handle when unnamed); the order
    follows the handle number, never the title.
    """

    def number(row: SessionRow) -> int:
        match = HANDLE_NUMBER.fullmatch(session_handle(snapshot, row.attribution.key))
        return int(match[1]) if match else 1 << 30

    pairs: list[tuple[Path, str]] = []
    for row in sorted(snapshot.sessions, key=number):
        facts = row.attribution
        checkout = facts.work_dir or facts.default_work_dir
        if checkout:
            pairs.append((Path(checkout), f"{session_title(snapshot, facts.key)} {SESSION_MODE}"))
    return pairs


def worktree_occupants(snapshot: DashboardSnapshot, worktree: Path) -> Occupants:
    """The connected sessions and unfinished Jobs in *worktree*, as *snapshot* showed them."""
    here = worktree.resolve()
    sessions = tuple(
        label for path, label in session_checkouts(snapshot) if path.resolve() == here
    )
    # Oldest first: the Job records are listed in the file system's order, which varies by host.
    unfinished = tuple(
        job.handle or job.record.run_id
        for job in sorted(
            snapshot.jobs.jobs, key=lambda row: (row.record.started_at, row.record.run_id)
        )
        if job.state not in FINISHED_JOB_STATES
        and job.record.work_dir
        and Path(job.record.work_dir).resolve() == here
    )
    return Occupants(sessions, unfinished)


def read_occupants(booley_dir: Path, jobs: JobReader, worktree: Path) -> Occupants:
    """One read of the sessions and Jobs, then who is in *worktree*."""
    now = wall_clock()
    sessions = SessionRegistry(booley_dir).visible_snapshot(now=now, scope=namespace())
    handles, titles, _diagnostics = HandleStore(booley_dir).titled_snapshot()
    snapshot = DashboardSnapshot(
        sessions.rows,
        jobs.read(session_handles=handles, now=now),
        session_handles=handles,
        session_titles=titles,
    )
    return worktree_occupants(snapshot, worktree)
