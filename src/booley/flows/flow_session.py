"""Compose deterministic Flow callbacks with per-invocation execution services."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING

from booley.evidence.acceptance import ResolvedFlowAcceptance
from booley.flows.endpoint_state import EndpointState
from booley.flows.execution_persistence import FlowExecutionAdapter, StandaloneFlowExecution
from booley.flows.request import FlowRequest
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow


class FlowSession(EndpointState):
    """One Flow call's state, admission, acceptance and publication lifetime."""

    def __init__(
        self,
        flow: BuiltinFlow,
        execution_adapter: FlowExecutionAdapter | None = None,
    ) -> None:
        super().__init__()
        execution_adapter = execution_adapter or StandaloneFlowExecution()
        self.publication_resources = ExitStack()
        self.flow = flow
        self.configure_flow_execution(execution_adapter)
        self.flow_acceptance = ResolvedFlowAcceptance()
        self.name = flow.name
        self.endpoint_kind = "flow"
        self.satisfies = flow.satisfies
        self.code_modifying = flow.code_modifying
        self.modifies_category = flow.modifies_category
        self.config_aware = flow.config_aware
        self.non_persisting_dry_run = flow.non_persisting_dry_run
        self.announce_success_report = flow.announce_success_report

    @property
    def args(self) -> FlowRequest:
        return super().args

    def _run(self) -> EndpointOutcome:
        return self.flow._run()

    def _pre_state_gate(self) -> EndpointOutcome | None:
        return self.flow._pre_state_gate()

    def _resolve_job_class(self) -> str | None:
        return self.flow._resolve_job_class()

    def _resolve_display_label(self) -> str | None:
        return self.flow._resolve_display_label()

    def execute_prepared(self) -> ExecutionResult:
        """Keep invocation resources alive through final report publication."""
        with self.publication_resources:
            return super().execute_prepared()
