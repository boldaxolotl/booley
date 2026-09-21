"""Criteria reconciliation for authenticated Simulation Campaign outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from booley.criteria.state import CriterionChange, DevelopmentState
from booley.flows.execution_persistence import AcceptanceRecorder
from booley.flows.sim.campaign import CampaignOutcome
from booley.flows.sim.campaign_reports import (
    target_report_directory,
    write_compatibility_projection,
)
from booley.targets.domain import criterion_matches_target

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState


@dataclass(frozen=True, slots=True)
class AcceptanceContext:
    """Ticket-owned inputs deliberately kept outside the campaign module."""

    ticket_slug: str
    ticket_identity: dict[str, Any]
    ticket_generation: str
    state: DevelopmentState
    recorder: AcceptanceRecorder
    invocation_id: str
    diagnostic: bool = False


@dataclass(frozen=True, slots=True)
class AcceptanceOutcome:
    """Reconciliation disposition retained through endpoint completion."""

    committed: bool
    transaction_id: str | None
    changes: tuple[CriterionChange, ...]
    reason: str


class SimulationAcceptanceCoordinator:
    """Derive and transactionally commit Criteria from campaign facts only."""

    def reconcile(
        self,
        outcome: CampaignOutcome,
        context: AcceptanceContext,
    ) -> AcceptanceOutcome:
        if context.diagnostic or context.state._file_path is None:
            return AcceptanceOutcome(False, None, (), "no_criteria")
        changes = self._derive_changes(outcome, context.state)
        if not changes:
            return AcceptanceOutcome(False, None, (), "no_applicable_criteria")
        transaction = context.recorder.record_or_verify_transaction(
            context.state,
            changes,
            acceptance_facts=outcome.acceptance_facts.document,
            ticket_identity=context.ticket_identity,
        )
        transaction_id = getattr(transaction, "transaction_id", None)
        return AcceptanceOutcome(True, transaction_id, tuple(changes), "committed")

    @staticmethod
    def _derive_changes(
        outcome: CampaignOutcome,
        state: DevelopmentState,
    ) -> list[CriterionChange]:
        facts = outcome.acceptance_facts.document
        target = facts["target"]
        if not isinstance(target, Mapping):
            raise TypeError("campaign acceptance Target is not a mapping")
        target_name = str(target["name"])
        shadow = deepcopy(state)
        changes = _cycle_changes(outcome, shadow, target)
        key = f"sim_pass_{target_name}"
        if key not in state.criteria and "sim_pass" in state.criteria:
            key = "sim_pass"
        if key not in state.criteria:
            return changes
        suite = facts["required_suite"]
        observations = facts["observations"]
        if not isinstance(suite, Mapping) or not isinstance(observations, tuple):
            raise TypeError("campaign acceptance suite is malformed")
        observed_names = {
            item["test"]
            for item in observations
            if isinstance(item, Mapping)
        }
        required_names = set(suite["names"])
        has_required_scope = (
            len(observations) == 1 and None in observed_names
            if suite["default_invocation"] is True
            else required_names.issubset(observed_names)
        )
        if not has_required_scope:
            return changes
        met = outcome.acceptance_ready and outcome.aggregate_grade == "pass"
        detail = {
            "campaign_manifest": str(outcome.manifest_path),
            "campaign_summary": str(outcome.summary_path),
            "campaign_id": facts["campaign_id"],
            "manifest_sha256": facts["manifest_sha256"],
            "acceptance_facts_sha256": outcome.acceptance_facts.sha256,
            "aggregate_grade": outcome.aggregate_grade,
        }
        changes.extend(shadow.set_criterion(key, met, detail=detail))
        return changes


def record_campaign_acceptance(
    endpoint: EndpointState,
    outcomes: tuple[object, ...],
) -> None:
    """Reconcile campaign facts before publishing compatibility projections."""
    recorder = endpoint._acceptance_recorder
    identity_loader = getattr(recorder, "_validated_ticket_identity", None)
    ticket_identity = identity_loader() if callable(identity_loader) else {}
    context = AcceptanceContext(
        endpoint.state.slug,
        dict(ticket_identity),
        str(ticket_identity.get("generation", "")),
        endpoint.state,
        recorder,
        endpoint._invocation_id,
        diagnostic=bool(getattr(endpoint.args, "diagnostic", False)),
    )
    reconciler = SimulationAcceptanceCoordinator()
    keys: list[str] = []
    acceptances = []
    for item in outcomes:
        if not isinstance(item, CampaignOutcome):
            raise TypeError("invalid Simulation Campaign outcome")
        accepted = reconciler.reconcile(item, context)
        acceptances.append(accepted)
        keys.extend(change.key for change in accepted.changes)
        complete = accepted.committed or accepted.reason in {
            "no_criteria",
            "no_applicable_criteria",
        }
        projection = _campaign_projection(item)
        origin = item.manifest_path.parents[1] / "simulation.json"
        write_compatibility_projection(origin, projection, acceptance_committed=complete)
        current_invocation = endpoint._reserved_invocation_dir
        if current_invocation is not None:
            current = (
                target_report_directory(current_invocation, str(item.target["selector"]))
                / "simulation.json"
            )
            if current != origin:
                write_compatibility_projection(
                    current, projection, acceptance_committed=complete
                )
    endpoint._simulation_acceptance_outcomes = tuple(acceptances)
    endpoint._pending_criteria_set = tuple(keys)


def _campaign_projection(outcome: CampaignOutcome) -> dict[str, object]:
    tests = [
        {
            "name": observation["test"] or "default",
            "passed": observation["execution"] == "completed"
            and observation["functional"] == "pass"
            and observation["assertions"] != "dirty",
            "cycles": observation["cycle_count"],
            "sva_errors": observation["assertion_count"],
            "timed_out": observation["execution"] == "timeout",
            "error_tail": str(observation["detail"]),
        }
        for observation in outcome.observations
    ]
    return {
        "flow": "sim",
        "mode": "simulate",
        "target": outcome.target["selector"],
        "target_identity": f"{outcome.target['vlnv']}#{outcome.target['name']}",
        "passed": outcome.aggregate_grade == "pass",
        "inconclusive": outcome.aggregate_grade == "inconclusive",
        "tests": tests,
        "campaign_manifest": str(outcome.manifest_path),
        "campaign_summary": str(outcome.summary_path),
    }


def _cycle_changes(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    target: Mapping[str, object],
) -> list[CriterionChange]:
    identity = f"{target['vlnv']}#{target['name']}"
    selector = str(target["selector"])
    observations = {
        item["test"]: item
        for item in outcome.acceptance_facts.document["observations"]  # type: ignore[union-attr]
        if isinstance(item, Mapping) and item["test"] is not None
    }
    baselines = {
        item["cycle_observation"]["test"]: item["cycle_observation"]
        for item in outcome.acceptance_facts.document["prerequisites"]  # type: ignore[union-attr]
        if isinstance(item, Mapping)
        and isinstance(item["cycle_observation"], Mapping)
    }
    changes: list[CriterionChange] = []
    for key, entry in shadow.criteria.items():
        params = entry.params or {}
        if not key.startswith("cycle_count_") or not criterion_matches_target(
            params, identity=identity, selector=selector
        ):
            continue
        test = params.get("test")
        current = observations.get(test)
        baseline = baselines.get(test)
        met = bool(
            current
            and current["execution"] == "completed"
            and current["functional"] == "pass"
            and current["assertions"] != "dirty"
            and current["cycle_count"] is not None
        )
        detail = {
            "mode": "simulate",
            "target": selector,
            "target_identity": identity,
            "test": test,
            "cycles": current["cycle_count"] if current else None,
            "baseline_cycles": baseline["cycle_count"] if baseline else None,
            "cycle_observation": "observed" if met else "missing",
            "baseline_observation": "observed" if baseline else "not_required",
            "evaluation": {
                "cycles": current["cycle_count"] if current else None,
            },
        }
        changes.extend(shadow.set_criterion(key, met, detail=detail))
    return changes


__all__ = [
    "AcceptanceContext",
    "AcceptanceOutcome",
    "SimulationAcceptanceCoordinator",
    "record_campaign_acceptance",
]
