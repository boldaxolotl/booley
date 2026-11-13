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
    criterion_key: str = ""
    criterion_met: bool | None = False
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


class AcceptanceRecorder(Protocol[Prepared_contra]):
    """Record immutable Criterion evidence for one prepared request."""

    def record_acceptance(self, prepared: Prepared_contra, outcome: EndpointOutcome) -> None:
        """Record evidence before mutable persistence begins."""
        ...


class ExecutableEndpoint(
    AcceptanceRecorder[Prepared_contra],
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
        acceptance_recorded: bool,
    ) -> ExecutionResult:
        """Publish completion, persisting mutable state only after acceptance."""
        ...


def _record_and_finish(
    endpoint: ExecutableEndpoint[Prepared_contra],
    prepared: Prepared_contra,
    outcome: EndpointOutcome,
    *,
    started: float | None,
) -> ExecutionResult:
    acceptance_recorded = False
    try:
        endpoint.record_acceptance(prepared, outcome)
        acceptance_recorded = True
    except Exception as exc:
        logger.debug("Endpoint acceptance failed", exc_info=True)
        normalize_completion_error(outcome, exc, "record acceptance and projections")
    return endpoint.finish_execution(
        prepared,
        outcome,
        started=started,
        acceptance_recorded=acceptance_recorded,
    )


@dataclass(eq=False)
class _JobAttempt:
    """One attempt of a Job: whether its commands stop after an OOM kill, and what happened."""

    stop_on_oom: bool
    oom_killed: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)


# The current attempt; shared by the Job's threads (Campaign lanes copy the context).
_JOB_ATTEMPT: ContextVar[_JobAttempt | None] = ContextVar("booley_job_attempt", default=None)


@contextmanager
def job_attempt(*, stop_on_oom: bool) -> Iterator[_JobAttempt]:
    """Run one attempt of a Job; with *stop_on_oom* its commands stop after an OOM kill."""
    attempt = _JobAttempt(stop_on_oom=stop_on_oom)
    token = _JOB_ATTEMPT.set(attempt)
    try:
        yield attempt
    finally:
        _JOB_ATTEMPT.reset(token)


def note_job_oom_kill() -> None:
    """Record that a command of the current Job was OOM-killed; nothing outside a Job."""
    attempt = _JOB_ATTEMPT.get()
    if attempt is not None:
        attempt.oom_killed.set()


def job_stopped_after_oom() -> bool:
    """Whether the current attempt must start no more commands.

    True once an attempt that will be requeued had a command OOM-killed: the
    rest of its work is wasted, so it ends at its first kill (ADR 0087).
    """
    attempt = _JOB_ATTEMPT.get()
    if attempt is None or not attempt.stop_on_oom or not attempt.oom_killed.is_set():
        return False
    attempt.stopped.set()
    return True


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


def _is_requeued(outcome: EndpointOutcome, attempt: _JobAttempt) -> bool:
    """Whether *attempt* ended with an own OOM kill that its result does not survive.

    A cancelled Job is never requeued. A Job that succeeded is requeued only
    when the kill made it skip a command, because its result then misses work.
    """
    if not attempt.oom_killed.is_set() or outcome.exit_code == EXIT_CANCELLED:
        return False
    return outcome.exit_code != EXIT_SUCCESS or attempt.stopped.is_set()


def _attempt_endpoint(
    endpoint: ExecutableEndpoint[Prepared_contra],
    request: Prepared_contra,
    *,
    requeued: bool,
) -> ExecutionResult | None:
    """Admit and run one attempt; ``None`` after releasing its claim to requeue the Job.

    Once admitted, the claim stays held through acceptance recording and
    mutable persistence/reporting. A first attempt that is requeued publishes
    nothing: its acceptance, report and display end never happen.
    """
    admission = ExitStack()
    try:
        borrowed = admission.enter_context(endpoint.admission(request, requeued=requeued))
    except EndpointRejectedError as exc:
        if requeued:
            _name_requeue(exc.outcome, started_again=False)
        return _record_and_finish(endpoint, request, exc.outcome, started=None)
    with admission:
        started = time.monotonic()
        # The requeued attempt is the last one: it is never armed.
        armed = not requeued and endpoint.may_requeue_after_oom(request, borrowed)
        with job_attempt(stop_on_oom=armed) as attempt:
            outcome = endpoint.invoke_endpoint(request, admission=borrowed, started=started)
        outcome.oom_killed = attempt.oom_killed.is_set()
        if armed and _is_requeued(outcome, attempt):
            endpoint.begin_requeued_attempt()
            return None
        if requeued:
            _name_requeue(outcome, started_again=True)
        return _record_and_finish(endpoint, request, outcome, started=started)


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
    first = _attempt_endpoint(endpoint, request, requeued=False)
    if first is not None:
        return first
    # The requeued attempt is never armed, so it always finishes.
    return cast(ExecutionResult, _attempt_endpoint(endpoint, request, requeued=True))
