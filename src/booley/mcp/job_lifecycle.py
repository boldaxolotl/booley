"""SDK-independent Job lifecycle backed by runtime records and admission slots.

The child owns admission. This owner supervises its process, pins persistence,
waits across server generations, and resolves terminal results before rendering.
Background tasks belong to the creating asyncio loop; loop shutdown aborts them
without recording the distinct user-requested CANCELLED outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from booley.flows.progress_lifecycle import repair_progress_after_reap
from booley.goals.paths import record_paths
from booley.mcp.call_context import CallContext
from booley.mcp.endpoint_process import (
    _cancel_adopted_process_group,
    _endpoint_subprocess_env,
    _run_subprocess,
    write_synthetic_endpoint_end,
)
from booley.mcp.job_results import (
    _KNOWN_JOB_ROOTS,
    JobRepository,
    LocatedJob,
    job_binding,
    read_record_or_none,
)
from booley.runtime import job_records as jobrec
from booley.runtime import job_slots
from booley.runtime.display_identity import DisplayIdentity, DisplayScope
from booley.runtime.handles import register_job
from booley.runtime.pid import is_pid_alive
from booley.runtime.timefmt import compact_utc_now, utc_now_rfc3339

logger = logging.getLogger(__name__)
_DISK_POLL_TICK_SECONDS = 5.0


class JobActivity(Protocol):
    """Narrow activity observer; does not own Job state or admission."""

    def mark_mcp_endpoint_start(self) -> None: ...
    def mark_mcp_endpoint_end(self) -> None: ...


class _IdleActivity:
    def mark_mcp_endpoint_start(self) -> None:
        pass

    def mark_mcp_endpoint_end(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class JobResult:
    """Authoritative terminal inputs; only fresh reports may vouch for the run."""

    exit_code: int
    stdout: str
    stderr: str
    report: dict[str, Any] | None
    report_fresh: bool


@dataclass(frozen=True, slots=True)
class JobPoll:
    """A finished, in-progress, or unknown Job, independent of wire presentation."""

    run_id: str
    status: Literal["unknown", "running", "finished"]
    tracked: bool
    result: JobResult | None = None
    record: jobrec.JobRecord | None = None
    phase: str = "RUNNING"
    progress: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class JobSubmission:
    """Submission outcome including whether it joined an existing live run."""

    poll: JobPoll
    attached: bool


class JobLifecycle:
    """In-process registry for detached endpoint jobs. Narrates; never enforces.

    Admission moved out of this class (ADR 0028): the spawned endpoint child
    claims its own slot in the shared on-disk store (``booley.runtime.job_slots``)
    from inside ``McpTool.main``, so concurrency is governed identically for MCP
    dispatch, Developer Agents, Specialists, and bare CLI invocations — this
    server included. A submit therefore always spawns the child immediately;
    a child over its class cap idles in the queue (a live process holding its
    claim — the cost ADR 0028 accepts for PID-native claim identity), and
    submit/poll render QUEUED/RUNNING/result from the claim + record state.
    """

    def __init__(self, lifetime: JobActivity | None = None) -> None:
        self._lifetime = lifetime or _IdleActivity()
        # run_id -> jobs root from the call context that started the job. A
        # job keeps writing there even if the server environment changes.
        self._job_roots: dict[str, Path | None] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        # run_id -> (exit_code, stdout, stderr, timed_out) once finished.
        self._results: dict[str, tuple[int, str, str, bool]] = {}
        self._counter = 0

    def _record_root(self, run_id: str) -> Path | None:
        """Root for *run_id*'s record writes: pinned at start, else the container root."""
        if run_id in self._job_roots:
            return self._job_roots[run_id]
        located = self._repository.locate(run_id)
        return None if located is None else located.root

    def _write_root(self, rec: jobrec.JobRecord) -> Path | None:
        """Use the admitted binding for every Goal write, including completion."""
        binding = job_binding(rec)
        if binding is not None:
            return record_paths(binding.booley_dir, binding.record_id).jobs_dir
        return self._record_root(rec.run_id)

    def _read_run(self, run_id: str) -> jobrec.JobRecord | None:
        """Read *run_id*'s record where it is written: the pinned root, else ``_locate_job``."""
        if run_id in self._job_roots:
            return read_record_or_none(run_id, self._job_roots[run_id])
        return self._repository.record(run_id)

    def _next_run_id(self, endpoint: str) -> str:
        self._counter += 1
        stamp = compact_utc_now()
        return jobrec.make_run_id(endpoint, stamp, self._counter)

    def _new_record(
        self,
        run_id: str,
        name: str,
        cmd: list[str],
        timeout: int,
    ) -> jobrec.JobRecord:
        return jobrec.JobRecord(
            run_id=run_id,
            endpoint=name,
            started_at=utc_now_rfc3339(),
            timeout_s=timeout,
            argv=cmd,
            display_scope=DisplayScope.current(),
        )

    @staticmethod
    def _stamp_context(rec: jobrec.JobRecord, context: CallContext) -> None:
        rec.work_dir = (
            str(context.work_dir)
            if context.binding
            else (str(context.explicit_work_dir) if context.explicit_work_dir else None)
        )
        rec.session_key = context.session_key
        rec.binding = None if context.binding is None else context.binding.to_json()
        from booley.runtime.booley_dir import resolve_booley_dir

        try:
            project = context.binding.booley_dir if context.binding else resolve_booley_dir()
            register_job(project, rec.run_id, rec.endpoint, rec.session_key, work_dir=rec.work_dir)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Job handle Project unavailable for %s: %s", rec.run_id, exc)

    def _stamp_pid(self, rec: jobrec.JobRecord, pid: int) -> None:
        rec.pid = pid
        from booley.runtime.pid import capture_process_identity

        # The Dashboard observes Linux /proc; optional identity must not block on host ps.
        identity = capture_process_identity(pid) if sys.platform == "linux" else None
        rec.process_identity = None if identity is None else identity.to_payload()
        jobrec.write_record(rec, root=self._write_root(rec))

    def _child_is_queued(self, rec: jobrec.JobRecord) -> bool:
        if rec.pid is None:
            return False
        root = job_slots.slots_dir()
        if root is None:
            return False
        try:
            state = job_slots.SlotStore(root).state_for_pid(rec.pid)
        except OSError:
            return False
        return state is not None and state.state == job_slots.QUEUED

    def _stamp_run_started(self, rec: jobrec.JobRecord) -> None:
        rec.run_started_at = utc_now_rfc3339()
        jobrec.write_record(rec, root=self._write_root(rec))

    def _repair_progress(self, rec: jobrec.JobRecord) -> None:
        """Terminalize the Job's checkpoint, following the Booley Directory entry."""
        located = self._repository.locate(rec.run_id)
        repair_progress_after_reap(
            self._repository.report_dirs(located),
            rec.endpoint,
            rec.run_id,
            booley_dir=self._repository.report_booley_dir(located),
        )

    def _record_terminal(
        self,
        rec: jobrec.JobRecord,
        exit_code: int,
        *,
        timed_out: bool,
    ) -> None:
        self._repair_progress(rec)
        rec.status = jobrec.terminal_status(exit_code, timed_out)
        rec.exit_code = exit_code
        rec.ended_at = utc_now_rfc3339()
        jobrec.write_record(rec, root=self._write_root(rec))

    @staticmethod
    def _display_identity(rec: jobrec.JobRecord) -> DisplayIdentity:
        return DisplayIdentity(rec.run_id, rec.display_scope)

    def _forget_synchronous(self, rec: jobrec.JobRecord) -> None:
        """Drop a finished inline run's record and its pinned root."""
        jobrec.delete_record(rec.run_id, root=self._write_root(rec))
        self._job_roots.pop(rec.run_id, None)

    def _abort_synchronous(self, rec: jobrec.JobRecord) -> None:
        self._record_terminal(rec, 2, timed_out=False)
        self._write_end(
            rec.endpoint,
            0,
            identity=self._display_identity(rec),
            outcome="aborted",
        )
        self._forget_synchronous(rec)

    def submit(
        self,
        name: str,
        cmd: list[str],
        timeout: int,
        *,
        context: CallContext,
    ) -> str:
        """Spawn *cmd* as a detached background job and return its run_id.

        Always spawns: admission (queue or run) is the child's own slot-store
        claim, not this server's decision. The record lives under the call
        context's jobs root.
        """
        run_id = context.binding.invocation_id if context.binding else self._next_run_id(name)
        rec = self._new_record(run_id, name, cmd, timeout)
        self._stamp_context(rec, context)
        self._job_roots[run_id] = context.jobs_root
        _KNOWN_JOB_ROOTS[run_id] = context.jobs_root
        jobrec.write_record(rec, root=self._write_root(rec))
        # The submit CALL returns in seconds (mark_mcp_endpoint_end fires then), so hold
        # the lifetime busy independently or the server idle-exits mid-run.
        self._lifetime.mark_mcp_endpoint_start()
        self._tasks[run_id] = asyncio.create_task(
            self._run_and_record(rec, context),
        )
        return run_id

    async def run_synchronous(
        self,
        name: str,
        cmd: list[str],
        timeout: int,
        *,
        context: CallContext,
    ) -> tuple[int, str, str, bool]:
        """Run an inline endpoint while publishing bounded status state."""
        rec = self._new_record(
            context.binding.invocation_id if context.binding else uuid.uuid4().hex,
            name,
            cmd,
            timeout,
        )
        self._stamp_context(rec, context)
        self._job_roots[rec.run_id] = context.jobs_root
        _KNOWN_JOB_ROOTS[rec.run_id] = context.jobs_root
        jobrec.write_record(rec, root=self._write_root(rec))
        try:
            result = await self._execute_record(rec, context, publish_display_identity=True)
        except asyncio.CancelledError:
            self._abort_synchronous(rec)
            raise
        except Exception:
            self._abort_synchronous(rec)
            raise
        exit_code, _stdout, _stderr, timed_out = result
        if timed_out:
            self._write_end(
                name,
                timeout,
                identity=self._display_identity(rec),
            )
        self._record_terminal(rec, exit_code, timed_out=timed_out)
        self._forget_synchronous(rec)
        return result

    async def _execute_record(
        self,
        rec: jobrec.JobRecord,
        context: CallContext,
        *,
        publish_display_identity: bool = False,
    ) -> tuple[int, str, str, bool]:
        """Apply one PID, admission, and environment policy to every supervised Job."""
        overrides = {"BOOLEY_RUN_ID": rec.run_id, "BOOLEY_SLOT_TIMEOUT_S": str(rec.timeout_s)}
        if publish_display_identity:
            overrides["BOOLEY_DISPLAY_INVOCATION_ID"] = rec.run_id
        return await self._run_process(
            rec.argv,
            timeout=rec.timeout_s,
            on_spawn=lambda pid: self._stamp_pid(rec, pid),
            env=_endpoint_subprocess_env(context, pytest_scope=rec.endpoint, **overrides),
            is_queued=lambda: self._child_is_queued(rec),
            on_first_active=lambda: self._stamp_run_started(rec),
        )

    async def _run_and_record(self, rec: jobrec.JobRecord, context: CallContext) -> None:
        """Run the subprocess to completion and persist the terminal outcome."""
        try:
            exit_code, stdout, stderr, timed_out = await self._execute_record(rec, context)
            if timed_out:
                self._write_end(rec.endpoint, rec.timeout_s, identity=self._display_identity(rec))
            self._results[rec.run_id] = (exit_code, stdout, stderr, timed_out)
            self._record_terminal(rec, exit_code, timed_out=timed_out)
        except asyncio.CancelledError:
            if rec.run_id not in self._cancel_requested:
                self._write_end(
                    rec.endpoint, 0, identity=self._display_identity(rec), outcome="aborted"
                )
                raise
            self._record_user_cancellation(rec)
        finally:
            self._lifetime.mark_mcp_endpoint_end()

    def _record_user_cancellation(self, rec: jobrec.JobRecord) -> None:
        self._repair_progress(rec)
        rec.status = jobrec.STATUS_CANCELLED
        rec.exit_code = 130
        rec.ended_at = utc_now_rfc3339()
        self._results[rec.run_id] = (
            130,
            "",
            f"CANCELLED: job {rec.run_id} was stopped by request.",
            False,
        )
        jobrec.write_record(rec, root=self._write_root(rec))
        self._write_end(
            rec.endpoint,
            0,
            identity=self._display_identity(rec),
            outcome="cancelled",
        )

    def _adopt_report_outcome(self, rec: jobrec.JobRecord) -> bool:
        report, report_fresh = self._report(rec)
        report_exit = report.get("exit_code") if report_fresh and report else None
        if not isinstance(report_exit, int):
            return False
        self._record_terminal(rec, report_exit, timed_out=False)
        return True

    def _cancel_waiter(self, rec: jobrec.JobRecord) -> bool:
        root = job_slots.slots_dir()
        if rec.pid is None or root is None:
            return False
        try:
            return job_slots.SlotStore(root).cancel_waiter(rec.pid)
        except OSError:
            return False

    async def _cancel_tracked_task(
        self,
        run_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if task.done():
            return
        self._cancel_requested.add(run_id)
        if not task.cancel():
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _cancel_adopted_job(self, rec: jobrec.JobRecord) -> None:
        if self._adopt_report_outcome(rec):
            return
        if rec.pid is not None and not self._is_alive(rec.pid):
            self._record_terminal(rec, 2, timed_out=False)
            return
        if rec.pid is not None:
            await self._cancel_process(rec.pid)
            if self._adopt_report_outcome(rec):
                return
        self._record_user_cancellation(rec)

    async def cancel(self, run_id: str) -> str | None:
        """Cancel a queued or running job; return its former phase.

        In-process jobs cancel their supervising task, which terminates the
        process tree before recording the outcome. Adopted jobs retain RUNNING
        state until termination and fresh-report reconciliation complete.
        """
        rec = self._read_run(run_id)
        if rec is None:
            return None

        if jobrec.derive_status(rec, self._is_alive) != jobrec.STATUS_RUNNING:
            return "already_finished"

        was_queued = self._cancel_waiter(rec)
        task = self._tasks.get(run_id)
        if task is not None:
            await self._cancel_tracked_task(run_id, task)
        else:
            await self._cancel_adopted_job(rec)
        final = self._read_run(run_id)
        if final is None or final.status != jobrec.STATUS_CANCELLED:
            return "finished"
        return "queued" if was_queued else "running"

    async def wait(self, run_id: str, timeout: float) -> bool | None:
        """Wait up to *timeout* seconds for a tracked job.

        Returns True (finished), False (still running), or None when *run_id* is
        not tracked in this server process (caller falls back to the durable
        disk record). ``asyncio.wait`` never cancels the task on timeout, so a
        poll that gives up leaves the job running.
        """
        task = self._tasks.get(run_id)
        if task is None:
            return None
        if not task.done() and timeout > 0:
            await asyncio.wait({task}, timeout=timeout)
        return task.done()

    def _result_parts(
        self,
        run_id: str,
    ) -> tuple[int, str, str, dict[str, Any] | None, bool]:
        """Resolve one terminal Job into authoritative rendering inputs."""
        rec = self._read_run(run_id)
        report, report_fresh = self._report(rec)
        finished = self._results.get(run_id)
        if finished is not None:
            exit_code, stdout, stderr, _timed_out = finished
            return exit_code, stdout, stderr, report, report_fresh
        if rec is not None and rec.status == jobrec.STATUS_CANCELLED:
            return (
                130,
                "",
                f"CANCELLED: job {run_id} was stopped by request.",
                report,
                report_fresh,
            )
        # Terminal-from-disk (server restarted): no captured stdout/stderr.
        exit_code = rec.exit_code if rec and rec.exit_code is not None else None
        if exit_code is None and report_fresh and isinstance(report.get("exit_code"), int):
            # Orphan rescue: the run itself finished and wrote this job's
            # report — only the server died before recording the outcome.
            # Trust the report's exit code over a blanket failure, so a
            # passing sim that outlived a SIGKILLed server still reads as a
            # pass instead of "EXIT_CODE: 2" with a passing report below it.
            exit_code = report["exit_code"]
        if exit_code is None:
            exit_code = 2
        return exit_code, "", "", report, report_fresh

    _is_alive = staticmethod(is_pid_alive)
    _repository = JobRepository()
    _run_process = staticmethod(_run_subprocess)
    _write_end = staticmethod(write_synthetic_endpoint_end)
    _cancel_process = staticmethod(_cancel_adopted_process_group)

    def _report(self, rec: jobrec.JobRecord | None) -> tuple[dict[str, Any] | None, bool]:
        return self._repository.report(rec)

    def terminal_result(self, run_id: str) -> JobResult:
        """Resolve captured output or a persisted outcome with report freshness."""
        return JobResult(*self._result_parts(run_id))

    async def submit_and_wait(
        self, name: str, cmd: list[str], timeout: int, *, context: CallContext, wait_seconds: float
    ) -> JobSubmission:
        """Join identical live work or submit once, then wait within the inline budget."""
        attached = self._repository.find_attachable(name, cmd, context=context)
        run_id = (
            attached if attached is not None else self.submit(name, cmd, timeout, context=context)
        )
        return JobSubmission(await self.poll(run_id, wait_seconds), attached is not None)

    async def poll(
        self, run_id: str, wait_seconds: float = 0.0, *, disk_tick: float = _DISK_POLL_TICK_SECONDS
    ) -> JobPoll:
        """Wait without cancelling live work; adopted Jobs use the same bounded budget."""
        state = await _poll_status(
            run_id, wait_seconds, self.wait, self._repository, self._is_alive, disk_tick=disk_tick
        )
        return self._complete_poll(state)

    async def poll_from_disk(
        self, run_id: str, wait_seconds: float = 0.0, *, disk_tick: float = _DISK_POLL_TICK_SECONDS
    ) -> JobPoll:
        """Resolve a bounded long poll from durable records after a restart."""
        state = await _poll_disk_status(
            run_id, wait_seconds, self._repository, self._is_alive, disk_tick=disk_tick
        )
        return self._complete_poll(state)

    def _complete_poll(self, state: JobPoll) -> JobPoll:
        if state.status == "finished":
            return replace(state, result=self.terminal_result(state.run_id))
        return state

    def reconcile_orphaned(self) -> None:
        """Adopt terminal evidence using existing runtime liveness/deadline rules."""
        from booley.runtime.bootstrap_mode import should_run_outer_bookkeeping

        if not should_run_outer_bookkeeping():
            return
        for located in (
            LocatedJob(rec, root)
            for root in self._repository.roots()
            for rec in jobrec.list_records(root=root)
        ):
            self._reconcile_one(located)

    def _reconcile_one(self, located: LocatedJob) -> None:
        rec = located.record
        if rec.status != jobrec.STATUS_RUNNING:
            return
        if jobrec.derive_status(rec, self._is_alive) == jobrec.STATUS_RUNNING:
            return
        report, fresh = self._report(rec)
        report_exit = report.get("exit_code") if fresh and report else None
        if isinstance(report_exit, int):
            rec.exit_code = report_exit
            rec.status = jobrec.terminal_status(report_exit, timed_out=False)
        else:
            rec.status = jobrec.STATUS_FAILED
            if rec.exit_code is None:
                rec.exit_code = 2
        self._repair_progress(rec)
        jobrec.write_record(rec, root=located.root)
        logger.info("Reconciled orphaned job %s from prior session", rec.run_id)


async def _poll_status(
    run_id: str,
    wait_seconds: float,
    wait: Callable[[str, float], Awaitable[bool | None]],
    repository: JobRepository,
    alive: Callable[[int], bool],
    *,
    disk_tick: float = _DISK_POLL_TICK_SECONDS,
) -> JobPoll:
    """Internal waiting seam retained for the adapter's historical waiter protocol."""
    finished = await wait(run_id, wait_seconds)
    if finished is None:
        return await _poll_disk_status(
            run_id, wait_seconds, repository, alive, disk_tick=disk_tick
        )
    return _poll_view(run_id, "finished" if finished else "running", True, repository)


async def _poll_disk_status(
    run_id: str,
    wait_seconds: float,
    repository: JobRepository,
    alive: Callable[[int], bool],
    *,
    disk_tick: float = _DISK_POLL_TICK_SECONDS,
) -> JobPoll:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        rec = repository.record(run_id)
        if rec is None:
            return JobPoll(run_id, "unknown", False)
        if jobrec.derive_status(rec, alive) != jobrec.STATUS_RUNNING:
            return _poll_view(run_id, "finished", False, repository)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _poll_view(run_id, "running", False, repository)
        await asyncio.sleep(min(disk_tick, remaining))


def _poll_view(
    run_id: str, status: Literal["running", "finished"], tracked: bool, repository: JobRepository
) -> JobPoll:
    return JobPoll(
        run_id,
        status,
        tracked,
        record=repository.record(run_id),
        phase=repository.phase(run_id),
        progress=repository.progress(run_id) if status == "running" else None,
    )
