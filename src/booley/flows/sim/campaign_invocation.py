"""Prepared invocation carrying one Simulation Campaign's preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from booley.flows.endpoint_report_criteria import ReportScope, target_keys
from booley.flows.prepared_invocation import PreparedInvocation
from booley.runtime.endpoint_execution import EndpointOutcome

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState
    from booley.flows.sim.flow import CandidateWorkload, PreparedSimulationEndpoint, SimulateFlow


def _workload_keys(
    endpoint: EndpointState, workload: CandidateWorkload, families: set[str]
) -> set[str]:
    """Planned Criterion keys one prepared candidate workload can judge."""
    standalone = endpoint.state._file_path is None
    keys = target_keys(endpoint, workload.handle, families, {"sim_pass"} if standalone else set())
    return {
        key
        for key in keys
        if (standalone or not key.startswith("sim_pass") or key in workload.eligible)
        and (
            not key.startswith("cycle_count_")
            or endpoint.state.criteria[key].params.get("test") in workload.selected
        )
    }


@dataclass(frozen=True)
class SimulationCampaignInvocation(PreparedInvocation):
    """A Simulation run whose Targets were prepared and authorized before admission."""

    flow: SimulateFlow
    prepared: PreparedSimulationEndpoint

    borrows_admission: ClassVar[bool] = True

    def run(self, admission: object | None) -> EndpointOutcome:
        return self.flow.run_prepared_simulation(self.prepared, admission)

    def report_scope(self, endpoint: EndpointState) -> ReportScope:
        workloads, coverage = self.flow.report_candidate_workloads(self.prepared)
        families = {"sim_pass", "cycle_count"} | ({"coverage"} if coverage else set())
        return ReportScope(
            targets=frozenset(workload.handle.identity for workload in workloads),
            planned=frozenset(
                key
                for workload in workloads
                for key in _workload_keys(endpoint, workload, families)
            ),
        )
