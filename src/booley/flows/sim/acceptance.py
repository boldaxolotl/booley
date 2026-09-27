"""Criteria reconciliation for authenticated Simulation Campaign outcomes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from booley.criteria.simulation import (
    SimulationCriterionContract,
    resolve_simulation_criterion_contract,
)
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
from booley.flows.sim.campaign import (
    SIMULATION_PROJECTION_SCHEMA,
    CampaignOutcome,
    build_artifact_reference,
    encode_artifact_reference,
)
from booley.flows.sim.campaign.codec import MANIFEST_MAX_BYTES
from booley.flows.sim.campaign_reports import (
    write_compatibility_projection,
)
from booley.flows.sim.coverage_projection import project_coverage_criterion
from booley.flows.sim.coverage_reference import (
    MAX_REFERENCE_BYTES,
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
    detail_stamper: Callable[[CriterionChange, str], dict[str, Any]] | None = None


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
        if context.detail_stamper is not None:
            selector = str(outcome.target["selector"])
            changes = [
                replace(change, detail=context.detail_stamper(change, selector))
                for change in changes
            ]
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
        changes.extend(_simulation_changes(outcome, shadow, target, target_name))
        return changes


def _simulation_changes(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    target: Mapping[str, object],
    target_name: str,
) -> list[CriterionChange]:
    if not outcome.complete or target["role"] != "candidate":
        return []
    keys = _matching_simulation_keys(shadow, target, target_name)
    if not keys:
        return []
    facts = outcome.acceptance_facts.document
    suite = facts["required_suite"]
    if not isinstance(suite, Mapping):
        raise TypeError("campaign acceptance suite is malformed")
    observations = _candidate_simulation_observations(facts, target)
    selected = {_test_name(name): item for name, item in observations.items()}
    registered = set(cast(tuple[str, ...], suite["names"]))
    changes: list[CriterionChange] = []
    for key in keys:
        entry = shadow.criteria[key]
        contract = resolve_simulation_criterion_contract(entry.params or {}, registered, selected)
        if contract.selector == "all":
            required = {"default"} if suite["default_invocation"] is True else registered
            contract = SimulationCriterionContract(
                contract.selector,
                frozenset(required),
                contract.minimum_total,
            )
        relevant = _criterion_observations(contract, suite, observations, selected)
        if relevant is None:
            continue
        met = _simulation_criterion_met(outcome, contract, relevant)
        detail = _simulation_detail(outcome, contract, registered, relevant)
        changes.extend(shadow.set_criterion(key, met, detail=detail))
    return changes


def _matching_simulation_keys(
    state: DevelopmentState,
    target: Mapping[str, object],
    target_name: str,
) -> list[str]:
    identity = f"{target['vlnv']}#{target_name}"
    selector = str(target["selector"])
    return [
        key
        for key, entry in state.criteria.items()
        if key in {"sim_pass", f"sim_pass_{target_name}"}
        or (
            key.startswith("sim_pass_")
            and criterion_matches_target(entry.params or {}, identity=identity, selector=selector)
        )
    ]


def _candidate_simulation_observations(
    facts: Mapping[str, object], target: Mapping[str, object]
) -> dict[object, Mapping[str, object]]:
    indexed: dict[object, Mapping[str, object]] = {}
    for item in facts["observations"]:  # type: ignore[union-attr]
        if not isinstance(item, Mapping):
            continue
        if (
            item["role"] != "candidate"
            or item["revision"] != target["revision"]
            or item["target"] != target
        ):
            continue
        test = item["test"]
        assert test not in indexed, "validated acceptance facts repeat an observation test"
        indexed[test] = item
    return indexed


def _criterion_observations(
    contract: SimulationCriterionContract,
    suite: Mapping[str, object],
    observations: Mapping[object, Mapping[str, object]],
    selected: Mapping[str, Mapping[str, object]],
) -> tuple[Mapping[str, object], ...] | None:
    if (
        contract.selector == "all"
        and suite["default_invocation"] is True
        and (len(observations) != 1 or None not in observations)
    ):
        return None
    if not contract.required_tests.issubset(selected):
        return None
    if contract.selector == "all":
        return tuple(selected[name] for name in sorted(selected))
    return tuple(selected[name] for name in sorted(contract.required_tests))


def _observation_passed(observation: Mapping[str, object]) -> bool:
    return (
        observation["execution"] == "completed"
        and observation["functional"] == "pass"
        and observation["assertions"] != "dirty"
    )


def _simulation_criterion_met(
    outcome: CampaignOutcome,
    contract: SimulationCriterionContract,
    observations: tuple[Mapping[str, object], ...],
) -> bool:
    minimum_met = (
        contract.minimum_total is not None and len(observations) >= contract.minimum_total
    )
    if contract.selector == "all":
        return minimum_met and outcome.acceptance_ready and outcome.aggregate_grade == "pass"
    return minimum_met and all(_observation_passed(item) for item in observations)


def _test_name(value: object) -> str:
    return "default" if value is None else str(value)


def _simulation_detail(
    outcome: CampaignOutcome,
    contract: SimulationCriterionContract,
    registered: set[str],
    observations: tuple[Mapping[str, object], ...],
) -> dict[str, object]:
    facts = outcome.acceptance_facts.document
    selected = [_test_name(item["test"]) for item in observations]
    passed = [_test_name(item["test"]) for item in observations if _observation_passed(item)]
    return {
        "campaign_id": facts["campaign_id"],
        "manifest_sha256": facts["manifest_sha256"],
        "acceptance_facts_sha256": outcome.acceptance_facts.sha256,
        "aggregate_grade": outcome.aggregate_grade,
        "tests_passed": len(passed),
        "tests_total": len(selected),
        "test_selector": contract.selector,
        "required_tests": sorted(contract.required_tests),
        "registry_tests": sorted(registered),
        "selected_tests": selected,
        "passed_tests": passed,
        "failed_tests": [name for name in selected if name not in passed],
        "skipped_tests": [],
    }


def record_campaign_acceptance(
    endpoint: EndpointState,
    outcomes: tuple[object, ...],
) -> None:
    """Reconcile campaign facts before publishing compatibility projections."""
    work_dir = getattr(endpoint.args, "work_dir", None)
    if endpoint.state._file_path is not None and work_dir is not None:
        endpoint.state.work_dir = str(Path(work_dir).resolve())
    recorder = endpoint._acceptance_recorder
    identity_loader = getattr(recorder, "_validated_ticket_identity", None)
    ticket_identity = identity_loader() if callable(identity_loader) else {}
    stamp = getattr(endpoint, "_stamp_source_fingerprint", None)
    context = AcceptanceContext(
        endpoint.state.slug,
        dict(ticket_identity),
        str(ticket_identity.get("generation", "")),
        endpoint.state,
        recorder,
        endpoint._invocation_id,
        diagnostic=bool(getattr(endpoint.args, "diagnostic", False)),
        detail_stamper=(
            lambda change, selector: (
                stamp(
                    change.key,
                    change.met,
                    change.detail,
                    source_target=selector,
                )
                or change.detail
            )
        )
        if callable(stamp)
        else None,
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
    endpoint._simulation_acceptance_outcomes = tuple(acceptances)
    endpoint._pending_criteria_set = tuple(keys)


def _campaign_projection(outcome: CampaignOutcome) -> dict[str, object]:
    facts = outcome.acceptance_facts.document
    campaign_id = str(facts["campaign_id"])
    target_directory = outcome.manifest_path.parents[1]
    projection = {
        "$schema": SIMULATION_PROJECTION_SCHEMA,
        "flow": "sim",
        "mode": "simulate",
        "target": outcome.target["selector"],
        "target_identity": f"{outcome.target['vlnv']}#{outcome.target['name']}",
        "passed": outcome.aggregate_grade == "pass",
        "inconclusive": outcome.aggregate_grade == "inconclusive",
        "tests": _campaign_test_projections(outcome),
        "campaign_id": campaign_id,
        "campaign_manifest": _artifact_document(
            outcome.manifest_path,
            base=target_directory,
            kind="simulation_campaign_manifest",
            owner=campaign_id,
            maximum=MANIFEST_MAX_BYTES,
        ),
    }
    if outcome.coverage_reference is not None:
        projection.update(_coverage_projection_fields(outcome, target_directory, campaign_id))
    return projection


def _campaign_test_projections(outcome: CampaignOutcome) -> list[dict[str, object]]:
    return [
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


def _coverage_projection_fields(
    outcome: CampaignOutcome, target_directory: Path, campaign_id: str
) -> dict[str, object]:
    public = target_directory / "coverage.json"
    coverage = _resolve_facts_coverage(outcome, public).loaded.campaign
    return {
        "coverage_campaign": _artifact_document(
            public,
            base=target_directory,
            kind="coverage_campaign_reference",
            owner=campaign_id,
            maximum=MAX_REFERENCE_BYTES,
        ),
        "collection": coverage.collection["status"],
        "evaluation": coverage.evaluation["status"],
    }


def _artifact_document(
    path: Path, *, base: Path, kind: str, owner: str, maximum: int
) -> dict[str, object]:
    reference = build_artifact_reference(
        path,
        base_name="origin_target",
        base=base,
        kind=kind,
        owner=owner,
        maximum=maximum,
    )
    return encode_artifact_reference(reference)


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
            and criterion_matches_target(entry.params or {}, identity=identity, selector=selector)
        )
    ]
    if not keys:
        return []
    detail = _coverage_detail(outcome, resolved, public_path)
    evaluation = campaign.evaluation
    changes: list[CriterionChange] = []
    for key in keys:
        met, projected_detail = project_coverage_criterion(
            shadow.criteria[key],
            evaluation,
            detail,
            atomic=key != direct,
        )
        changes.extend(
            shadow.set_criterion(
                key,
                met,
                detail=projected_detail,
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
        "coverage_campaign_reference": {
            "path_base": "reports_root",
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
