"""The detached Standalone Job worker: ``python -m booley.harness.standalone_worker <kind> ...``.

Kinds: ``standalone <booley_dir> <run_id>`` runs the Flow or Specialist request
:func:`submit` wrote beside the Job's ``job.log``; ``goal-baseline <request path>``
supervises a Goal baseline (:mod:`booley.mcp.sim_baseline_jobs`). The worker stamps
the submitter's pid-less record and writes the terminal state like a shell run.
A Specialist runs with its worktree's agent settings and a transcript directory
beside ``job.log``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.config.settings import set_agent_settings, worktree_agent_settings
from booley.core.boundary import BoundaryError, require_dict, require_list, require_str
from booley.harness.standalone_job import (
    RUN_ID_ENV,
    STANDALONE_TIMEOUT_S,
    StandaloneJob,
    checkout_report_root,
    standalone_jobs_dir,
    standalone_log_path,
    standalone_request_path,
    standalone_transcript_dir,
)
from booley.runtime import detached_job, job_records
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.handles import register_job
from booley.runtime.timefmt import compact_utc_now, utc_now_rfc3339

_KINDS = frozenset({"flow", "specialist"})
# The Dashboard shows only Jobs whose endpoint matches this pattern.
_ENDPOINT_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_FALLBACK_STEM = "standalone"
_PARENT_WATCHDOG_ENV = "BOOLEY_PARENT_WATCHDOG"
_START_FAILURE_EXIT_CODE = 2
_USAGE = "usage: standalone_worker (standalone <booley_dir> <run_id> | goal-baseline <path>)"


@dataclass(frozen=True)
class StandaloneRequest:
    """One Flow or Specialist run: ``booley <kind> <name> *argv`` typed in *worktree*."""

    worktree: Path
    kind: str
    name: str
    argv: tuple[str, ...] = ()


def submit(booley_dir: Path, request: StandaloneRequest) -> str:
    """Run *request* as a detached Standalone Job and return its ``run_id``.

    The caller may exit at once. A start failure (bad request, unknown Flow,
    missing worktree) leaves a ``failed`` record with the reason in ``job.log``;
    ``OSError`` is raised only when the record itself cannot be written.

    Limit: a worker submitted from inside a ``booley session`` terminal attachment
    ends with that attachment's command (see :mod:`booley.runtime.detached_job`).
    """
    # One validated stem names both run and endpoint, so a bad name still
    # yields a Job the Dashboard can list as failed.
    stem = request.name if _ENDPOINT_PATTERN.fullmatch(request.name) else _FALLBACK_STEM
    run_id = job_records.make_run_id(stem, compact_utc_now(), 1)
    log_path = standalone_log_path(booley_dir, run_id)
    document = {
        "worktree": str(request.worktree),
        "kind": request.kind,
        "name": request.name,
        "argv": list(request.argv),
    }
    record = job_records.JobRecord(
        run_id=run_id,
        endpoint=stem,
        started_at=utc_now_rfc3339(),
        timeout_s=STANDALONE_TIMEOUT_S,
        argv=["booley", request.kind, request.name, *request.argv],
        work_dir=str(request.worktree),
        report_root=checkout_report_root(request.worktree),
    )
    atomic_replace_bytes(
        standalone_request_path(booley_dir, run_id), json.dumps(document).encode("utf-8")
    )
    jobs_root = standalone_jobs_dir(booley_dir)
    job_records.write_record_strict(record, jobs_root)
    register_job(booley_dir, run_id, stem, None, work_dir=record.work_dir)
    try:
        detached_job.spawn(
            detached_job.worker_command("standalone", str(booley_dir), run_id),
            log_path,
            jobs_root=jobs_root,
            run_id=run_id,
        )
    except OSError as exc:
        # Best effort: an unwritable log must not stop the record turning failed.
        with contextlib.suppress(OSError), log_path.open("ab") as log:
            log.write(f"Standalone Job not started: {exc}\n".encode())
        record.status = job_records.STATUS_FAILED
        record.exit_code = _START_FAILURE_EXIT_CODE
        record.ended_at = utc_now_rfc3339()
        job_records.write_record(record, jobs_root)
    return run_id


def _start_request(request_path: Path, transcript_dir: Path) -> int:
    """Validate the request and run it as a human would type it in its worktree.

    No outer ``--project``: it would make the CLI refuse endpoints without the shared
    CLI that a shell run accepts. The cwd comes from the request, never the submitter.

    A Specialist also gets its worktree's agent settings (``[models]`` pins, auth
    policy; an invalid one is a start failure) and ``--transcript-dir`` appended to
    the CLI argv only, so the recorded command is unchanged. Assumes every
    ``booley specialist`` endpoint takes the shared Specialist parser's
    ``--transcript-dir``, as MCP calls already pass it to every Specialist.
    """
    try:
        document = require_dict(json.loads(request_path.read_bytes()), field="request")
        kind = require_str(document, "kind")
        if kind not in _KINDS:
            raise BoundaryError(f"kind must be one of {sorted(_KINDS)}, got {kind!r}")
        name = require_str(document, "name")
        argv = require_list(document.get("argv"), field="argv")
        if not all(isinstance(arg, str) for arg in argv):
            raise BoundaryError("argv must be a list of strings")
        worktree = Path(require_str(document, "worktree"))
        if not worktree.is_dir():
            raise BoundaryError(f"worktree is not a directory: {worktree}")
        if kind == "specialist":
            set_agent_settings(worktree_agent_settings(worktree))
            argv = [*argv, "--transcript-dir", str(transcript_dir)]
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: Standalone Job not started: {exc}", file=sys.stderr)
        return _START_FAILURE_EXIT_CODE
    from booley.harness import booley as booley_cli

    os.chdir(worktree)
    sys.argv = ["booley", kind, name, *argv]  # the CLI parses its argv from here
    return booley_cli.main()


def _run_standalone(booley_dir: Path, run_id: str) -> int:
    """Run the submitted request as the Job whose record its submitter wrote."""
    job = StandaloneJob.adopt(booley_dir, run_id)
    if job is None:
        print(f"ERROR: Standalone Job record not found: {run_id}", file=sys.stderr)
        return _START_FAILURE_EXIT_CODE
    # Its own run id (the CLI sees a nested run) and no watchdog: the submitter exits.
    os.environ[RUN_ID_ENV] = run_id
    os.environ[_PARENT_WATCHDOG_ENV] = "0"
    # fd 1 is the block-buffered ``job.log``: a live log would stay empty.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    request_path = standalone_request_path(booley_dir, run_id)
    transcript_dir = standalone_transcript_dir(booley_dir, run_id)
    return job.run(lambda: _start_request(request_path, transcript_dir))


def main(argv: list[str] | None = None) -> int:
    """Dispatch one worker kind; an unknown kind or bad argument count is a usage error."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "goal-baseline":
        from booley.mcp.sim_baseline_jobs import supervise

        return supervise(Path(args[1]))
    if len(args) == 3 and args[0] == "standalone":
        return _run_standalone(Path(args[1]), args[2])
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
