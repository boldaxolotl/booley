"""One worktree's Jobs, resources and admission as plain JSON values.

Textual-free and HTTP-free: ``web`` routes requests and calls ``read_snapshot``.
Sizes are bytes and durations are seconds; timestamps are the stored RFC 3339 strings.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from time import monotonic
from time import time as wall_clock
from typing import Any
from urllib.parse import urlencode

from booley.harness.dashboard.jobs import (
    FINISHED_JOB_STATES,
    JobReader,
    JobView,
    job_elapsed_seconds,
)
from booley.harness.dashboard.resources import Resources, ResourceSampler
from booley.harness.dashboard.sessions import session_key_label
from booley.presentation.report_results import present_job_report
from booley.runtime import job_records, job_slots
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.handles import job_prefix
from booley.runtime.memory_budget import compute_memory_budget
from booley.runtime.pid import DEAD_PROCESS_STATES, observe_process
from booley.runtime.timefmt import parse_timestamp

# The newest finished Jobs listed under Results.
MAX_RESULTS = 50
_MIB = 2**20
_PASSED = frozenset({"pass", "warn"})
_FAILED = frozenset({"fail", "attention"})


def read_snapshot(
    reader: JobReader,
    sampler: ResourceSampler,
    *,
    slots: Path | None,
    proc_root: Path,
    worktree: Path,
    branch: str | None,
) -> dict[str, Any]:
    """Read every retained Job once and project the part that belongs to *worktree*."""
    now = wall_clock()
    jobs = _read_jobs(reader, worktree, now)
    resources = sampler.sample(worktree, now=monotonic())
    admission = _admission(jobs, slots, proc_root)
    mine = _worktree_jobs(worktree, jobs)
    return {
        "header": _header(worktree, branch, resources, admission),
        "now": _now_rows(mine, now),
        "results": _results(mine, worktree, now),
    }


def find_report(reader: JobReader, worktree: Path, report: str) -> Path | None:
    """The report file a Job of *worktree* links to as *report*; ``None`` for any other path."""
    jobs = _read_jobs(reader, worktree, wall_clock())
    return next(
        (
            job.report_path
            for job in _worktree_jobs(worktree, jobs)
            if job.report_path is not None and str(job.report_path) == report
        ),
        None,
    )


def _read_jobs(reader: JobReader, worktree: Path, now: float) -> tuple[JobView, ...]:
    # A checkout outside the Main Booley Directory keeps its Standalone Jobs' reports in
    # its own Booley Directory.
    try:
        extra = (resolve_checkout_booley_dir(worktree).resolve(),)
    except (OSError, ValueError, RuntimeError):
        extra = ()
    return reader.read_on_disk(now=now, artifact_roots=extra).jobs


def _worktree_jobs(worktree: Path, jobs: Sequence[JobView]) -> list[JobView]:
    resolved: dict[str, Path | None] = {}
    mine: list[JobView] = []
    for job in jobs:
        work_dir = job.record.work_dir
        if not work_dir:
            continue
        if work_dir not in resolved:
            try:
                resolved[work_dir] = Path(work_dir).resolve()
            except (OSError, ValueError, RuntimeError):
                resolved[work_dir] = None
        if resolved[work_dir] == worktree:
            mine.append(job)
    return mine


def _header(
    worktree: Path, branch: str | None, resources: Resources, admission: dict[str, Any]
) -> dict[str, Any]:
    return {
        "worktree": str(worktree),
        "branch": branch,
        "resources": {
            "cpu_percent": resources.cpu_percent,
            "memory_bytes": resources.memory,
            "memory_limit_bytes": resources.memory_limit,
            "disk_free_bytes": resources.disk_free,
        },
        "admission": admission,
    }


def _admission(jobs: Sequence[JobView], slots: Path | None, proc_root: Path) -> dict[str, Any]:
    """Sandbox-wide Job admission: the Memory Budget, memory claims and Job counts.

    Every Job record is one Job, so a Job holding several memory claims counts once.
    """
    try:
        budget: int | None = compute_memory_budget(proc_root=proc_root).bytes
        unavailable = None
    except (OSError, ValueError) as exc:
        budget, unavailable = None, str(exc)
    reserved, alone = _memory_claims(slots, proc_root)
    return {
        "memory_budget_bytes": budget,
        "memory_budget_unavailable": unavailable,
        "reserved_bytes": reserved,
        "running_alone": alone,
        "running_jobs": sum(job.state == "running" for job in jobs),
        "queued_jobs": sum(job.state == "queued" for job in jobs),
    }


def _memory_claims(slots: Path | None, proc_root: Path) -> tuple[int, bool]:
    """Bytes reserved by live memory holders, and whether one runs alone.

    Reads without reaping, so a holder whose owner is gone but not yet reaped is skipped.
    """
    holders = (
        [] if slots is None else job_slots.SlotStore(slots).observe(job_slots.CLASS_MEMORY)[0]
    )
    claims = [
        token.memory
        for token in holders
        if token.memory is not None
        and (
            token.owner_identity is None
            or observe_process(token.owner_identity, proc_root=proc_root).state
            not in DEAD_PROCESS_STATES
        )
    ]
    reserved = sum(claim.reservation_mb or 0.0 for claim in claims)
    return round(reserved * _MIB), any(claim.run_alone for claim in claims)


def _label(job: JobView) -> str:
    return job.handle or job_prefix(job.record.endpoint) + "?"


def _owner(job: JobView) -> str:
    """``you`` for a Standalone Job (no owning session), else the session's handle or key."""
    key = job.record.session_key
    return "you" if not key else session_key_label(job.owner_handle or key)


def _started(job: JobView) -> float:
    return parse_timestamp(job.record.started_at).timestamp()


def _finished_at(job: JobView) -> float:
    return parse_timestamp(job.record.ended_at or job.record.started_at).timestamp()


def _now_rows(jobs: Sequence[JobView], now: float) -> list[dict[str, Any]]:
    unfinished = sorted(
        (job for job in jobs if job.state not in FINISHED_JOB_STATES), key=_started, reverse=True
    )
    return [
        {
            "handle": _label(job),
            "flow": job.record.endpoint,
            "target": job_records.target_arg(job.record.argv),
            "owner": _owner(job),
            "state": job.state,
            "stage": job.stage,
            "elapsed_s": job_elapsed_seconds(job, now),
            "queue_position": job.queue_position,
        }
        for job in unfinished
    ]


def _results(jobs: Sequence[JobView], worktree: Path, now: float) -> dict[str, Any]:
    """The newest finished Jobs, with pass and fail counts over the listed ones."""
    finished = sorted(
        (job for job in jobs if job.state in FINISHED_JOB_STATES), key=_finished_at, reverse=True
    )[:MAX_RESULTS]
    rows: list[dict[str, Any]] = []
    passed = failed = 0
    for job in finished:
        presentation = present_job_report(
            job.report or {},
            endpoint=job.record.endpoint,
            exit_code=job.record.exit_code,
            argv=job.record.argv,
        )
        semantic = presentation.flow_word.semantic
        passed += semantic in _PASSED
        failed += semantic in _FAILED
        row = {
            "handle": _label(job),
            "flow": job.record.endpoint,
            "target": job_records.target_arg(job.record.argv),
            "owner": _owner(job),
            "result": presentation.flow_word.value,
            "ended_at": job.record.ended_at,
            "duration_s": job_elapsed_seconds(job, now),
            "report_url": report_url(worktree, job),
        }
        if presentation.criterion is not None:
            row["criterion"] = {
                "text": presentation.criterion.value,
                "met": presentation.criterion.semantic == "met",
            }
        rows.append(row)
    return {"jobs": rows, "passed": passed, "failed": failed}


def report_url(worktree: Path, job: JobView) -> str | None:
    """Where the Job's report is served, or ``None`` when it has none."""
    if job.report_path is None:
        return None
    query = urlencode({"worktree": str(worktree), "report": str(job.report_path)})
    return f"/api/report?{query}"
