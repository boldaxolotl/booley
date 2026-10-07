"""Seam between deterministic Flow execution and acceptance persistence."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from booley.criteria.state import CriterionChange, DevelopmentState, StatePersistence
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

    def record_or_verify_transaction(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        acceptance_facts: Mapping[str, Any],
        ticket_identity: Mapping[str, Any],
    ) -> object | None: ...

    def acceptance_identity(self) -> Mapping[str, Any] | None:
        """Validated identity evidence is recorded under, or ``None`` without a recorder.

        Raises :class:`AcceptanceRecordingError` when the identity cannot be
        established for an execution that requires one.
        """
        ...


@runtime_checkable
class CriterionSourceTarget(Protocol):
    """Optional recorder policy for the Target a Criterion's source stamp describes."""

    def criterion_source_target(self, key: str, fallback: str | None) -> str | None: ...


def criterion_source_target_for(
    recorder: AcceptanceRecorder, key: str, fallback: str | None
) -> str | None:
    """Use declared acceptance policy when present, otherwise retain producer policy."""
    if isinstance(recorder, CriterionSourceTarget):
        return recorder.criterion_source_target(key, fallback)
    return fallback


@runtime_checkable
class CriterionFreshness(Protocol):
    """Optional acceptance policy for whether recorded evidence is current."""

    def criterion_is_current(self, state: DevelopmentState, key: str) -> bool: ...


def criterion_is_current_for(
    recorder: AcceptanceRecorder, state: DevelopmentState, key: str
) -> bool | None:
    """Read acceptance freshness when supported; None keeps the caller policy."""
    return (
        recorder.criterion_is_current(state, key)
        if isinstance(recorder, CriterionFreshness)
        else None
    )


@runtime_checkable
class StatePersistenceSource(Protocol):
    """Optional recorder capability: choose how the execution's state is saved.

    A recorder that does not implement it leaves its state on the default
    atomic file write, as every recorder does today.
    """

    def state_persistence(self) -> StatePersistence: ...


def state_persistence_for(recorder: AcceptanceRecorder) -> StatePersistence | None:
    """Strategy *recorder* supplies for its state, or ``None`` for the default."""
    if isinstance(recorder, StatePersistenceSource):
        return recorder.state_persistence()
    return None


class AcceptanceRecordingError(RuntimeError):
    """Durable Criterion evidence could not be recorded."""


class EvidenceDiscarded(AcceptanceRecordingError):  # noqa: N818 - names the outcome, as ADR 0067 B1 does
    """An invocation's evidence was discarded before anything was written.

    A recorder raises it when the authority the run was admitted under no
    longer holds (Goal Mode's publication gate). The endpoint completion path
    catches it once per invocation, writes nothing more for that invocation,
    and reports ``evidence discarded: <reason>`` with the run's own result.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"evidence discarded: {reason}")
        self.reason = reason


class FlowExecutionAdapter(AcceptanceRecorder, Protocol):
    """Resolve admission inputs and persist evidence outside Flow mechanics."""

    def validate_and_resolve(
        self,
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

    def record_or_verify_transaction(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        acceptance_facts: Mapping[str, Any],
        ticket_identity: Mapping[str, Any],
    ) -> object | None:
        return None

    def acceptance_identity(self) -> Mapping[str, Any] | None:
        return None


class StandaloneFlowExecution(NoAcceptanceRecorder):
    """Standalone adapter: no Board initialization and no persistence."""

    def validate_and_resolve(
        self,
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
