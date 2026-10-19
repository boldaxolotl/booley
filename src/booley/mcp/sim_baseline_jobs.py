"""Compose report-only simulation baselines with detached, durable Jobs.

Jobs live outside Goal Record roots. Only active-call adapters schedule them;
Finish, abandonment, recovery and Dashboard reads never spawn baseline work.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from booley.core.file_lock import nonblocking_file_lock
from booley.goals.model import GoalRecord, GoalState
from booley.goals.protected_inputs import ProtectedInputRoots
from booley.goals.sim_baseline_store import (
    SCHEMA,
    baseline_jobs_dir,
    entry_inputs,
    read_document,
    request_identity,
    request_path,
    sim_goals,
    write_document,
)
from booley.runtime import job_records
from booley.runtime.handles import register_job
from booley.runtime.pid import is_pid_alive
from booley.runtime.process_group import new_group_kwargs
from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)
BASELINE_TIMEOUT_S = 3900
Launch = Callable[[Path, dict[str, Any]], None]


def baseline_environment() -> dict[str, str]:
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
        "BOOLEY_TICKET_FILE",
        "BOOLEY_DIR",
        "BOOLEY_CONTROL_PROJECT_ROOT",
    }
    return {key: value for key, value in os.environ.items() if key not in excluded}


def launch_baseline(path: Path, request: dict[str, Any]) -> None:
    """Spawn the supervisor; it owns PID stamping, watchdog and terminal publication."""
    command = [sys.executable, "-m", "booley.mcp.sim_baseline_jobs", str(path)]
    rec = job_records.JobRecord(
        request["job_id"],
        "sim_baseline",
        utc_now_rfc3339(),
        BASELINE_TIMEOUT_S,
        argv=command,
        work_dir=request["record"]["worktree_path"],
        session_key=request["record"]["session_key"],
    )
    jobs = Path(request["jobs_root"])
    register_job(
        Path(request["booley_dir"]),
        rec.run_id,
        rec.endpoint,
        rec.session_key,
        work_dir=rec.work_dir,
    )
    job_records.write_record(rec, jobs)
    environment = baseline_environment()
    source = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (source, environment.get("PYTHONPATH")))
    )
    with (path.parent / "job.log").open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=environment,
            **new_group_kwargs(),
        )
    # The supervisor stamps itself; the parent must never overwrite a terminal record.
    threading.Thread(
        target=_reap_unstarted, args=(process, path), daemon=True, name="sim-baseline-reaper"
    ).start()


def _reap_unstarted(process: subprocess.Popen[Any], path: Path) -> None:
    """Publish startup errors immediately after the child exits, without PID write races."""
    code = process.wait()
    try:
        request = read_document(path)
        jobs = Path(request["jobs_root"])
        job = job_records.read_record(request["job_id"], jobs)
        if job is not None and job.pid is None and job.status == job_records.STATUS_RUNNING:
            job.status, job.exit_code, job.ended_at = (
                job_records.STATUS_FAILED,
                code,
                utc_now_rfc3339(),
            )
            job_records.write_record(job, jobs)
            write_document(
                path,
                request
                | {
                    "status": "unavailable",
                    "reason": f"baseline worker exited before initialization (exit {code}); see job.log",
                },
            )
    except (OSError, ValueError, KeyError):
        logger.warning("Baseline Job startup result could not be published at %s", path)


def schedule_sim_baselines(
    project: Path, record: GoalRecord, *, launch: Launch | None = None
) -> None:
    """Idempotent active-call scheduling; failure never changes lifecycle success.

    *launch* defaults to :func:`launch_baseline`, looked up at call time.
    """
    if record.state is not GoalState.ACTIVE:
        return
    launcher = launch_baseline if launch is None else launch
    try:
        inputs = entry_inputs(project, record)
        for goal in sim_goals(record):
            identity = request_identity(record, goal, inputs)
            path = request_path(project, identity)
            path.parent.mkdir(parents=True, exist_ok=True)
            with (
                (path.parent / "launch.lock").open("a+b") as handle,
                nonblocking_file_lock(handle),
            ):
                if path.exists():
                    _reconcile_request(path)
                    continue
                request = {
                    "schema": SCHEMA,
                    "identity": identity,
                    "inputs": inputs,
                    "booley_dir": str(project),
                    "record": record.to_json(),
                    "source_repository": str(
                        ProtectedInputRoots(Path(record.worktree_path), project).main_checkout()
                        or Path(record.worktree_path)
                    ),
                    "status": "pending",
                    "results": [],
                    "job_id": f"sim-baseline-{path.parent.name}",
                    "jobs_root": str(baseline_jobs_dir(project)),
                }
                write_document(path, request)
                try:
                    launcher(path, request)
                except (OSError, ValueError, RuntimeError) as exc:
                    write_document(
                        path,
                        request
                        | {
                            "status": "unavailable",
                            "reason": f"baseline Job launch failed: {exc}",
                        },
                    )
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Simulation baseline scheduling unavailable for %s: %s", record.id, exc)


def _reconcile_request(path: Path) -> None:
    """An interrupted Job is unavailable; reap only its proven, idle scratch checkout."""
    request = read_document(path)
    job = job_records.read_record(request["job_id"], Path(request["jobs_root"]))
    if job is not None and job_records.is_active(job, is_pid_alive):
        return
    if request["status"] == "pending":
        request = request | {
            "status": "unavailable",
            "reason": "baseline Job interrupted or unavailable",
        }
        write_document(path, request)
    if job is not None and job.pid is not None and not is_pid_alive(job.pid):
        from booley.mcp.sim_baseline_execution import reap_owned_checkout

        reap_owned_checkout(request, job.pid)


def _cancel(_signal: int, _frame: object) -> None:
    """Unwind owned checkout/child contexts on a graceful Job cancellation."""
    raise InterruptedError("baseline Job cancelled")


def main(argv: list[str] | None = None) -> int:
    """Supervise one immutable request independently of the MCP server lifetime."""
    from booley.mcp.sim_baseline_execution import run_baseline

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 2
    path = Path(args[0])
    request = read_document(path)
    jobs = Path(request["jobs_root"])
    job = job_records.read_record(request["job_id"], jobs)
    if job is None:
        return 2
    job.pid = os.getpid()
    job_records.write_record(job, jobs)
    signal.signal(signal.SIGTERM, _cancel)
    try:
        results = run_baseline(path, request)
        write_document(path, request | {"status": "complete", "results": results})
        job.status, job.exit_code = job_records.STATUS_DONE, 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        write_document(
            path, request | {"status": "unavailable", "reason": f"baseline unavailable: {exc}"}
        )
        job.status, job.exit_code = job_records.STATUS_FAILED, 2
    finally:
        job.ended_at = utc_now_rfc3339()
        job_records.write_record(job, jobs)
    return job.exit_code or 0


if __name__ == "__main__":
    raise SystemExit(main())
