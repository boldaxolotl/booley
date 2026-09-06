"""Transport-independent sequencing for one Flow, Specialist, or MCP endpoint.

The coordinator deliberately knows nothing about argparse, MCP schemas, wire
payloads, or concrete Flow/Specialist implementations.  An adapter supplies the
small lifecycle interface below; this module owns the ordering shared by CLI
and MCP-triggered processes.
"""

from __future__ import annotations

import time
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2


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


Prepared_contra = TypeVar("Prepared_contra", contravariant=True)


class AcceptanceRecorder(Protocol[Prepared_contra]):
    """Record immutable acceptance evidence for one prepared request."""

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

    def admission(self, prepared: Prepared_contra) -> AbstractContextManager[None]:
        """Validate and return the admission lifetime held through finish."""
        ...

    def invoke_endpoint(
        self,
        prepared: Prepared_contra,
        *,
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
    ) -> int:
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
    finally:
        exit_code = endpoint.finish_execution(
            prepared,
            outcome,
            started=started,
            acceptance_recorded=acceptance_recorded,
        )
    return ExecutionResult(exit_code=exit_code, outcome=outcome)


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
        admission.enter_context(endpoint.admission(request))
    except EndpointRejectedError as exc:
        return _record_and_finish(endpoint, request, exc.outcome, started=None)
    with admission:
        started = time.monotonic()
        outcome = endpoint.invoke_endpoint(request, started=started)
        return _record_and_finish(endpoint, request, outcome, started=started)
