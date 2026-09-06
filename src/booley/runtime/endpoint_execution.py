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


Prepared = TypeVar("Prepared")


class ExecutableEndpoint(Protocol[Prepared]):
    """Internal adapter interface consumed by :func:`execute_endpoint`.

    The interface describes lifecycle stages, not the implementation helpers
    historically inherited by endpoint classes.  Transport adapters may parse
    different request representations while sharing the same execution order.
    """

    def prepare_execution(self, argv: list[str] | None) -> Prepared | EndpointOutcome:
        """Normalize the request and perform pre-state preparation."""

    def reject_execution(self, prepared: Prepared) -> EndpointOutcome | None:
        """Return a pre-admission rejection, if any."""

    def admission(self, prepared: Prepared) -> AbstractContextManager[None]:
        """Return the admission lifetime held around invocation and finish."""

    def begin_invocation(self, prepared: Prepared) -> None:
        """Capture state that must precede the execution clock."""

    def invoke_endpoint(self, prepared: Prepared, *, started: float) -> EndpointOutcome:
        """Run the endpoint implementation and finalize its raw outcome."""

    def invocation_failed(self, exc: Exception) -> EndpointOutcome:
        """Normalize an unexpected endpoint exception."""

    def finish_execution(
        self,
        prepared: Prepared,
        outcome: EndpointOutcome,
        *,
        started: float | None,
    ) -> int:
        """Persist and publish the final outcome, returning its process code."""


def _finish(
    endpoint: ExecutableEndpoint[Prepared],
    prepared: Prepared,
    outcome: EndpointOutcome,
    *,
    started: float | None,
) -> ExecutionResult:
    exit_code = endpoint.finish_execution(prepared, outcome, started=started)
    return ExecutionResult(exit_code=exit_code, outcome=outcome)


def execute_endpoint(
    endpoint: ExecutableEndpoint[Prepared],
    argv: list[str] | None = None,
) -> ExecutionResult:
    """Execute one endpoint with invariant lifecycle ordering.

    Preparation rejection happens before admission.  Once admitted, the claim
    remains held through final persistence/reporting and is released by the
    supplied context manager on every exit path.
    """

    prepared = endpoint.prepare_execution(argv)
    if isinstance(prepared, EndpointOutcome):
        return ExecutionResult(exit_code=prepared.exit_code, outcome=prepared)

    rejection = endpoint.reject_execution(prepared)
    if rejection is not None:
        return _finish(endpoint, prepared, rejection, started=None)

    admission = ExitStack()
    try:
        admission.enter_context(endpoint.admission(prepared))
    except EndpointRejectedError as exc:
        return _finish(endpoint, prepared, exc.outcome, started=None)
    with admission:
        endpoint.begin_invocation(prepared)
        started = time.monotonic()
        try:
            outcome = endpoint.invoke_endpoint(prepared, started=started)
        except Exception as exc:  # noqa: BLE001 - endpoint failures normalize to exit 2
            outcome = endpoint.invocation_failed(exc)
        return _finish(endpoint, prepared, outcome, started=started)
