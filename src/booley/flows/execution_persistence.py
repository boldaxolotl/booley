"""Seam between deterministic Flow execution and acceptance persistence."""

from __future__ import annotations

import os
from typing import Protocol

from booley.criteria.state import CriterionChange, DevelopmentState
from booley.evidence.acceptance import ResolvedFlowAcceptance
from booley.flows.request import FlowRequest
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome


class AcceptanceRecorder(Protocol):
    """Persist effective Criterion changes for one execution context."""

    def record_changes(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        invocation_id: str,
        producer: str,
        transaction_id: str | None = None,
    ) -> None: ...


class AcceptanceRecordingError(RuntimeError):
    """Durable acceptance evidence could not be recorded."""


class FlowExecutionAdapter(AcceptanceRecorder, Protocol):
    """Resolve admission inputs and persist evidence outside Flow mechanics."""

    def validate_and_resolve(
        self,
        flow_name: str,
        request: FlowRequest,
    ) -> ResolvedFlowAcceptance | EndpointOutcome: ...


class NoAcceptanceRecorder:
    """Recorder for executions that have no durable Ticket state."""

    def record_changes(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        invocation_id: str,
        producer: str,
        transaction_id: str | None = None,
    ) -> None:
        return


class StandaloneFlowExecution(NoAcceptanceRecorder):
    """Standalone adapter: no Board initialization and no persistence."""

    def validate_and_resolve(
        self,
        flow_name: str,
        request: FlowRequest,
    ) -> ResolvedFlowAcceptance | EndpointOutcome:
        if os.environ.get("BOOLEY_TICKET_FILE"):
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=(
                    "BLOCKED: ticket-backed Flow execution requires Ticket Board "
                    "composition; run it through `booley flow` or the MCP endpoint"
                ),
            )
        return ResolvedFlowAcceptance()
