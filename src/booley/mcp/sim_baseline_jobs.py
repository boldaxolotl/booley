"""Compose report-only Goal baselines with detached, durable Jobs.

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
from booley.goals import sim_baseline_store
from booley.goals.model import GoalRecord, GoalState, RecordedGoal
from booley.goals.protected_inputs import ProtectedInputRoots
from booley.goals.sim_baseline_store import (
    SIM_BASELINE,
    BaselineFamily,
    baseline_jobs_dir,
    entry_inputs,
    read_document,
    request_identity,
    request_path,
    write_document,
)
from booley.runtime import job_records
from booley.runtime.handles import register_job
from booley.runtime.pid import capture_process_identity, is_pid_alive
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
        # Would link the supervisor's slot claim to the launching session's
        # execution and get it reaped when that session ends, mid-measurement.
        "BOOLEY_RUNTIME_EXECUTION_ID",
        "BOOLEY_TICKET_FILE",
        "BOOLEY_DIR",
        "BOOLEY_CONTROL_PROJECT_ROOT",
    }
    return {key: value for key, value in os.environ.items() if key not in excluded}


def _supervisor_command(path: Path) -> list[str]:
    """The supervisor argv; recorded in the Job record and used to spawn it."""
    return [sys.executable, "-m", "booley.mcp.sim_baseline_jobs", str(path)]


def _publish_job_record(
    path: Path, request: dict[str, Any], family: BaselineFamily = SIM_BASELINE
) -> None:
    """Register and durably write the Job record, raising ``OSError`` if it cannot land.

    Runs before the request is published as ``pending`` so a reader that sees
    ``pending`` always finds the Job it waits for.
    """
    rec = job_records.JobRecord(
        request["job_id"],
        family.endpoint,
        utc_now_rfc3339(),
        BASELINE_TIMEOUT_S,
        argv=_supervisor_command(path),
        work_dir=request["record"]["worktree_path"],
        session_key=request["record"]["session_key"],
    )
    register_job(
        Path(request["booley_dir"]),
        rec.run_id,
        rec.endpoint,
        rec.session_key,
        work_dir=rec.work_dir,
    )
    job_records.write_record_strict(rec, Path(request["jobs_root"]))


def launch_baseline(path: Path, request: dict[str, Any]) -> None:
    """Spawn the supervisor; it owns PID stamping, watchdog and terminal publication.

    The scheduler has already published the Job record (:func:`_publish_job_record`).
    """
    command = _supervisor_command(path)
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
        request = read_document(path, None)
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
    """Schedule the SIM family's baselines."""
    schedule_family_baselines(project, record, SIM_BASELINE, launch=launch)


def schedule_goal_baselines(
    project: Path, record: GoalRecord, *, launch: Launch | None = None
) -> None:
    """Schedule every registered family's baselines; the active-call adapters' entry point."""
    for family in sim_baseline_store.BASELINE_FAMILIES:
        schedule_family_baselines(project, record, family, launch=launch)


def schedule_family_baselines(
    project: Path, record: GoalRecord, family: BaselineFamily, *, launch: Launch | None = None
) -> None:
    """Idempotent active-call scheduling; failure never changes lifecycle success.

    *launch* defaults to :func:`launch_baseline`, looked up at call time. One
    Goal's failure is logged and never stops the scheduling of the others.
    """
    if record.state is not GoalState.ACTIVE:
        return
    launcher = launch_baseline if launch is None else launch
    try:
        inputs = entry_inputs(project, record)
        goals = family.goals(record)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Baseline scheduling unavailable for %s: %s", record.id, exc)
        return
    for goal in goals:
        try:
            _schedule_goal(project, record, goal, inputs, launcher, family)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Baseline scheduling unavailable for %s: %s", record.id, exc)


def _schedule_goal(
    project: Path,
    record: GoalRecord,
    goal: RecordedGoal,
    inputs: dict[str, Any],
    launcher: Launch,
    family: BaselineFamily,
) -> None:
    """Reconcile or create one Goal's request under its launch lock.

    The Job record is written before the request is published as ``pending``,
    so no reader observes ``pending`` without the Job it waits for.
    """
    identity = request_identity(project, record, goal, inputs, family)
    path = request_path(project, identity, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / "launch.lock").open("a+b") as handle, nonblocking_file_lock(handle):
        if path.exists():
            _reconcile_request(path)
            return
        request = {
            "schema": family.schema,
            "identity": identity,
            "inputs": inputs,
            "booley_dir": str(project),
            "record": record.to_json(),
            # An entry-pinned Paired Checkout exists only in the Goal worktree (#1961).
            "source_repository": str(
                Path(record.worktree_path)
                if record.paired_project_base_sha is not None
                else ProtectedInputRoots(Path(record.worktree_path), project).main_checkout()
                or Path(record.worktree_path)
            ),
            "status": "pending",
            "results": [],
            "job_id": f"{family.job_prefix}{path.parent.name}",
            "jobs_root": str(baseline_jobs_dir(project, family)),
        }
        try:
            _publish_job_record(path, request, family)
            write_document(path, request)
            launcher(path, request)
        except (OSError, ValueError, RuntimeError) as exc:
            write_document(
                path,
                request
                | {"status": "unavailable", "reason": f"baseline Job launch failed: {exc}"},
            )


def _reconcile_request(path: Path) -> None:
    """An interrupted Job is unavailable; reap only its proven, idle scratch checkout."""
    request = read_document(path, None)
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
    request = read_document(path, None)
    jobs = Path(request["jobs_root"])
    job = job_records.read_record(request["job_id"], jobs)
    if job is None:
        return 2
    job.pid = os.getpid()
    # Linux-only like the MCP Job lifecycle: status readers match the
    # supervisor's slot claim to this identity to report the Job as queued.
    identity = capture_process_identity(job.pid) if sys.platform == "linux" else None
    job.process_identity = None if identity is None else identity.to_payload()
    job_records.write_record(job, jobs)
    signal.signal(signal.SIGTERM, _cancel)
    try:
        results = run_baseline(path, request)
        if results and all(row["grade"] == "unavailable" for row in results):
            # Nothing was observed: a finished Job must not read as a complete baseline.
            reasons = "; ".join(sorted({str(row.get("reason", "")) for row in results}))
            write_document(
                path,
                request
                | {
                    "status": "unavailable",
                    "reason": f"baseline unavailable: {reasons}",
                    "results": results,
                },
            )
            job.status, job.exit_code = job_records.STATUS_FAILED, 2
        else:
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
