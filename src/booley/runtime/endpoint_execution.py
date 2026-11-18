"""Transport-independent sequencing for one Flow, Specialist, or MCP endpoint.

The coordinator deliberately knows nothing about argparse, MCP schemas, wire
payloads, or concrete Flow/Specialist implementations.  An adapter supplies the
small lifecycle interface below; this module owns the ordering shared by CLI
and MCP-triggered processes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, cast

# Re-exported: callers import the verdict codes from here.
from booley.core.exit_codes import (  # noqa: F401
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
)

logger = logging.getLogger(__name__)
_MAX_COMPLETION_MESSAGE_CHARS = 500

#: Why a Job's second attempt waits for the memory to itself (ADR 0087).
OOM_REQUEUE_REASON = "requeued after out-of-memory"
#: Opens the note a requeued Job's final result ends with.
OOM_REQUEUE_NOTE = "Requeued after out-of-memory"
#: Opens each line a Job's final result adds for an OOM kill that is not retried.
OOM_KILL_NOTE = "Killed for out-of-memory"
#: Opens the line a Job's final result leads with when the host, not the Sandbox's limit, killed it.
HOST_OOM_KILL_NOTE = "Killed for host out-of-memory"
#: Printed to stdout between the killed attempt's output and the final attempt's.
OOM_REQUEUE_MARKER = f"[requeue] {OOM_REQUEUE_REASON}; the output above is from the killed attempt"
#: What a command that was not started because the attempt's earlier command was OOM-killed reports.
OOM_STOPPED_MESSAGE = (
    "Not run: an earlier command of this Job was OOM-killed; the Job is requeued."
)


@dataclass
class EndpointOutcome:
    """Verdict and evidence returned by an endpoint implementation."""

    exit_code: int = EXIT_SUCCESS
    detail: dict[str, Any] = field(default_factory=dict)
    report_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: float = 0.0
    lines_added: int = 0
    lines_removed: int = 0
    display_lines: list[str] = field(default_factory=list)
    display_label: str | None = None
    summary: str = ""
    cycle_report_json: str = "{}"
    # Whether the OOM killer took a process of one of this Job's own commands.
    # Set by ``execute_endpoint``; an endpoint never sets it itself.
    oom_killed: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Final coordinator result shared by transport adapters."""

    exit_code: int
    outcome: EndpointOutcome


class EndpointRejectedError(RuntimeError):
    """Expected pre-invocation rejection carrying its structured outcome."""

    def __init__(self, outcome: EndpointOutcome) -> None:
        super().__init__(outcome.report_text)
        self.outcome = outcome


def normalize_completion_error(
    outcome: EndpointOutcome,
    exc: Exception,
    operation: str,
    *,
    path: str | os.PathLike[str] | None = None,
) -> EndpointOutcome:
    """Add one endpoint-completion failure without discarding known evidence."""
    error_path = path
    if error_path is None and isinstance(exc, OSError):
        filename = exc.filename
        if isinstance(filename, (str, os.PathLike)):
            error_path = filename
    message = str(exc)
    if len(message) > _MAX_COMPLETION_MESSAGE_CHARS:
        message = f"{message[: _MAX_COMPLETION_MESSAGE_CHARS - 1]}…"
    completion_error: dict[str, str] = {
        "operation": operation,
        "type": type(exc).__name__,
        "message": message,
    }
    if error_path is not None:
        completion_error["path"] = os.fspath(error_path)
    outcome.exit_code = EXIT_ERROR
    outcome.detail = dict(outcome.detail)
    primary = outcome.detail.get("completion_error")
    if isinstance(primary, dict):
        secondary = list(outcome.detail.get("completion_errors", []))
        secondary.append(completion_error)
        outcome.detail["completion_errors"] = secondary
    else:
        outcome.detail["completion_error"] = completion_error
    diagnosis = f"Completion failure ({operation}): {type(exc).__name__}: {message}"
    outcome.report_text = "\n".join(filter(None, (outcome.report_text, diagnosis)))
    return outcome


Prepared_contra = TypeVar("Prepared_contra", contravariant=True)


class RecordPublisher(Protocol[Prepared_contra]):
    """Publish the records of one prepared request."""

    def publish_records(self, prepared: Prepared_contra, outcome: EndpointOutcome) -> None:
        """Publish records before the report is written."""
        ...


class ExecutableEndpoint(
    RecordPublisher[Prepared_contra],
    Protocol[Prepared_contra],
):
    """Internal adapter interface consumed by :func:`execute_endpoint`.

    The interface describes lifecycle stages, not the implementation helpers
    historically inherited by endpoint classes.  Transport adapters may parse
    different request representations while sharing the same execution order.
    """

    def admission(
        self, prepared: Prepared_contra, *, requeued: bool = False
    ) -> AbstractContextManager[object | None]:
        """Validate and yield the admission value held through finish.

        *requeued* admits the Job's second attempt, after an OOM kill, to run alone.
        """
        ...

    def may_requeue_after_oom(self, prepared: Prepared_contra, admission: object | None) -> bool:
        """Whether an OOM kill of this admitted Job's first attempt requeues it."""
        ...

    def begin_requeued_attempt(self) -> None:
        """Reset the state one attempt leaves behind, before the Job's second attempt."""
        ...

    def invoke_endpoint(
        self,
        prepared: Prepared_contra,
        *,
        admission: object | None,
        started: float,
    ) -> EndpointOutcome:
        """Run the endpoint implementation and finalize its raw outcome."""
        ...

    def finish_execution(
        self,
        prepared: Prepared_contra,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        records_published: bool,
    ) -> ExecutionResult:
        """Publish completion; the report refreshes its detail once records are published."""
        ...


def _publish_and_finish(
    endpoint: ExecutableEndpoint[Prepared_contra],
    prepared: Prepared_contra,
    outcome: EndpointOutcome,
    *,
    started: float | None,
) -> ExecutionResult:
    records_published = False
    try:
        endpoint.publish_records(prepared, outcome)
        records_published = True
    except Exception as exc:
        logger.debug("Endpoint record publication failed", exc_info=True)
        normalize_completion_error(outcome, exc, "publish Simulation Record projections")
    return endpoint.finish_execution(
        prepared,
        outcome,
        started=started,
        records_published=records_published,
    )


@dataclass(eq=False)
class _JobAttempt:
    """One attempt of a Job or of a Simulation Record work item: whether its commands stop after an OOM kill.

    *parent* is the attempt it runs inside (a work item's attempt inside its
    Flow's), so a kill marks only the innermost attempt while a kill of an
    armed ancestor still stops the work inside it.
    """

    stop_on_oom: bool
    parent: _JobAttempt | None = None
    work_item: bool = False
    oom_killed: threading.Event = field(default_factory=threading.Event)
    # A command was killed while the Sandbox stayed below its own limit: never retried.
    host_oom_killed: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)
    # Work items whose last attempt inside this one died of its own OOM kill (lanes append).
    final_item_kills: list[str] = field(default_factory=list[str])
    final_item_host_kills: list[str] = field(default_factory=list[str])
    _kills_lock: threading.Lock = field(default_factory=threading.Lock)


# The current attempt; shared by the Job's threads (Simulation Record lanes copy the context).
_JOB_ATTEMPT: ContextVar[_JobAttempt | None] = ContextVar("booley_job_attempt", default=None)


# The running Job's opaque id; ``None`` outside ``execute_endpoint``.
_JOB_ID: ContextVar[str | None] = ContextVar("booley_job_id", default=None)


def current_job() -> str | None:
    """The id of the Job this code runs in, shared by its attempts and threads; it sorts by start."""
    return _JOB_ID.get()


@contextmanager
def job_attempt(*, stop_on_oom: bool, work_item: bool = False) -> Iterator[_JobAttempt]:
    """Run one attempt of a Job; with *stop_on_oom* its commands stop after an OOM kill.

    A *work_item* attempt is one Simulation Record work item's, run inside the Flow's.
    """
    attempt = _JobAttempt(stop_on_oom=stop_on_oom, parent=_JOB_ATTEMPT.get(), work_item=work_item)
    token = _JOB_ATTEMPT.set(attempt)
    try:
        yield attempt
    finally:
        _JOB_ATTEMPT.reset(token)


def note_job_oom_kill(*, host: bool = False) -> None:
    """Record that a command of the current attempt was OOM-killed; nothing outside a Job.

    A *host* kill (the Sandbox stayed below its limit) is recorded apart: it
    never stops or requeues the attempt, because running alone does not help.
    """
    attempt = _JOB_ATTEMPT.get()
    if attempt is None:
        return
    (attempt.host_oom_killed if host else attempt.oom_killed).set()


def note_final_work_item_oom_kill(name: str, *, passed: bool, raised: bool = False) -> None:
    """Record that work item *name*'s last attempt died of its own OOM kill, on its Job's attempt.

    Only an unarmed work item attempt that was killed and failed counts: an
    armed one is requeued, and a passed one keeps its result, unless *raised*:
    the attempt's execution failed after the kill, so nothing requeues it.
    Nothing outside a work item attempt.
    """
    attempt = _JOB_ATTEMPT.get()
    if attempt is None or not attempt.work_item or passed or attempt.parent is None:
        return
    # A host kill is never requeued, armed or not.
    if attempt.host_oom_killed.is_set():
        with attempt.parent._kills_lock:
            attempt.parent.final_item_host_kills.append(name)
        return
    if (attempt.stop_on_oom and not raised) or not attempt.oom_killed.is_set():
        return
    with attempt.parent._kills_lock:
        attempt.parent.final_item_kills.append(name)


def _limit_killed(attempt: _JobAttempt) -> bool:
    """Whether *attempt* has a Sandbox-limit kill to requeue.

    A host kill anywhere in the attempt vetoes it: the host's pressure stays,
    so a rerun cannot help, whatever other command hit the limit.
    """
    return attempt.oom_killed.is_set() and not attempt.host_oom_killed.is_set()


def job_stopped_after_oom() -> bool:
    """Whether the current attempt must start no more commands.

    True once an attempt that will be requeued had a command OOM-killed: the
    rest of its work is wasted, so it ends at its first kill (ADR 0087). A kill
    of an enclosing Job attempt stops the work inside it too.
    """
    attempt = _JOB_ATTEMPT.get()
    while attempt is not None:
        if attempt.stop_on_oom and _limit_killed(attempt):
            attempt.stopped.set()
            return True
        attempt = attempt.parent
    return False


def work_item_attempt_requeues(*, passed: bool) -> bool:
    """Whether the current Simulation Record work item attempt ended with an own OOM kill to requeue.

    Only an armed work item attempt that was killed itself counts, and only
    when its result does not survive the kill: it failed, or it skipped a
    command (the Job rule). An item running under its Flow's claim has no
    attempt of its own: its kill marks the Flow's attempt.
    """
    attempt = _JOB_ATTEMPT.get()
    if attempt is None or not attempt.work_item or not attempt.stop_on_oom:
        return False
    return _limit_killed(attempt) and (not passed or attempt.stopped.is_set())


def _name_requeue(outcome: EndpointOutcome, *, started_again: bool) -> None:
    """Tell the caller that this final result is the requeued Job's second attempt."""
    outcome.detail = {
        **outcome.detail,
        "oom_requeue": {"reason": OOM_REQUEUE_REASON, "attempts": 2},
    }
    line = (
        f"{OOM_REQUEUE_NOTE}: this result is from the Job's second attempt, which ran alone."
        if started_again
        else f"{OOM_REQUEUE_NOTE}: the Job did not start again."
    )
    outcome.report_text = "\n".join(filter(None, (outcome.report_text, line)))


def _name_final_oom_kills(
    outcome: EndpointOutcome, attempt: _JobAttempt, *, requeued: bool
) -> None:
    """Tell the caller which OOM kills ended this Job's final attempt, and that none is retried."""
    if outcome.exit_code == EXIT_CANCELLED:
        return
    lines: list[str] = []
    host = _host_killed(outcome, attempt)
    if host:
        outcome.report_text = "\n".join(filter(None, (_host_oom_line(host), outcome.report_text)))
    if _killed_for_oom(outcome, attempt):
        lines.append(
            f"{OOM_KILL_NOTE}: the second attempt was OOM-killed too, so the Job is not retried again."
            if requeued
            else f"{OOM_KILL_NOTE}: this Job may not be requeued after an OOM kill, so it is not retried."
        )
    lines.extend(
        f"{OOM_KILL_NOTE}: work item {name}'s last attempt was OOM-killed, so it is not retried again."
        for name in attempt.final_item_kills
    )
    outcome.report_text = "\n".join(filter(None, (outcome.report_text, *lines)))


def _host_killed(outcome: EndpointOutcome, attempt: _JobAttempt) -> list[str]:
    """What the host's OOM killer took from this final attempt: the Job, work items by name."""
    names = list(attempt.final_item_host_kills)
    if attempt.host_oom_killed.is_set() and outcome.exit_code != EXIT_SUCCESS:
        names.insert(0, "the Job")
    return names


def _host_oom_line(names: list[str]) -> str:
    subjects = ", ".join(name if name == "the Job" else f"work item {name}" for name in names)
    verb = "is" if len(names) == 1 else "are"
    return (
        f"{HOST_OOM_KILL_NOTE}: the host ran out of memory while the Sandbox stayed within its "
        f"[sandbox].memory limit, so {subjects} {verb} not retried. Free memory on the host, "
        "or lower [sandbox].memory and recreate the Sandbox, then run it again."
    )


def _killed_for_oom(outcome: EndpointOutcome, attempt: _JobAttempt) -> bool:
    """Whether *attempt* ended with an own OOM kill that its result does not survive.

    A cancelled Job is never requeued. A Job that succeeded is requeued only
    when the kill made it skip a command, because its result then misses work.
    """
    if not _limit_killed(attempt) or outcome.exit_code == EXIT_CANCELLED:
        return False
    return outcome.exit_code != EXIT_SUCCESS or attempt.stopped.is_set()


def _attempt_endpoint(
    endpoint: ExecutableEndpoint[Prepared_contra],
    request: Prepared_contra,
    *,
    requeued: bool,
) -> ExecutionResult | None:
    """Admit and run one attempt; ``None`` after releasing its claim to requeue the Job.

    Once admitted, the claim stays held through record publication and
    reporting. A first attempt that is requeued publishes
    nothing: its records, report and display end never happen, and its
    progress document is retired so no caller reads it for the Job.
    """
    admission = ExitStack()
    try:
        borrowed = admission.enter_context(endpoint.admission(request, requeued=requeued))
    except EndpointRejectedError as exc:
        if requeued:
            _name_requeue(exc.outcome, started_again=False)
        return _publish_and_finish(endpoint, request, exc.outcome, started=None)
    with admission:
        started = time.monotonic()
        # The requeued attempt is the last one: it is never armed.
        armed = not requeued and endpoint.may_requeue_after_oom(request, borrowed)
        with job_attempt(stop_on_oom=armed) as attempt:
            outcome = endpoint.invoke_endpoint(request, admission=borrowed, started=started)
        outcome.oom_killed = attempt.oom_killed.is_set() or attempt.host_oom_killed.is_set()
        if armed and _killed_for_oom(outcome, attempt):
            endpoint.begin_requeued_attempt()
            return None
        if requeued:
            _name_requeue(outcome, started_again=True)
        _name_final_oom_kills(outcome, attempt, requeued=requeued)
        return _publish_and_finish(endpoint, request, outcome, started=started)


def execute_endpoint(
    endpoint: ExecutableEndpoint[Prepared_contra],
    request: Prepared_contra,
) -> ExecutionResult:
    """Execute one endpoint with invariant lifecycle ordering.

    The transport adapter prepares *request* before calling this interface.
    Admission includes pre-execution validation. A memory-admitted Job whose
    first attempt was OOM-killed gets one more attempt, run alone, and its
    result comes from that attempt only.
    """
    # A fresh id, not BOOLEY_RUN_ID: an inherited one could merge separate invocations.
    # It starts with the time, so ids sort by Job start.
    token = _JOB_ID.set(f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}")
    try:
        first = _attempt_endpoint(endpoint, request, requeued=False)
        if first is not None:
            return first
        # The requeued attempt is never armed, so it always finishes.
        return cast(ExecutionResult, _attempt_endpoint(endpoint, request, requeued=True))
    finally:
        _JOB_ID.reset(token)
