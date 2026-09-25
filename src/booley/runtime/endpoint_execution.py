"""Transport-independent sequencing for one Flow, Specialist, or MCP endpoint.

The coordinator deliberately knows nothing about argparse, MCP schemas, wire
payloads, or concrete Flow/Specialist implementations.  An adapter supplies the
small lifecycle interface below; this module owns the ordering shared by CLI
and MCP-triggered processes.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2
EXIT_CANCELLED = 130

logger = logging.getLogger(__name__)


@dataclass
class EndpointOutcome:
    """Verdict and evidence returned by an endpoint implementation."""

    exit_code: int = EXIT_SUCCESS
    criterion_key: str = ""
    criterion_met: bool = False
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
    completion_error = {
        "operation": operation,
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if error_path is not None:
        completion_error["path"] = os.fspath(error_path)
    outcome.exit_code = EXIT_ERROR
    outcome.criterion_key = ""
    outcome.criterion_met = False
    outcome.detail = dict(outcome.detail)
    outcome.detail["completion_error"] = completion_error
    diagnosis = f"Completion failure ({operation}): {type(exc).__name__}: {exc}"
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

    def admission(self, prepared: Prepared_contra) -> AbstractContextManager[object | None]:
        """Validate and yield the admission value held through finish."""
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


def execute_endpoint(
    endpoint: ExecutableEndpoint[Prepared_contra],
    request: Prepared_contra,
) -> ExecutionResult:
    """Execute one endpoint with invariant lifecycle ordering.

    The transport adapter prepares *request* before calling this interface.
    Admission includes pre-execution validation. Once admitted, the claim stays
    held through acceptance recording and mutable persistence/reporting.
    """

    admission = ExitStack()
    try:
        borrowed = admission.enter_context(endpoint.admission(request))
    except EndpointRejectedError as exc:
        return _record_and_finish(endpoint, request, exc.outcome, started=None)
    with admission:
        started = time.monotonic()
        outcome = endpoint.invoke_endpoint(request, admission=borrowed, started=started)
        return _record_and_finish(endpoint, request, outcome, started=started)
