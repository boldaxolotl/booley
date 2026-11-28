"""Spawn a detached Job worker that outlives the process that submitted it.

The submitter publishes a pid-less ``running`` Job record, then spawns
``python -m booley.harness.standalone_worker`` in its own session with
session routing stripped. The worker stamps its PID and writes the
terminal state; this module fails the record if the worker dies first.

Inside a ``booley session`` terminal attachment the helper records a release
token, so the attachment's cleanup leaves the worker running (ADR 0095).
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from booley.runtime import job_records
from booley.runtime.booley_dir import resolve_booley_dir
from booley.runtime.execution_records import (
    RELEASE_TOKEN_ENV,
    RUNTIME_EXECUTION_ENV,
    ExecutionId,
    execution_paths,
    record_release_token,
    verify_inherited_execution_id,
)
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


def worker_environment() -> dict[str, str]:
    """The environment a detached worker runs in: the detached one plus this source tree on the path."""
    environment = detached_environment()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (_SOURCE_ROOT, environment.get("PYTHONPATH")))
    )
    return environment


def _release_token() -> str | None:
    """Record and return a release token when inside a terminal attachment.

    An absent or malformed inherited execution ID is outside an attachment:
    nothing is recorded. A valid one that does not verify, or whose token
    cannot be recorded, raises ``OSError`` so no worker starts.
    """
    raw = os.environ.get(RUNTIME_EXECUTION_ENV)
    try:
        execution_id = ExecutionId(raw)
    except ValueError:
        return None
    try:
        booley_dir = resolve_booley_dir()
    except (RuntimeError, ValueError) as exc:
        raise OSError(f"cannot resolve the Booley directory for execution {execution_id}") from exc
    owner = verify_inherited_execution_id(
        execution_id, booley_dir=booley_dir, owner_pid=os.getpid()
    )
    if owner is None:
        raise OSError(
            f"cannot record a release token with terminal attachment execution {execution_id}"
        )
    token = secrets.token_hex(16)
    record_release_token(execution_paths(execution_id, booley_dir=booley_dir), token)
    return token


def worker_command(*args: str) -> list[str]:
    """The worker argv; recorded in the Job record and used to spawn it."""
    return [sys.executable, "-m", WORKER_MODULE, *args]


def spawn(command: list[str], log_path: Path, *, jobs_root: Path, run_id: str) -> None:
    """Start *command* detached in its own session, output appended to *log_path*.

    The caller already published the pid-less ``running`` record *run_id* under
    *jobs_root*. If the worker exits before stamping its PID, a daemon thread
    marks the record ``failed``.
    If this process dies first too, the record stays pid-less.
    """
    environment = worker_environment()
    token = _release_token()
    if token is not None:
        environment[RELEASE_TOKEN_ENV] = token
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
        args=(process, log_path, jobs_root, run_id),
        daemon=True,
        name="detached-job-reaper",
    ).start()


def _reap_unstarted(
    process: subprocess.Popen[Any], log_path: Path, jobs_root: Path, run_id: str
) -> None:
    """Fail a still pid-less record when its worker exits; errors are logged, never raised."""
    code = process.wait()
    try:
        job = job_records.read_record(run_id, jobs_root)
        if job is None or job.pid is not None or job.status != job_records.STATUS_RUNNING:
            return
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
