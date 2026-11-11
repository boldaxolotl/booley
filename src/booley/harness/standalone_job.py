"""Standalone Jobs: a human's `booley flow` / `booley specialist` shell run as a Job.

A run typed in a Sandbox shell is foreground and in-process, so it used to leave
no Job record. :class:`StandaloneJob` gives it one: a Job record with no session
under ``<common Booley Directory>/.interactive_logs/standalone/.runtime/jobs`` (a
root the Dashboard already scans) and a ``job.log`` copy of the run's output next
to it. The terminal output is unchanged and the process exits exactly as before.

Liveness of these records is judged by ``process_identity`` (Dashboard, directory
migration). ``derive_status`` and ``is_active``, which compare ``argv`` with
``/proc``, only ever read MCP, Goal and baseline roots, never this one, so the
human ``argv`` stored here is for display and never matched against a process.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import BinaryIO, TextIO

from booley.core.booley_layout import INTERACTIVE_LOGS_DIR_NAME
from booley.flows.cli_selection import INVOCATION_ORIGIN_ENV
from booley.presentation.job_log_stream import JobLogStream
from booley.runtime import job_records
from booley.runtime.handles import register_job
from booley.runtime.pid import (
    DEAD,
    REUSED,
    ZOMBIE,
    ProcessIdentity,
    capture_process_identity,
    observe_process,
)
from booley.runtime.session_paths import logs_runtime_dir, session_jobs_dir
from booley.runtime.timefmt import compact_utc_now, utc_now_rfc3339

# A foreground run has no Job budget, but strict readers need a positive timeout.
STANDALONE_TIMEOUT_S = 7 * 24 * 3600
_STANDALONE_DIR = "standalone"
JOB_LOG_NAME = "job.log"
RUN_ID_ENV = "BOOLEY_RUN_ID"
_CTRL_C_EXIT_CODE = 130
_SignalHandler = signal.Handlers | int | Callable[[int, FrameType | None], object] | None


def standalone_log_root(booley_dir: Path) -> Path:
    """Human-facing log root of every Standalone Job."""
    return booley_dir / INTERACTIVE_LOGS_DIR_NAME / _STANDALONE_DIR


def standalone_jobs_dir(booley_dir: Path) -> Path:
    """Directory holding the Standalone Job records."""
    return session_jobs_dir(logs_runtime_dir(standalone_log_root(booley_dir)))


def standalone_log_path(booley_dir: Path, run_id: str) -> Path:
    """Where the output of one Standalone Job is copied."""
    return standalone_log_root(booley_dir) / run_id / JOB_LOG_NAME


def standalone_request_path(booley_dir: Path, run_id: str) -> Path:
    """Where a detached Standalone Job's request is written, beside its ``job.log``."""
    return standalone_log_root(booley_dir) / run_id / "request.json"


def checkout_report_root(project_root: Path) -> str | None:
    """The Project's resolved checkout Booley Directory, where this run's reports land."""
    from booley.runtime.booley_dir import resolve_checkout_booley_dir

    try:
        # Resolved like the common directory the Dashboard scans from: a symlinked
        # ``.booley`` is outside that scan root under its lexical name.
        return str(resolve_checkout_booley_dir(project_root).resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def _exit_code(code: object) -> int:
    """The process exit status a ``SystemExit`` payload means."""
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


class StandaloneJob:
    """One shell run's Job record and ``job.log``, from :meth:`prepare` to the terminal state."""

    def __init__(
        self, booley_dir: Path, record: job_records.JobRecord, *, adopted: bool = False
    ) -> None:
        self._booley_dir = booley_dir
        self._record = record
        self._root = standalone_jobs_dir(booley_dir)
        self._finished = False
        # An adopted Job's fds 1 and 2 already are its ``job.log``: nothing to tee.
        self._adopted = adopted

    @classmethod
    def adopt(cls, booley_dir: Path, run_id: str) -> StandaloneJob | None:
        """The Job a detached worker runs: the record its submitter wrote, or ``None``."""
        record = job_records.read_record(run_id, standalone_jobs_dir(booley_dir))
        if record is None:
            return None
        return cls(booley_dir, record, adopted=True)

    @classmethod
    def prepare(
        cls, project_root: Path, kind: str, name: str, argv: list[str]
    ) -> StandaloneJob | None:
        """Allocate the run id and export it, or ``None`` when this run is not a Standalone Job.

        Not a Standalone Job: an MCP child or Doctor probe (marked by its invocation
        origin), a run inside another Job's process tree (``BOOLEY_RUN_ID`` is set),
        or a Project whose common Booley Directory cannot be resolved.
        """
        from booley.runtime.booley_dir import resolve_common_booley_dir

        if os.environ.get(INVOCATION_ORIGIN_ENV) == "transport" or os.environ.get(RUN_ID_ENV):
            return None
        try:
            booley_dir = resolve_common_booley_dir(project_root)
        except (OSError, ValueError, RuntimeError):
            return None
        started_at = utc_now_rfc3339()
        record = job_records.JobRecord(
            run_id=job_records.make_run_id(name, compact_utc_now(), 1),
            endpoint=name,
            started_at=started_at,
            timeout_s=STANDALONE_TIMEOUT_S,
            argv=["booley", kind, name, *argv],
            pid=os.getpid(),
            run_started_at=started_at,
            work_dir=str(project_root),
            report_root=checkout_report_root(project_root),
        )
        os.environ[RUN_ID_ENV] = record.run_id
        return cls(booley_dir, record)

    def discard(self) -> None:
        """Unexport the run id; the caller does this once the run, or its help path, is over."""
        os.environ.pop(RUN_ID_ENV, None)

    def run(self, run: Callable[[], int]) -> int:
        """Run *run* as this Job; its result, exit and exceptions pass through unchanged."""
        restore_signals = self._install_signal_handlers()
        try:
            if self._adopted:
                self._start_adopted()
                return self._run_recorded(run)
            log = self._start()
            if log is None:
                return run()
            return self._run_logged(run, log)
        finally:
            restore_signals()

    def _run_logged(self, run: Callable[[], int], log: BinaryIO) -> int:
        original = sys.stdout, sys.stderr
        # A closed fd 1 or 2 leaves the stream None: print() drops it silently, so keep it None.
        stdout: TextIO | None = original[0]
        stderr: TextIO | None = original[1]
        if stdout is not None:
            sys.stdout = JobLogStream(stdout, log)
        if stderr is not None:
            sys.stderr = JobLogStream(stderr, log)
        try:
            return self._run_recorded(run)
        finally:
            sys.stdout, sys.stderr = original
            log.close()

    def _run_recorded(self, run: Callable[[], int]) -> int:
        """Run *run* and write the terminal record its outcome means."""
        status, exit_code = job_records.STATUS_FAILED, 1
        try:
            exit_code = run()
            return exit_code
        except SystemExit as exc:
            exit_code = _exit_code(exc.code)
            raise
        except KeyboardInterrupt:
            status, exit_code = job_records.STATUS_CANCELLED, _CTRL_C_EXIT_CODE
            raise
        finally:
            if status == job_records.STATUS_FAILED:  # no Ctrl-C: the exit code decides
                status = job_records.terminal_status(exit_code, False)
            self._end(status, exit_code)

    def _start_adopted(self) -> None:
        """Stamp the submitter's pid-less record with this process and leave it ``running``."""
        self._reconcile_dead_records()
        record = self._record
        identity = capture_process_identity(os.getpid()) if sys.platform == "linux" else None
        record.pid = os.getpid()
        record.process_identity = None if identity is None else identity.to_payload()
        record.run_started_at = record.run_started_at or utc_now_rfc3339()
        job_records.write_record(record, self._root)

    def _start(self) -> BinaryIO | None:
        """Write the running record and open ``job.log``; ``None`` leaves the run unrecorded."""
        self._reconcile_dead_records()
        record = self._record

        # The Dashboard observes Linux /proc; optional identity must not block on host ps.
        identity = capture_process_identity(os.getpid()) if sys.platform == "linux" else None
        record.process_identity = None if identity is None else identity.to_payload()
        log_path = standalone_log_path(self._booley_dir, record.run_id)
        log: BinaryIO | None = None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab", buffering=0)  # unbuffered: a signal handler never flushes
            job_records.write_record_strict(record, self._root)
        except OSError as exc:
            if log is not None:
                log.close()
            self._finished = True  # unrecorded: the signal handler must not write a record
            print(f"WARN: Standalone Job record unavailable: {exc}", file=sys.stderr)
            return None
        register_job(
            self._booley_dir, record.run_id, record.endpoint, None, work_dir=record.work_dir
        )
        return log

    def _reconcile_dead_records(self) -> None:
        """Rewrite earlier runs killed before they could end as ``failed`` (no ``ended_at``).

        Mirrors MCP startup reconciliation: the missing ``ended_at`` lets
        ``job_handle_expired`` age the record from ``started_at``. Best effort.
        """
        with contextlib.suppress(OSError, job_records.JobRecordError):
            for old in job_records.list_records(self._root, skip_malformed=True):
                identity = ProcessIdentity.from_payload(old.process_identity)
                if old.status != job_records.STATUS_RUNNING or identity is None:
                    continue
                if observe_process(identity).state not in {DEAD, REUSED, ZOMBIE}:
                    continue
                # The process is gone, so its record is final. Re-read it: the run may have
                # written its terminal state after the listing above, and that must win.
                current = job_records.read_record(old.run_id, self._root)
                if current is not None and current.status == job_records.STATUS_RUNNING:
                    current.status = job_records.STATUS_FAILED
                    job_records.write_record(current, self._root)

    def _end(self, status: str, exit_code: int) -> None:
        self._finished = True
        self._record.status = status
        self._record.exit_code = exit_code
        self._record.ended_at = utc_now_rfc3339()
        job_records.write_record(self._record, self._root)

    def _install_signal_handlers(self) -> Callable[[], None]:
        """Record SIGTERM and SIGHUP as ``cancelled`` and still die by the signal.

        Only a default disposition is replaced, and only from the main thread.
        Installed before the first record write so no signal can land between a
        written ``running`` record and its handler.
        """
        previous: dict[int, _SignalHandler] = {}
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None or signal.getsignal(signum) != signal.SIG_DFL:
                continue
            with contextlib.suppress(ValueError):  # not the main thread
                previous[signum] = signal.signal(signum, self._on_signal)

        def restore() -> None:
            for signum, handler in previous.items():
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)

        return restore

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        """Lock-free: write the terminal record, restore the default and re-send the signal."""
        if not self._finished:
            self._end(job_records.STATUS_CANCELLED, 128 + signum)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
