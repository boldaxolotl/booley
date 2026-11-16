"""Prepared invocation carrying one Simulation Record's preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from booley.flows.endpoint_report_criteria import ReportScope
from booley.flows.prepared_invocation import PreparedInvocation
from booley.runtime.endpoint_execution import EndpointOutcome

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState
    from booley.flows.job_estimates import JobRequest
    from booley.flows.sim.flow import PreparedSimulationEndpoint, SimulateFlow


@dataclass(frozen=True)
class SimulationRecordInvocation(PreparedInvocation):
    """A Simulation run whose Targets were prepared and authorized before admission."""

    flow: SimulateFlow
    prepared: PreparedSimulationEndpoint

    borrows_admission: ClassVar[bool] = True

    def run(self, admission: object | None) -> EndpointOutcome:
        return self.flow.run_prepared_simulation(self.prepared, admission)

    def job_requests(self) -> tuple[JobRequest, ...] | None:
        return self.flow.simulation_record_job_requests(self.prepared)

    def report_scope(self, endpoint: EndpointState) -> ReportScope:
        """The Simulation Flow judges no Criterion, so nothing is planned."""
        return ReportScope(targets=frozenset(), planned=frozenset())
