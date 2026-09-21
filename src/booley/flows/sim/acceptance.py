"""Criteria reconciliation for authenticated Simulation Campaign outcomes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from booley.criteria.state import CriterionChange, DevelopmentState
from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.criteria.thresholds import (
    CYCLE_COUNT_PARAMS,
    evaluate_cycle_threshold,
    has_relative_threshold,
)
from booley.evidence.fields import BASELINE_REF_PARAM
from booley.flows.execution_persistence import (
    AcceptanceRecorder,
    AcceptanceRecordingError,
    NoAcceptanceRecorder,
)
from booley.flows.sim.campaign import CampaignOutcome
from booley.flows.sim.campaign_reports import (
    target_report_directory,
    write_compatibility_projection,
)
from booley.flows.sim.coverage_reference import (
    ResolvedCoverageCampaign,
    authenticate_coverage_campaign_owner,
    encode_coverage_campaign_reference,
    resolve_coverage_campaign_reference,
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
        if transaction is None and isinstance(context.recorder, NoAcceptanceRecorder):
            return AcceptanceOutcome(False, None, tuple(changes), "no_recorder")
        if transaction is None:
            raise AcceptanceRecordingError(
                "campaign Criteria require a durable acceptance transaction store"
            )
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
        changes.extend(_coverage_changes(outcome, shadow, target))
        key = f"sim_pass_{target_name}"
        if key not in state.criteria and "sim_pass" in state.criteria:
            key = "sim_pass"
        if key not in state.criteria:
            return changes
        suite = facts["required_suite"]
        observations = facts["observations"]
        if not isinstance(suite, Mapping) or not isinstance(observations, tuple):
            raise TypeError("campaign acceptance suite is malformed")
        observed_names = {item["test"] for item in observations if isinstance(item, Mapping)}
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
                write_compatibility_projection(current, projection, acceptance_committed=complete)
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
    projection = {
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
    if outcome.coverage_reference is not None:
        public = outcome.manifest_path.parents[1] / "coverage.json"
        coverage = _resolve_facts_coverage(outcome, public).loaded.campaign
        projection.update(
            coverage_campaign="coverage.json",
            coverage_campaign_base="origin_target",
            collection=coverage.collection["status"],
            evaluation=coverage.evaluation["status"],
        )
    return projection


def _coverage_changes(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    target: Mapping[str, object],
) -> list[CriterionChange]:
    """Bind nested Coverage Campaign evaluation into the joint transaction."""
    if outcome.coverage_reference is None:
        return []
    public_path = outcome.manifest_path.parents[1] / "coverage.json"
    resolved = _resolve_facts_coverage(outcome, public_path)
    campaign = resolved.loaded.campaign
    name = str(target["name"])
    identity = f"{target['vlnv']}#{name}"
    selector = str(target["selector"])
    direct = f"coverage_{name}"
    keys = [
        key
        for key, entry in shadow.criteria.items()
        if key == direct
        or (
            key.startswith("coverage_")
            and criterion_matches_target(
                entry.params or {}, identity=identity, selector=selector
            )
        )
    ]
    if not keys:
        return []
    detail = _coverage_detail(outcome, resolved, public_path)
    changes: list[CriterionChange] = []
    for key in keys:
        changes.extend(
            shadow.set_criterion(
                key,
                campaign.evaluation["status"] == "pass",
                detail=detail,
            )
        )
    return changes


def _resolve_facts_coverage(
    outcome: CampaignOutcome, public_path: Path
) -> ResolvedCoverageCampaign:
    facts = outcome.acceptance_facts.document
    bound = facts["coverage_reference"]
    if not isinstance(bound, Mapping) or not isinstance(outcome.coverage_reference, Mapping):
        raise AcceptanceRecordingError("coverage acceptance facts omit their public reference")
    document = bound["document"]
    evidence = bound["reference"]
    if not isinstance(document, Mapping) or not isinstance(evidence, Mapping):
        raise AcceptanceRecordingError("coverage acceptance reference is malformed")
    resolved = resolve_coverage_campaign_reference(public_path)
    raw = encode_coverage_campaign_reference(resolved.reference)
    invocation = outcome.manifest_path.parents[3]
    expected_path = public_path.relative_to(invocation).as_posix()
    if (
        resolved.reference.document != document
        or outcome.coverage_reference != document
        or evidence["path"] != expected_path
        or evidence["bytes"] != len(raw)
        or evidence["sha256"] != "sha256:" + hashlib.sha256(raw).hexdigest()
        or evidence["owner"] != facts["campaign_id"]
    ):
        raise AcceptanceRecordingError(
            "public Coverage Campaign reference disagrees with Acceptance Facts"
        )
    authenticate_coverage_campaign_owner(resolved)
    return resolved


def _coverage_detail(
    outcome: CampaignOutcome,
    resolved: ResolvedCoverageCampaign,
    public_path: Path,
) -> dict[str, object]:
    campaign = resolved.loaded.campaign
    raw = encode_coverage_campaign_reference(resolved.reference)
    nested = outcome.coverage_reference["coverage_campaign"]
    assert isinstance(nested, Mapping)
    report_root = outcome.manifest_path.parents[5]
    relative_public_path = public_path.relative_to(report_root).as_posix()
    return {
        "coverage_campaign": relative_public_path,
        "coverage_campaign_reference": {
            "path": relative_public_path,
            "bytes": len(raw),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "nested_campaign_sha256": nested["sha256"],
        },
        "evaluation": _plain_json(campaign.evaluation),
        "criterion_fingerprint": campaign.evaluation.get("criterion_fingerprint"),
    }


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain_json(item) for item in value]
    return value


def _cycle_changes(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    target: Mapping[str, object],
) -> list[CriterionChange]:
    identity = f"{target['vlnv']}#{target['name']}"
    selector = str(target["selector"])
    facts = outcome.acceptance_facts.document
    observations = _candidate_cycle_observations(facts, target)
    prerequisites = tuple(
        item
        for item in facts["prerequisites"]  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    )
    changes: list[CriterionChange] = []
    for key, entry in shadow.criteria.items():
        params = entry.params or {}
        if not key.startswith("cycle_count_") or not criterion_matches_target(
            params, identity=identity, selector=selector
        ):
            continue
        test = params.get("test")
        current = observations.get(test)
        baseline, baseline_state = _bound_baseline(prerequisites, params, test)
        current_cycles = _healthy_cycles(current)
        baseline_cycles = baseline["cycle_count"] if baseline is not None else None
        checks = _cycle_threshold_checks(params, current_cycles, baseline_cycles)
        met = current_cycles is not None and all(check["pass"] for check in checks)
        detail = _cycle_detail(
            selector,
            identity,
            test,
            current_cycles,
            baseline_cycles,
            baseline_state,
            checks,
        )
        changes.extend(shadow.set_criterion(key, met, detail=detail))
    return changes


def _candidate_cycle_observations(
    facts: Mapping[str, object], target: Mapping[str, object]
) -> dict[object, Mapping[str, object]]:
    return {
        item["test"]: item
        for item in facts["observations"]  # type: ignore[union-attr]
        if isinstance(item, Mapping)
        and item["test"] is not None
        and item["role"] == "candidate"
        and item["revision"] == target["revision"]
        and item["target"] == target
    }


def _healthy_cycles(observation: Mapping[str, object] | None) -> int | None:
    if not observation:
        return None
    cycles = observation["cycle_count"]
    healthy = (
        observation["execution"] == "completed"
        and observation["functional"] == "pass"
        and observation["assertions"] == "clean"
        and type(cycles) is int
    )
    return cast(int, cycles) if healthy else None


def _bound_baseline(
    prerequisites: tuple[Mapping[str, object], ...],
    params: Mapping[str, object],
    test: object,
) -> tuple[Mapping[str, object] | None, str]:
    if not has_relative_threshold(dict(params)):
        return None, "not_required"
    selector = params.get(BASELINE_TARGET_PARAM, params.get("_target_selector"))
    revision = params.get(BASELINE_REF_PARAM)
    for item in prerequisites:
        target = item.get("target")
        cycle = item.get("cycle_observation")
        if not isinstance(target, Mapping) or not isinstance(cycle, Mapping):
            continue
        manifest = item.get("manifest")
        result = item.get("result")
        bound = (
            item.get("role") == "cycle_count_baseline"
            and target.get("role") == "cycle_count_baseline"
            and target.get("selector") == selector
            and (revision is None or target.get("revision") == revision)
            and cycle.get("test") == test
            and isinstance(manifest, Mapping)
            and manifest.get("owner") == item.get("campaign_id")
            and isinstance(result, Mapping)
            and isinstance(item.get("work_item_id"), str)
        )
        if bound:
            return cycle, "observed"
    return None, "mismatched"


def _cycle_threshold_checks(
    params: Mapping[str, object], current: int | None, baseline: object
) -> list[dict[str, Any]]:
    return [
        evaluate_cycle_threshold(
            name,
            cast(int | float, params[name]),
            current=current,
            baseline=cast(int | None, baseline),
        )
        for name in sorted(CYCLE_COUNT_PARAMS)
        if name in params
    ]


def _cycle_detail(
    selector: str,
    identity: str,
    test: object,
    current: int | None,
    baseline: object,
    baseline_state: str,
    checks: list[dict[str, Any]],
) -> dict[str, object]:
    return {
        "mode": "simulate",
        "target": selector,
        "target_identity": identity,
        "test": test,
        "cycles": current,
        "baseline_cycles": baseline,
        "cycle_observation": "observed" if current is not None else "missing",
        "baseline_observation": baseline_state,
        "evaluation": {"cycles": current, "checks": checks},
    }


__all__ = [
    "AcceptanceContext",
    "AcceptanceOutcome",
    "SimulationAcceptanceCoordinator",
    "record_campaign_acceptance",
]
