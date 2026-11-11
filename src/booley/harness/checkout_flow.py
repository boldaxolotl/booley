"""Run one Booley Flow for one Target in an exported checkout (``booley measure``'s runner).

The checkout comes from :func:`booley.runtime.commit_export.export_commit`: a
Git-less directory that is the Project Root of the run. The Flow runs as a child
process whose ``BOOLEY_DIR`` is the caller's Main Booley Directory, so admission
and its settings, the Resource History and the Job record stay there, while
nothing shared is written into the checkout. The child gets the scrubbed
:func:`~booley.measurement.environment.measurement_environment`, a fresh ``HOME``
and ``TMPDIR`` beside the checkout, and its reports go to a directory the caller
names outside the checkout. Once the entry point returns, no Job record names a
path inside the checkout.

Limit: the child selects the checkout with the outer ``booley --project``, which
only endpoints on the shared CLI honour (built-in Flows and ``BooleyFlow`` Project
Flows); a Project Flow that overrides ``main`` is refused by the CLI with exit 2.

The child half (:func:`child_main`) runs with the checkout as its working
directory, so the checkout path is never part of the child's exec argv.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict
from booley.flows.metric_declarations import FlowMetricValues
from booley.harness.standalone_job import (
    RUN_ID_ENV,
    STANDALONE_TIMEOUT_S,
    StandaloneJob,
    standalone_jobs_dir,
    standalone_log_path,
)
from booley.measurement.environment import measurement_environment
from booley.measurement.flow_metrics import report_metric_values
from booley.runtime import booley_dir, job_records, runtime_context
from booley.runtime.handles import register_job
from booley.runtime.process_group import (
    capture_process_group,
    force_group_termination,
    new_group_kwargs,
)
from booley.runtime.project_discovery import has_git_worktree_marker
from booley.runtime.timefmt import compact_utc_now, utc_now_rfc3339
from booley.targets.flow_names import canonical

# The Flow's tools run in sessions of their own: SIGINT lets the Flow reap them
# before this grace period ends and the process group is killed.
_STOP_GRACE_S = 30
_START_FAILURE_EXIT_CODE = 2
_FLOW_NAME = re.compile(r"[A-Za-z0-9_-]+")
# The slot claim records this process's argv, so the bootstrap stays one short line.
_BOOTSTRAP = (
    "import sys;sys.path.append(sys.argv.pop(1));"
    "from booley.harness.checkout_flow import child_main;raise SystemExit(child_main())"
)
# Where ``import booley`` resolves from: located by argv, so ``PYTHONPATH`` stays out.
_BOOLEY_SOURCE_ROOT = Path(__file__).resolve().parents[2]


class CheckoutFlowError(ValueError):
    """The Flow was refused or could not be started; no Flow ran."""


class CheckoutFlowStatus(StrEnum):
    """How the Flow ended."""

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CheckoutFlowRequest:
    """One Flow run: *flow* for *target*, Project Root *checkout*, state in *main_booley_dir*."""

    checkout: Path
    main_booley_dir: Path
    report_dir: Path  # outside the checkout; the Flow's reports land under ``flow-reports/``
    flow: str
    target: str
    timeout_s: float | None = None  # wall clock of the run, admission queueing included


@dataclass(frozen=True)
class CheckoutFlowResult:
    """What the caller needs from the run."""

    run_id: str
    status: CheckoutFlowStatus
    exit_code: int
    reports: tuple[Path, ...]  # this run's ``report.json`` files, oldest first
    metrics: Mapping[str, FlowMetricValues]  # per Target; empty when the Flow declares none
    log: Path  # the Job's ``job.log`` in the Main Booley Directory


def _refuse_unsafe(request: CheckoutFlowRequest) -> tuple[Path, Path, Path]:
    """Return the resolved checkout, Main Booley Directory and report directory.

    Raises before anything is created when the request is unsafe.
    """
    if not runtime_context.inside_session_runtime():
        raise CheckoutFlowError("a checkout Flow runs only inside the Sandbox")
    if request.timeout_s is not None and not 0 < request.timeout_s < math.inf:
        raise CheckoutFlowError(
            f"timeout must be a positive number of seconds: {request.timeout_s}"
        )
    if not _FLOW_NAME.fullmatch(request.flow):
        raise CheckoutFlowError(f"invalid Flow name: {request.flow!r}")
    if "\0" in request.target:
        raise CheckoutFlowError(f"invalid Target name: {request.target!r}")
    checkout = request.checkout.resolve()
    if not checkout.is_dir():
        raise CheckoutFlowError(f"checkout is not a directory: {checkout}")
    # Explicit Project selection walks ancestors for ``.git``: a checkout at or below
    # any Git checkout (the live working tree above all) would capture the child.
    for candidate in (checkout, *checkout.parents):
        if has_git_worktree_marker(candidate):
            raise CheckoutFlowError(f"checkout {checkout} is inside the Git checkout {candidate}")
    main = request.main_booley_dir.resolve()
    state_dirs = [main]
    if os.environ.get("BOOLEY_DIR"):
        state_dirs.append(Path(os.environ["BOOLEY_DIR"]).resolve())
    for state_dir in state_dirs:
        # Identity too: the Sandbox mounts its Booley Directory under two spellings.
        if checkout.is_relative_to(state_dir) or booley_dir.contains(
            checkout, booley_dir=state_dir
        ):
            raise CheckoutFlowError(
                f"checkout {checkout} is inside the Booley Directory {state_dir}"
            )
    # The reverse too: shared state inside the checkout would vanish with it.
    if main.is_relative_to(checkout):
        raise CheckoutFlowError(f"Booley Directory {main} is inside the checkout {checkout}")
    report_dir = request.report_dir.resolve()
    if report_dir.is_relative_to(checkout):
        raise CheckoutFlowError(f"report directory {report_dir} is inside the checkout {checkout}")
    return checkout, main, report_dir


def _launch(
    request: CheckoutFlowRequest,
    checkout: Path,
    main: Path,
    report_dir: Path,
    run_id: str,
    log_path: Path,
) -> tuple[subprocess.Popen[Any], Path]:
    """Start the child; return it with the directory holding its fresh ``HOME`` and ``TMPDIR``."""
    scratch = Path(tempfile.mkdtemp(dir=checkout.parent, prefix=f".{checkout.name}.env-"))
    try:
        home, tmp = scratch / "home", scratch / "tmp"
        home.mkdir()
        tmp.mkdir()
        environment = measurement_environment(os.environ, main_booley_dir=main, home=home, tmp=tmp)
        command = [
            sys.executable,
            "-c",
            _BOOTSTRAP,
            str(_BOOLEY_SOURCE_ROOT),
            str(main),
            run_id,
            request.flow,
            "--target",
            request.target,
            "--report-dir",
            str(report_dir / "flow-reports"),
        ]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=checkout,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **new_group_kwargs(),
            )
    except BaseException:  # includes a Ctrl-C: the directory must not outlive the run
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return process, scratch


def _stop(process: subprocess.Popen[Any]) -> None:
    """Interrupt the child so the Flow reaps its tools; kill the group if it does not end."""
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=_STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        force_group_termination(process, capture_process_group(process))
        process.wait()
    except BaseException:  # a second Ctrl-C: never leave the Flow running
        force_group_termination(process, capture_process_group(process))
        process.wait()
        raise


def _wait(process: subprocess.Popen[Any], timeout_s: float | None) -> bool:
    """Wait for the child; return whether the budget expired. A caller's Ctrl-C stops it first."""
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop(process)
        return True
    except BaseException:
        _stop(process)
        raise
    return False


def _shell_status(returncode: int) -> int:
    """A shell's exit status for a process that died by signal."""
    return returncode if returncode >= 0 else 128 - returncode


def _read_reports(
    report_dir: Path, flow: str, run_id: str
) -> list[tuple[Path, dict[str, object]]]:
    """This run's numbered reports with their documents, oldest first; unreadable ones are skipped."""
    found: list[tuple[int, Path, dict[str, object]]] = []
    for path in (report_dir / "flow-reports" / flow).glob("*/report.json"):
        try:
            document = as_dict(json.loads(path.read_bytes()))
        except (OSError, ValueError):
            continue
        if (
            document is not None
            and document.get("run_id") == run_id
            and path.parent.name.isdigit()
        ):
            found.append((int(path.parent.name), path, document))
    return [(path, document) for _, path, document in sorted(found)]


def _settle_record(
    record: job_records.JobRecord, jobs_root: Path, returncode: int | None, *, timed_out: bool
) -> job_records.JobRecord:
    """Return the final record, failing one the child left running or that timed out."""
    try:
        final = job_records.read_record(record.run_id, jobs_root) or record
    except OSError:  # unreadable record: settle what this process knows
        final = record
    code = final.exit_code
    if code is None:
        code = _START_FAILURE_EXIT_CODE if returncode is None else _shell_status(returncode)
    # A child that finished ``done`` before the stop landed keeps its record.
    if final.status == job_records.STATUS_RUNNING or (
        timed_out and final.status != job_records.STATUS_DONE
    ):
        final.status = job_records.STATUS_FAILED
        final.exit_code = code
        final.ended_at = final.ended_at or utc_now_rfc3339()
        job_records.write_record(final, jobs_root)
    return final


def _result_from(
    request: CheckoutFlowRequest,
    run_id: str,
    final: job_records.JobRecord,
    log_path: Path,
    report_dir: Path,
    *,
    timed_out: bool,
) -> CheckoutFlowResult:
    """The caller's view of a settled run: outcome, this run's reports and their typed metrics."""
    reports = _read_reports(report_dir, request.flow, run_id)
    if timed_out and final.status != job_records.STATUS_DONE:
        status = CheckoutFlowStatus.TIMED_OUT
    elif final.status == job_records.STATUS_CANCELLED:
        status = CheckoutFlowStatus.CANCELLED
    else:
        status = CheckoutFlowStatus.PASSED if final.exit_code == 0 else CheckoutFlowStatus.FAILED
    detail = as_dict(reports[-1][1].get("detail")) if reports else None
    return CheckoutFlowResult(
        run_id=run_id,
        status=status,
        exit_code=final.exit_code if final.exit_code is not None else _START_FAILURE_EXIT_CODE,
        reports=tuple(path for path, _ in reports),
        metrics={} if detail is None else report_metric_values(request.flow, detail),
        log=log_path,
    )


def run_checkout_flow(request: CheckoutFlowRequest) -> CheckoutFlowResult:
    """Run the Flow in the checkout and return its outcome, reports and typed metrics.

    Raises:
        CheckoutFlowError: the request is refused or the child cannot be started.
    """
    checkout, main, report_dir = _refuse_unsafe(request)
    request = replace(request, flow=canonical(request.flow))  # the CLI reports under this name
    run_id = job_records.make_run_id(request.flow, compact_utc_now(), 1)
    log_path = standalone_log_path(main, run_id)
    jobs_root = standalone_jobs_dir(main)
    record = job_records.JobRecord(
        run_id=run_id,
        endpoint=request.flow,
        started_at=utc_now_rfc3339(),
        timeout_s=STANDALONE_TIMEOUT_S
        if request.timeout_s is None
        else math.ceil(request.timeout_s),
        argv=["booley", "flow", request.flow, "--target", request.target],
        report_root=str(report_dir),
    )
    try:
        job_records.write_record_strict(record, jobs_root)
        register_job(main, run_id, request.flow, None)
        process, scratch = _launch(request, checkout, main, report_dir, run_id, log_path)
    except OSError as exc:
        # Best effort: an unwritable log must not stop the record turning failed.
        with contextlib.suppress(OSError), log_path.open("ab") as log:
            log.write(f"Checkout Flow not started: {exc}\n".encode())
        record.status = job_records.STATUS_FAILED
        record.exit_code = _START_FAILURE_EXIT_CODE
        record.ended_at = utc_now_rfc3339()
        job_records.write_record(record, jobs_root)
        raise CheckoutFlowError(f"Flow {request.flow!r} could not be started: {exc}") from exc
    timed_out = False
    try:
        timed_out = _wait(process, request.timeout_s)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        final = _settle_record(record, jobs_root, process.returncode, timed_out=timed_out)
    return _result_from(request, run_id, final, log_path, report_dir, timed_out=timed_out)


def _run_flow_cli(flow: str, flow_argv: list[str]) -> int:
    """Run ``booley flow`` with the working directory as the explicitly selected Project Root."""
    from booley.harness import booley as booley_cli

    sys.argv = ["booley", "--project", str(Path.cwd()), "flow", flow, *flow_argv]
    return booley_cli.main()


def child_main(argv: list[str] | None = None) -> int:
    """The child half: adopt the Job record the parent wrote and run the Flow in the cwd.

    *argv* is ``<main booley dir> <run id> <flow> [flow arguments...]``.
    """
    main_dir, run_id, flow, *flow_argv = sys.argv[1:] if argv is None else argv
    job = StandaloneJob.adopt(Path(main_dir), run_id)
    if job is None:
        print(f"ERROR: Checkout Flow record not found: {run_id}", file=sys.stderr)
        return _START_FAILURE_EXIT_CODE
    # Its own run id: the CLI then sees a nested run and writes no second record.
    os.environ[RUN_ID_ENV] = run_id
    # fd 1 is the block-buffered ``job.log``: a live log would stay empty.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    return job.run(lambda: _run_flow_cli(flow, flow_argv))
