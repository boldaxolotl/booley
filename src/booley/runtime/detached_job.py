"""Spawn a detached Job worker that outlives the process that submitted it.

The submitter publishes a pid-less ``running`` Job record, then spawns
``python -m booley.harness.standalone_worker`` in its own session with the Goal
binding and session routing stripped. The worker stamps its PID and writes the
terminal state; this module fails the record if the worker dies first.

Known limit: a new session does not escape a ``booley session`` terminal
attachment, whose execution supervisor ends every descendant when the
attachment's command ends. A worker spawned inside one ends with it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from booley.runtime import job_records
from booley.runtime.process_group import new_group_kwargs
from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)

# A string, not an import: Runtime must not import the Harness (D6), and both
# callers record the exact argv they spawn, which ``derive_status`` matches
# against ``/proc``.
WORKER_MODULE = "booley.harness.standalone_worker"
_SOURCE_ROOT = str(Path(__file__).resolve().parents[2])


def detached_environment() -> dict[str, str]:
    """Keep EDA provisioning but remove candidate evidence and execution routing."""
    excluded = {
        "BOOLEY_GOAL_FILE",
        "BOOLEY_GOAL_RUN_BINDING",
        "BOOLEY_STATE_FILE",
        "BOOLEY_SLUG",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_RUNTIME_DIR",
        "BOOLEY_RUN_ID",
        "BOOLEY_WORKTREE",
        "BOOLEY_EXECUTION_ID",
        # Would link the worker's slot claim to the launching session's
        # execution and get it reaped when that session ends, mid-run.
        "BOOLEY_RUNTIME_EXECUTION_ID",
        "BOOLEY_TICKET_FILE",
        "BOOLEY_DIR",
        "BOOLEY_CONTROL_PROJECT_ROOT",
    }
    return {key: value for key, value in os.environ.items() if key not in excluded}


def worker_command(*args: str) -> list[str]:
    """The worker argv; recorded in the Job record and used to spawn it."""
    return [sys.executable, "-m", WORKER_MODULE, *args]


def spawn(
    command: list[str],
    log_path: Path,
    *,
    jobs_root: Path,
    run_id: str,
    on_unstarted: Callable[[int], None] | None = None,
) -> None:
    """Start *command* detached in its own session, output appended to *log_path*.

    The caller already published the pid-less ``running`` record *run_id* under
    *jobs_root*. If the worker exits before stamping its PID, a daemon thread
    calls *on_unstarted* with the exit code, then marks the record ``failed``.
    If this process dies first too, the record stays pid-less.
    """
    environment = detached_environment()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (_SOURCE_ROOT, environment.get("PYTHONPATH")))
    )
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=environment,
            **new_group_kwargs(),
        )
    # The worker stamps itself; the parent must never overwrite a terminal record.
    threading.Thread(
        target=_reap_unstarted,
        args=(process, log_path, jobs_root, run_id, on_unstarted),
        daemon=True,
        name="detached-job-reaper",
    ).start()


def _reap_unstarted(
    process: subprocess.Popen[Any],
    log_path: Path,
    jobs_root: Path,
    run_id: str,
    on_unstarted: Callable[[int], None] | None,
) -> None:
    """Fail a still pid-less record when its worker exits; errors are logged, never raised.

    *on_unstarted* runs first, so a caller that publishes its own final state (the
    Goal baseline request) keeps the order its readers rely on.
    """
    code = process.wait()
    try:
        job = job_records.read_record(run_id, jobs_root)
        if job is None or job.pid is not None or job.status != job_records.STATUS_RUNNING:
            return
        if on_unstarted is not None:
            on_unstarted(code)
        with log_path.open("ab") as log:
            log.write(f"worker exited before initialization (exit {code})\n".encode())
        job.status, job.exit_code, job.ended_at = (
            job_records.STATUS_FAILED,
            code,
            utc_now_rfc3339(),
        )
        job_records.write_record(job, jobs_root)
    except (OSError, ValueError, KeyError):
        logger.warning("Job %s startup result could not be published at %s", run_id, jobs_root)
