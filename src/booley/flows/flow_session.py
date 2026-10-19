"""Compose deterministic Flow callbacks with per-invocation execution services."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from booley.evidence.acceptance import ResolvedFlowAcceptance
from booley.flows.endpoint_state import EndpointState
from booley.flows.execution_persistence import FlowExecutionAdapter, StandaloneFlowExecution
from booley.flows.request import FlowRequest
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow
    from booley.flows.terminal_progress import TerminalProgress


class FlowSession(EndpointState):
    """One Flow call's state, admission, acceptance and publication lifetime."""

    def __init__(
        self,
        flow: BuiltinFlow,
        execution_adapter: FlowExecutionAdapter | None = None,
    ) -> None:
        super().__init__()
        execution_adapter = execution_adapter or StandaloneFlowExecution()
        self.terminal_progress: TerminalProgress | None = None
        self.publication_resources = ExitStack()
        self.flow = flow
        self._automatic_target_notice = ""
        self._automatic_target_notice_published = False
        flow._prepared_target_selection = None
        self.configure_flow_execution(execution_adapter)
        self.flow_acceptance = ResolvedFlowAcceptance()
        self.name = flow.name
        self.endpoint_kind = "flow"
        self.satisfies = flow.satisfies
        self.satisfies_args = flow.satisfies_args
        self.code_modifying = flow.code_modifying
        self.modifies_category = flow.modifies_category
        self.config_aware = flow.config_aware
        self.non_persisting_dry_run = flow.non_persisting_dry_run
        self.announce_success_report = flow.announce_success_report
        self._simulation_campaign_outcomes: tuple[object, ...] = ()
        self.cycle_report_json: str | None = None
        flow.begin_invocation()

    def reset_report_metadata(self) -> None:
        """Clear invocation-local Simulation metadata before admission gates."""
        self.cycle_report_json = None
        self._automatic_target_notice = ""
        self._automatic_target_notice_published = False
        self.flow._prepared_target_selection = None
        self.flow.begin_invocation()

    @property
    def args(self) -> FlowRequest:
        return super().args

    def _adapt_outcome(self, outcome: EndpointOutcome) -> EndpointOutcome:
        outcome.cycle_report_json = self.cycle_report_json or "{}"
        return outcome

    def _run(self) -> EndpointOutcome:
        return self.flow._run()

    def _pre_state_gate(self) -> EndpointOutcome | None:
        return self.flow._pre_state_gate()

    def prepare_target_endpoint(self) -> EndpointOutcome | None:
        return self.flow.prepare_target_endpoint()

    def _resolve_job_class(self) -> str | None:
        return self.flow._resolve_job_class()

    def _resolve_display_label(self) -> str | None:
        return self.flow._resolve_display_label()

    def _next_invocation_dir(self, report_dir: Path) -> Path:
        """Let Simulation lock a number before its generic report directory appears."""
        if self.name == "sim":
            return self.flow._next_invocation_dir(report_dir)
        return super()._next_invocation_dir(report_dir)

    def execute_prepared(self) -> ExecutionResult:
        """Keep invocation resources alive through final report publication."""
        with self.publication_resources:
            return super().execute_prepared()
