"""Criteria reconciliation for authenticated Simulation Campaign outcomes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
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
    frozen_test_runs_for,
)
from booley.flows.frozen_test_runs import FrozenTestRuns, GoalTestRuns
from booley.flows.sim.campaign import (
    SIMULATION_PROJECTION_SCHEMA,
    CampaignOutcome,
    build_artifact_reference,
    encode_artifact_reference,
)
from booley.flows.sim.campaign.codec import MANIFEST_MAX_BYTES, SimulationCampaignIntegrityError
from booley.flows.sim.campaign.test_run_report import evidence_test_run_rows
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
from booley.flows.sim.test_run_invocation import baseline_refusal_key
from booley.flows.test_runs import UNNAMED_TEST_DISPLAY
from booley.targets.domain import criterion_matches_target

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState


@dataclass(frozen=True, slots=True)
class AcceptanceContext:
    """Execution-owned inputs kept outside the Campaign module."""

    ticket_slug: str
    identity: dict[str, Any]
    ticket_generation: str
    state: DevelopmentState
    recorder: AcceptanceRecorder
    invocation_id: str
    diagnostic: bool = False
    #: Why a Cycle Count baseline Test Run was not run, by (baseline Target
    #: selector, Test). The verdict comes from the facts; this names its reason.
    baseline_refusals: Mapping[tuple[str, str], str] = field(default_factory=dict)
    observer: Callable[[list[CriterionChange]], None] | None = None
    detail_stamper: Callable[[CriterionChange, str], dict[str, Any]] | None = None
    #: The Test Runs the run's Goals are frozen to; ``None`` outside Goal Mode.
    frozen: FrozenTestRuns | None = None


@dataclass(frozen=True, slots=True)
class AcceptanceOutcome:
    """Reconciliation disposition retained through endpoint completion."""

    committed: bool
    transaction_id: str | None
    changes: tuple[CriterionChange, ...]
    reason: str


class _TestRunRows:
    """The authenticated Test Run rows of one outcome, read on first use."""

    def __init__(self, outcome: CampaignOutcome) -> None:
        self._outcome = outcome
        self._rows: dict[object, dict[str, object]] | None = None

    def of(self, observations: Iterable[Mapping[str, object] | None]) -> list[dict[str, object]]:
        """The row of each of *observations* that names a Test Run, in order."""
        if self._rows is None:
            rows = evidence_test_run_rows(self._outcome)
            self._rows = {row["name"]: row for row in rows}
        named = (item.get("test_run") for item in observations if item is not None)
        return [dict(self._rows[name]) for name in named if name in self._rows]

    def all(self) -> list[dict[str, object]]:
        """Every Test Run row of the outcome."""
        self.of(())
        assert self._rows is not None
        return [dict(row) for row in self._rows.values()]


class SimulationAcceptanceCoordinator:
    """Derive and transactionally commit Criteria from campaign facts only."""

    def reconcile(
        self,
        outcome: CampaignOutcome,
        context: AcceptanceContext,
    ) -> AcceptanceOutcome:
        if context.diagnostic or context.state._file_path is None:
            return AcceptanceOutcome(False, None, (), "no_criteria")
        changes = self._derive_changes(
            outcome, context.state, context.baseline_refusals, context.frozen
        )
        if context.detail_stamper is not None:
            selector = str(outcome.target["selector"])
            changes = [
                replace(change, detail=context.detail_stamper(change, selector))
                for change in changes
            ]
        if not changes:
            return AcceptanceOutcome(False, None, (), "no_applicable_criteria")
        if context.observer is not None:
            context.observer(changes)
        transaction = context.recorder.record_or_verify_transaction(
            context.state,
            changes,
            acceptance_facts=outcome.acceptance_facts.document,
            identity=context.identity,
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
        baseline_refusals: Mapping[tuple[str, str], str] | None = None,
        frozen: FrozenTestRuns | None = None,
    ) -> list[CriterionChange]:
        facts = outcome.acceptance_facts.document
        target = facts["target"]
        if not isinstance(target, Mapping):
            raise TypeError("campaign acceptance Target is not a mapping")
        target_name = str(target["name"])
        shadow = deepcopy(state)
        rows = _TestRunRows(outcome)
        if frozen is not None:
            return _goal_changes(outcome, shadow, target, rows, baseline_refusals or {}, frozen)
        changes = _cycle_changes(outcome, shadow, target, rows, baseline_refusals or {})
        changes.extend(_coverage_changes(outcome, shadow, target, rows))
        changes.extend(_simulation_changes(outcome, shadow, target, target_name, rows))
        return changes


def _simulation_changes(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    target: Mapping[str, object],
    target_name: str,
    rows: _TestRunRows,
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
        if relevant is None or any(_unobserved_infrastructure(item) for item in relevant):
            continue
        met = _simulation_criterion_met(outcome, contract, relevant)
        detail = _simulation_detail(outcome, contract, registered, relevant)
        detail["test_runs"] = rows.of(relevant)
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


def eligible_simulation_keys(
    state: DevelopmentState,
    identity: str,
    selector: str,
    name: str,
    registered: set[str],
    selected: set[str],
) -> set[str]:
    """Return declared sim Criteria whose required tests fit the candidate workload."""
    target = {"vlnv": identity.rsplit("#", 1)[0], "selector": selector}
    keys = _matching_simulation_keys(state, target, name)
    eligible: set[str] = set()
    for key in keys:
        contract = resolve_simulation_criterion_contract(
            state.criteria[key].params or {}, registered, selected
        )
        required = registered if contract.selector == "all" else contract.required_tests
        if required.issubset(selected):
            eligible.add(key)
    return eligible


def _candidate_observations(
    facts: Mapping[str, object], target: Mapping[str, object]
) -> dict[str, Mapping[str, object]]:
    """The candidate revision's observations of *target*, by Test Run name.

    Facts before v2 name Tests only; such an observation is keyed by its Test
    (``<default>`` for the unnamed invocation), which is unique there.
    """
    indexed: dict[str, Mapping[str, object]] = {}
    for item in facts["observations"]:  # type: ignore[union-attr]
        if not isinstance(item, Mapping):
            continue
        if (
            item["role"] != "candidate"
            or item["revision"] != target["revision"]
            or item["target"] != target
        ):
            continue
        name = str(item.get("test_run") or item["test"] or UNNAMED_TEST_DISPLAY)
        if name in indexed:
            raise SimulationCampaignIntegrityError(
                f"validated acceptance facts repeat Test Run {name!r}"
            )
        indexed[name] = item
    return indexed


def _candidate_simulation_observations(
    facts: Mapping[str, object], target: Mapping[str, object]
) -> dict[object, Mapping[str, object]]:
    """Candidate observations by Test, for Criteria that name Tests.

    Outside Goal Mode a Criterion names a Test, so it has one observation to
    judge only when the campaign ran one Test Run of that Test. A Test the
    campaign ran as several Test Runs is left out: no Criterion that needs
    it is judged from this campaign, rather than letting one Test Run stand
    in for the others. Goals name Test Runs and are judged by
    :func:`_goal_changes` instead.
    """
    indexed: dict[object, Mapping[str, object]] = {}
    several: set[object] = set()
    for item in _candidate_observations(facts, target).values():
        test = item["test"]
        if test in indexed:
            several.add(test)
        indexed[test] = item
    return {test: item for test, item in indexed.items() if test not in several}


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


def _unobserved_infrastructure(observation: Mapping[str, object] | None) -> bool:
    return observation is not None and (
        observation.get("failure_class") == "infrastructure"
        and observation.get("functional") == "not_observed"
    )


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


def _detail_stamper(
    endpoint: EndpointState,
) -> Callable[[CriterionChange, str], dict[str, Any]] | None:
    """Stamp a Criterion change's detail with the endpoint's source fingerprint, if it has one."""
    stamp = getattr(endpoint, "_stamp_source_fingerprint", None)
    if not callable(stamp):
        return None
    return lambda change, selector: (
        stamp(change.key, change.met, change.detail, source_target=selector) or change.detail
    )


def record_campaign_acceptance(
    endpoint: EndpointState,
    outcomes: tuple[object, ...],
    *,
    baseline_refusals: Mapping[tuple[str, str], str] | None = None,
) -> None:
    """Reconcile campaign facts before publishing compatibility projections."""
    work_dir = getattr(endpoint.args, "work_dir", None)
    if endpoint.state._file_path is not None and work_dir is not None:
        endpoint.state.work_dir = str(Path(work_dir).resolve())
    recorder = endpoint._acceptance_recorder
    recorded_identity = recorder.acceptance_identity()
    identity: Mapping[str, Any] = recorded_identity if recorded_identity is not None else {}
    context = AcceptanceContext(
        endpoint.state.slug,
        dict(identity),
        str(identity.get("generation", "")),
        endpoint.state,
        recorder,
        endpoint._invocation_id,
        diagnostic=bool(getattr(endpoint.args, "diagnostic", False)),
        baseline_refusals=dict(baseline_refusals or {}),
        observer=endpoint.record_report_criteria,
        detail_stamper=_detail_stamper(endpoint),
        frozen=None if endpoint.state._file_path is None else frozen_test_runs_for(recorder),
    )
    reconciler = SimulationAcceptanceCoordinator()
    keys: list[str] = []
    acceptances = []
    for item in outcomes:
        if not isinstance(item, CampaignOutcome):
            raise TypeError("invalid Simulation Campaign outcome")
        if endpoint.state._file_path is None and not context.diagnostic:
            _record_standalone_campaign(endpoint, item)
        accepted = reconciler.reconcile(item, context)
        acceptances.append(accepted)
        keys.extend(change.key for change in accepted.changes)
        endpoint._simulation_acceptance_outcomes = tuple(acceptances)
        endpoint._pending_criteria_set = tuple(keys)
        complete = accepted.committed or accepted.reason in {
            "no_criteria",
            "no_applicable_criteria",
        }
        projection = _campaign_projection(item)
        origin = item.manifest_path.parents[1] / "simulation.json"
        write_compatibility_projection(origin, projection, acceptance_committed=complete)


def _record_standalone_campaign(endpoint: EndpointState, outcome: CampaignOutcome) -> None:
    """Record a conclusive candidate workload without creating Ticket state."""
    if not outcome.complete or outcome.target["role"] != "candidate":
        return
    if outcome.aggregate_grade not in {"pass", "fail"}:
        return
    facts = outcome.acceptance_facts.document
    observations = _candidate_simulation_observations(facts, outcome.target)
    if not observations or any(_unobserved_infrastructure(item) for item in observations.values()):
        return
    endpoint.set_criterion(
        f"sim_pass_{outcome.target['name']}",
        outcome.aggregate_grade == "pass",
        source_target=str(outcome.target["selector"]),
        detail={
            "mode": "simulate",
            "selected_tests": [_test_name(name) for name in observations],
            "test_runs": _TestRunRows(outcome).of(observations.values()),
        },
    )


def _campaign_projection(outcome: CampaignOutcome) -> dict[str, object]:
    facts = outcome.acceptance_facts.document
    campaign_id = str(facts["campaign_id"])
    target_directory = outcome.manifest_path.parents[1]
    test_runs = [
        {
            key: value
            for key, value in row.items()
            if key not in {"functional_failure", "failure_execution"}
        }
        for row in evidence_test_run_rows(outcome)
    ]
    projection = {
        "$schema": SIMULATION_PROJECTION_SCHEMA,
        "flow": "sim",
        "mode": "simulate",
        "target": outcome.target["selector"],
        "target_identity": f"{outcome.target['vlnv']}#{outcome.target['name']}",
        "passed": outcome.aggregate_grade == "pass",
        "inconclusive": outcome.aggregate_grade == "inconclusive",
        "tests": _campaign_test_projections(outcome, test_runs),
        "test_runs": test_runs,
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


def _campaign_test_projections(
    outcome: CampaignOutcome, test_runs: list[dict[str, object]]
) -> list[dict[str, object]]:
    """One row per observation, with the Test Run it observed when recorded.

    *test_runs* is either empty (facts that name no Test Runs) or aligned
    with the observations.
    """
    runs: list[dict[str, object] | None] = (
        list(test_runs) if test_runs else [None] * len(outcome.observations)
    )
    return [
        {
            "name": observation["test"] or "default",
            **_test_run_fields(run),
            "passed": _observation_passed(observation),
            "cycles": observation["cycle_count"],
            "sva_errors": observation["assertion_count"],
            "timed_out": observation["execution"] == "timeout",
            "execution": observation["execution"],
            "failure_kind": observation["failure_class"] or "",
            "error_tail": _observation_error_tail(observation),
        }
        for observation, run in zip(outcome.observations, runs, strict=True)
    ]


def _test_run_fields(run: Mapping[str, object] | None) -> dict[str, object]:
    """What a per-Test row says about its Test Run (the coverage rows' keys)."""
    if run is None:
        return {}
    return {
        "test_run": run["name"],
        "variant": run["variant"],
        "seed": run["seed"],
        "fingerprint": run["fingerprint"],
    }


def _observation_error_tail(observation: Mapping[str, object]) -> str:
    detail = observation["detail"]
    if not isinstance(detail, Mapping) or not isinstance(detail.get("reason"), str):
        raise SimulationCampaignIntegrityError(
            "Simulation Campaign observation detail must contain a string reason"
        )
    if _observation_passed(observation):
        return ""
    return detail["reason"]


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
    rows: _TestRunRows,
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
    detail["test_runs"] = rows.of(outcome.observations)
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


@dataclass(frozen=True, slots=True)
class _CycleFacts:
    """What a Cycle Count Criterion of one candidate Target is judged from."""

    selector: str
    identity: str
    prerequisites: tuple[Mapping[str, object], ...]
    refusals: Mapping[tuple[str, str], str]

    @classmethod
    def of(
        cls,
        outcome: CampaignOutcome,
        target: Mapping[str, object],
        refusals: Mapping[tuple[str, str], str],
    ) -> _CycleFacts:
        prerequisites = tuple(
            item
            for item in outcome.acceptance_facts.document["prerequisites"]  # type: ignore[union-attr]
            if isinstance(item, Mapping)
        )
        identity = f"{target['vlnv']}#{target['name']}"
        return cls(str(target["selector"]), identity, prerequisites, refusals)


def _cycle_changes(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    target: Mapping[str, object],
    rows: _TestRunRows,
    refusals: Mapping[tuple[str, str], str],
) -> list[CriterionChange]:
    facts = _CycleFacts.of(outcome, target, refusals)
    observations = _candidate_cycle_observations(outcome.acceptance_facts.document, target)
    changes: list[CriterionChange] = []
    for key, entry in shadow.criteria.items():
        params = entry.params or {}
        if not key.startswith("cycle_count_") or not criterion_matches_target(
            params, identity=facts.identity, selector=facts.selector
        ):
            continue
        current = observations.get(params.get("test"))
        changes.extend(_cycle_change(shadow, key, current, facts, rows))
    return changes


def _cycle_change(
    shadow: DevelopmentState,
    key: str,
    current: Mapping[str, object] | None,
    facts: _CycleFacts,
    rows: _TestRunRows,
) -> list[CriterionChange]:
    """Judge Cycle Count Criterion *key* from the candidate observation *current*.

    No change when the observation, or the baseline one it compares with,
    was lost to infrastructure: that says nothing about the Cycle Count.
    """
    params = shadow.criteria[key].params or {}
    test = params.get("test")
    if _unobserved_infrastructure(current):
        return []
    baseline, baseline_state = _bound_baseline(facts.prerequisites, params, test, current)
    if _unobserved_infrastructure(baseline):
        return []
    current_cycles = _healthy_cycles(current)
    baseline_cycles = baseline["cycle_count"] if baseline is not None else None
    checks = _cycle_threshold_checks(params, current_cycles, baseline_cycles)
    met = current_cycles is not None and all(check["pass"] for check in checks)
    detail = _cycle_detail(
        facts.selector,
        facts.identity,
        test,
        current_cycles,
        baseline_cycles,
        baseline_state,
        checks,
    )
    detail["test_runs"] = rows.of((current,))
    if baseline_state == "mismatched":
        run_name = current.get("test_run") if current is not None else None
        refused_as = baseline_refusal_key(str(test), cast("str | None", run_name))
        _name_baseline_refusal(detail, facts.refusals.get((facts.selector, refused_as)))
    return shadow.set_criterion(key, met, detail=detail)


def _candidate_cycle_observations(
    facts: Mapping[str, object], target: Mapping[str, object]
) -> dict[object, Mapping[str, object]]:
    """Candidate observations of named Tests, by Test (one Test Run each)."""
    return {
        test: item
        for test, item in _candidate_simulation_observations(facts, target).items()
        if test is not None
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
    current: Mapping[str, object] | None,
) -> tuple[Mapping[str, object] | None, str]:
    """The baseline observation a relative Cycle Count Criterion compares with.

    It must be the baseline's observation of the Test Run *current* observed:
    the same name and the same declared-inputs fingerprint on both revisions.
    """
    if not has_relative_threshold(dict(params)):
        return None, "not_required"
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
            and _names_baseline_target(params, target)
            and (revision is None or target.get("revision") == revision)
            and cycle.get("test") == test
            and _same_test_run(cycle, current)
            and isinstance(manifest, Mapping)
            and manifest.get("owner") == item.get("campaign_id")
            and isinstance(result, Mapping)
            and isinstance(item.get("work_item_id"), str)
        )
        if bound:
            return cycle, "observed"
    return None, "mismatched"


def _same_test_run(cycle: Mapping[str, object], current: Mapping[str, object] | None) -> bool:
    """Whether a baseline observation is of the Test Run the candidate observed.

    Facts before v2 name Tests only, on both sides; such a baseline
    observation binds by Test, which is unique there.
    """
    if "test_run" not in cycle:
        return current is None or "test_run" not in current
    return (
        current is not None
        and cycle["test_run"] == current.get("test_run")
        and cycle["test_run_fingerprint"] == current.get("test_run_fingerprint")
    )


def _name_baseline_refusal(detail: dict[str, object], refusal: str | None) -> None:
    """Say why no baseline ran when its revision could not run the Test Run.

    The comparison is then inconclusive rather than merely unbound.
    """
    if refusal is not None:
        detail["baseline_observation"] = "inapplicable"
        detail["reason"] = refusal


def _names_baseline_target(params: Mapping[str, object], target: Mapping[str, object]) -> bool:
    """Whether *target* is the baseline Target a Cycle Count Criterion compares against.

    An explicit baseline Target names its selector. Otherwise the baseline is
    the Criterion's own Target, which a Goal names only in ``target`` (as a
    selector or an identity) and a Criterion may also pin by selector.
    """
    if BASELINE_TARGET_PARAM in params:
        return target.get("selector") == params[BASELINE_TARGET_PARAM]
    return criterion_matches_target(
        params,
        identity=f"{target.get('vlnv')}#{target.get('name')}",
        selector=str(target.get("selector")),
    )


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


# ---------------------------------------------------------------------------
# Goals: Criteria judged by frozen Test Runs (ADR 0072)
# ---------------------------------------------------------------------------


def _goal_changes(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    target: Mapping[str, object],
    rows: _TestRunRows,
    refusals: Mapping[tuple[str, str], str],
    frozen: FrozenTestRuns,
) -> list[CriterionChange]:
    """The Criterion changes of every Goal this campaign's Test Runs are evidence for.

    A Test Run is evidence for a Goal only when its resolved name and its
    declared-inputs fingerprint equal one of the Goal's frozen Test Runs.
    A Goal none of whose frozen runs this campaign graded gets no change: for
    that Goal the campaign is diagnostic. Every other Goal gets one change
    that carries its matching runs, whatever else the campaign ran and
    whether or not it ran to its end: a matching failure is never left out.
    A Cycle Count or coverage Goal's change also carries the metric when
    this campaign measured it (:meth:`GoalTestRuns.measured_by`).

    Whether the Goal is met is decided by its recorder: from every matching
    run recorded so far, or for a coverage Goal from the coverage floors
    alone. ``met`` here is this campaign's own verdict.
    """
    if target["role"] != "candidate":
        return []
    names = {f"{target['vlnv']}#{target['name']}", str(target["selector"])}
    cycle = _CycleFacts.of(outcome, target, refusals)
    ran = rows.all()
    changes: list[CriterionChange] = []
    for goal in frozen.of_target(names):
        matched = goal.matching(ran)
        if goal.key not in shadow.criteria or not _graded(matched):
            continue
        measured: list[CriterionChange] = []
        if goal.kind == "cycle_count" and goal.measured_by(ran):
            measured = _goal_cycle_change(outcome, shadow, goal, matched, cycle, rows)
        elif goal.kind == "coverage" and goal.measured_by(ran) and outcome.complete:
            measured = _goal_coverage_change(outcome, shadow, target, goal, rows)
        if not measured:
            detail = {**_goal_simulation_detail(outcome, matched), **goal.outcomes_detail(matched)}
            measured = shadow.set_criterion(goal.key, goal.own_verdict(matched), detail=detail)
        changes.extend(measured)
    return changes


def _graded(rows: Sequence[Mapping[str, object]]) -> bool:
    """Whether any of *rows* observed its Test Run pass or fail."""
    return any(row["grade"] in {"pass", "fail"} for row in rows)


def _goal_simulation_detail(
    outcome: CampaignOutcome, matched: list[dict[str, object]]
) -> dict[str, object]:
    """What one campaign observed of a simulation Goal's frozen Test Runs."""
    facts = outcome.acceptance_facts.document
    names = [str(row["name"]) for row in matched]
    passed = [str(row["name"]) for row in matched if row["grade"] == "pass"]
    return {
        "campaign_id": facts["campaign_id"],
        "manifest_sha256": facts["manifest_sha256"],
        "acceptance_facts_sha256": outcome.acceptance_facts.sha256,
        "aggregate_grade": outcome.aggregate_grade,
        "tests_passed": len(passed),
        "tests_total": len(names),
        "selected_tests": names,
        "passed_tests": passed,
        "failed_tests": [name for name in names if name not in passed],
        "skipped_tests": [],
        "test_runs": matched,
    }


def _goal_cycle_change(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    goal: GoalTestRuns,
    matched: list[dict[str, object]],
    cycle: _CycleFacts,
    rows: _TestRunRows,
) -> list[CriterionChange]:
    """Judge a Cycle Count Goal from the observation of its one frozen Test Run.

    Nothing when that observation, or its baseline one, cannot be judged.
    """
    observations = _candidate_observations(outcome.acceptance_facts.document, outcome.target)
    current = observations.get(str(matched[0]["name"]))
    if current is None:
        return []
    return _cycle_change(shadow, goal.key, current, cycle, rows)


def _goal_coverage_change(
    outcome: CampaignOutcome,
    shadow: DevelopmentState,
    target: Mapping[str, object],
    goal: GoalTestRuns,
    rows: _TestRunRows,
) -> list[CriterionChange]:
    """Judge a coverage Goal from a coverage campaign that ran exactly its frozen Test Runs.

    Coverage is merged over the campaign's Test Runs, so only such a campaign
    measured the Goal's coverage; it is never merged across campaigns.
    Nothing when the campaign collected no coverage.
    """
    if outcome.coverage_reference is None:
        return []
    return [
        change
        for change in _coverage_changes(outcome, shadow, target, rows)
        if change.key == goal.key
    ]


__all__ = [
    "AcceptanceContext",
    "AcceptanceOutcome",
    "SimulationAcceptanceCoordinator",
    "record_campaign_acceptance",
]
