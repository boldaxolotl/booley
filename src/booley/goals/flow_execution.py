"""The Goal counterpart of the Ticket Flow execution adapter (ADR 0067 D5, B8).

:class:`GoalFlowExecution` is the
:class:`~booley.flows.execution_persistence.FlowExecutionAdapter` of one
bound Goal run: it admits a Flow request into the bound worktree, points it at
the record's ``booley_state.json``, and records evidence through
:class:`~booley.goals.recorder.GoalEvidenceRecorder`, whose state persistence
it also supplies. Custom MCP tools and Specialists receive the same adapter as
their recorder, so every endpoint of a Goal run publishes through one gate.

It imports no concrete Flow, MCP, harness, Specialist, or Ticket Board code
(contract rule D35); the MCP entry point composes it with a runner.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from booley.criteria.evidence_ledger import AcceptanceTransaction
from booley.criteria.state import CriterionChange, DevelopmentState, StatePersistence
from booley.evidence.acceptance import PairedProjectBaseline, ResolvedFlowAcceptance
from booley.flows.request import FlowRequest
from booley.goals.binding import GoalRunBinding
from booley.goals.checkout import CheckoutError, GoalCheckout
from booley.goals.freshness import DEFAULT_RESOLVERS, GoalFreshnessResolvers
from booley.goals.paths import record_paths
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.store import GoalStore, GoalStoreError
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome


class GoalFlowExecution:
    """Admit, record, and persist one bound Goal run (module docstring)."""

    def __init__(
        self,
        binding: GoalRunBinding,
        *,
        resolvers: GoalFreshnessResolvers = DEFAULT_RESOLVERS,
        publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._binding = binding
        self._recorder = GoalEvidenceRecorder(
            binding, resolvers=resolvers, publication_checkpoint=publication_checkpoint
        )

    @property
    def binding(self) -> GoalRunBinding:
        """The run this adapter executes for."""
        return self._binding

    @property
    def state_file(self) -> Path:
        """The bound record's ``booley_state.json``."""
        return record_paths(self._binding.project_dir, self._binding.record_id).state_file

    def validate_and_resolve(
        self, request: FlowRequest
    ) -> ResolvedFlowAcceptance | EndpointOutcome:
        """Admit *request* into the bound worktree and point it at the Goal state."""
        try:
            found = GoalCheckout(request.work_dir).containing_repository()
            record = GoalStore(self._binding.project_dir).load(self._binding.record_id)
        except (CheckoutError, GoalStoreError) as exc:
            return _blocked(str(exc))
        root = None if found is None else found[0].resolve()
        if root != self._binding.worktree_root.resolve():
            return _blocked(
                f"{request.work_dir} is not the Goal worktree {self._binding.worktree_root}"
            )
        request.slug = self._binding.record_id
        request.state_file = self.state_file
        paired = (
            PairedProjectBaseline.absent()
            if record.paired_project_base_sha is None
            else PairedProjectBaseline.entry_pinned(record.paired_project_base_sha)
        )
        return ResolvedFlowAcceptance(paired_project=paired)

    def state_persistence(self) -> StatePersistence:
        """The gated, merging persistence of the run's state."""
        return self._recorder.state_persistence()

    def acceptance_identity(self) -> dict[str, Any]:
        """The identity of every Goal the run was admitted with."""
        return self._recorder.acceptance_identity()

    def record_changes(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        invocation_id: str,
        producer: str,
        transaction_id: str | None = None,
    ) -> None:
        """Append Goal evidence (V1) through the publication gate."""
        self._recorder.record_changes(
            state,
            changes,
            invocation_id=invocation_id,
            producer=producer,
            transaction_id=transaction_id,
        )

    def record_or_verify_transaction(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        acceptance_facts: Mapping[str, Any],
        ticket_identity: Mapping[str, Any],
    ) -> AcceptanceTransaction:
        """Record or recover a Simulation Campaign transaction (V2) as Goal evidence."""
        return self._recorder.record_or_verify_transaction(
            state, changes, acceptance_facts=acceptance_facts, ticket_identity=ticket_identity
        )


def _blocked(reason: str) -> EndpointOutcome:
    return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"BLOCKED: {reason}")
