"""Dashboard Job history and Job handles."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from booley.core.booley_layout import JOB_LOGS_DIR_NAME
from booley.core.boundary import as_float
from booley.harness.standalone_job import standalone_jobs_dir
from booley.runtime.handles import HandleStore, job_handle_expired
from booley.runtime.job_observation import JobObservation, JobObserver
from booley.runtime.timefmt import parse_timestamp

MAX_INTERACTIVE_ROOTS = 64
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})
# An orphaned process is known dead even though its Job record never finalized.
FINISHED_JOB_STATES = TERMINAL_JOB_STATES | {"orphaned"}


@dataclass(frozen=True)
class JobView(JobObservation):
    """A neutral observation enriched for Dashboard display."""

    handle: str = ""


@dataclass(frozen=True)
class JobSnapshot:
    """Prepared Dashboard rows with isolated observation diagnostics."""

    jobs: tuple[JobView, ...]
    diagnostics: tuple[str, ...] = ()


class RetainedRoots(NamedTuple):
    """Job roots split by what is read from them."""

    # Finished-Job history is read from these: the caller's root, then the newest sessions.
    history: tuple[Path, ...]
    # Every other session root, newest first: only its unfinished Jobs are read.
    older: tuple[Path, ...]


def retained_job_roots(booley_dir: Path, *, interactive_root: Path | None = None) -> RetainedRoots:
    """Interactive roots: the caller's own root and the newest sessions, then the rest.

    Ordered by what a bounded scan must not lose. Every session root is statted once.
    """
    roots = [] if interactive_root is None else [interactive_root]
    sessions = []
    for path in booley_dir.glob(f"{JOB_LOGS_DIR_NAME}/*/.runtime/jobs"):
        try:
            sessions.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    newest_first = [path for _, path in sorted(sessions, key=lambda item: item[0], reverse=True)]
    history = dict.fromkeys((*roots, *newest_first[:MAX_INTERACTIVE_ROOTS]))
    older = (path for path in newest_first[MAX_INTERACTIVE_ROOTS:] if path not in history)
    return RetainedRoots(tuple(history), tuple(older))


class JobReader:
    """Compose retained history and display policy over one persistent observer."""

    def __init__(
        self,
        booley_dir: Path,
        *,
        slots_root: Path | None = None,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self._booley_dir = booley_dir
        self._observer = JobObserver(
            booley_dir,
            slots_root=slots_root,
            proc_root=proc_root,
        )

    def read(
        self,
        *,
        interactive_root: Path | None = None,
        now: float | None = None,
        artifact_roots: Sequence[Path] = (),
    ) -> JobSnapshot:
        """Read the retained Job history and every unfinished Job into one display snapshot.

        Reports are also found under *artifact_roots* (a checkout's own Booley
        Directory outside the Main Booley Directory).
        """
        roots = retained_job_roots(self._booley_dir, interactive_root=interactive_root)
        observed = self._observer.read(
            roots.history, unfinished_roots=roots.older, artifact_roots=artifact_roots
        )
        handles, diagnostics = HandleStore(self._booley_dir).snapshot()
        when = time.time() if now is None else now
        standalone = standalone_jobs_dir(self._booley_dir)
        rows = tuple(_job_view(job, handles, when, standalone) for job in observed.jobs)
        return JobSnapshot(rows, (*observed.diagnostics, *diagnostics))


def _job_view(
    job: JobObservation,
    handles: dict[str, str],
    now: float,
    standalone: Path,
) -> JobView:
    record = job.record
    state = job.state
    if job.root == standalone:
        # A Standalone Job has no supervisor: its dead process is a failed run.
        state = "failed" if state == "orphaned" else state
    handle = handles.get(record.run_id, "")
    if not handle and job_handle_expired(record, now):
        # A pruned Job is history, not an unallocated live one: name it by run ID
        # so it does not show the live-only "?".
        handle = record.run_id
    return JobView(
        root=job.root,
        record=job.record,
        state=state,
        report=job.report,
        report_path=job.report_path,
        stage=job.stage,
        diagnostic=job.diagnostic,
        artifacts=job.artifacts,
        queue=job.queue,
        queue_position=job.queue_position,
        handle=handle,
    )


def job_elapsed_seconds(job: JobView, now: float | None = None) -> float | None:
    """Extract an interval, freezing terminal Jobs even without an end stamp."""
    if job.state in TERMINAL_JOB_STATES and not job.record.ended_at:
        reported = as_float((job.report or {}).get("elapsed_s"))
        return reported if reported is not None and reported >= 0 else None
    try:
        start = parse_timestamp(job.record.run_started_at or job.record.started_at).timestamp()
        end = (
            parse_timestamp(job.record.ended_at).timestamp()
            if job.record.ended_at
            else (time.time() if now is None else now)
        )
        return max(0, end - start)
    except ValueError:
        return None
