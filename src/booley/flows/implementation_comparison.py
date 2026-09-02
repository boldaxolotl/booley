"""Canonical Target-pair plans for baseline-relative implementation Criteria."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict, as_str
from booley.criteria.templates import BASELINE_TARGET_PARAM, TargetPair
from booley.fusesoc import fusesoc_registry
from booley.targets.target import TargetHandle, select_target
from booley.ticket_board.target_contract import (
    SCHEMA_VERSION,
    ContractTargetBinding,
    TargetContract,
)


class ImplementationComparisonError(ValueError):
    """Persisted criterion metadata cannot define an executable Target pair."""


@dataclass(frozen=True)
class TargetExecutionRef:
    """Checkout-independent Target identity and its callable selector."""

    identity: str
    selector: str
    vlnv: str


_PLAN_FACTORY_KEY = object()


@dataclass(frozen=True)
class TargetPairPlan:
    """Validated baseline/candidate execution authority for one Flow."""

    flow: str
    baseline: TargetExecutionRef
    candidate: TargetExecutionRef
    binding: ContractTargetBinding | None
    _factory_key: InitVar[object]

    def __post_init__(self, _factory_key: object) -> None:
        if _factory_key is not _PLAN_FACTORY_KEY:
            raise TypeError("TargetPairPlan values are created by the pair-plan factory")

    @property
    def sealed(self) -> bool:
        """Whether a unique persisted binding is this plan's authority."""
        return self.binding is not None

    def evidence_pair(self) -> TargetPair:
        """Return selector compatibility fields for existing report readers."""
        return TargetPair(self.baseline.selector, self.candidate.selector)


def _execution_ref(
    handle: TargetHandle,
    *,
    selector: str | None = None,
) -> TargetExecutionRef:
    return TargetExecutionRef(handle.identity, selector or handle.selector, handle.vlnv)


def _make_plan(
    flow: str,
    baseline: TargetExecutionRef,
    candidate: TargetExecutionRef,
    binding: ContractTargetBinding | None,
) -> TargetPairPlan:
    return TargetPairPlan(flow, baseline, candidate, binding, _PLAN_FACTORY_KEY)


def _entry_params(entry: object) -> dict[Any, Any] | None:
    return as_dict(getattr(entry, "params", None))


def _state_pair_from_entry(
    entry: object | None,
    criterion_key: str,
    candidate: str,
) -> TargetPair:
    params = _entry_params(entry) if entry is not None else None
    if params is None or BASELINE_TARGET_PARAM not in params:
        return TargetPair(candidate, candidate)
    baseline = as_str(params[BASELINE_TARGET_PARAM])
    if baseline is None or not baseline.strip():
        raise ImplementationComparisonError(
            f"{criterion_key} has invalid baseline Target metadata"
        )
    return TargetPair(baseline.strip(), candidate)


def _state_pair(criteria: Mapping[str, Any], criterion_prefix: str, candidate: str) -> TargetPair:
    """Read the persisted pair, preserving standalone equal-Target behavior."""
    key = f"{criterion_prefix}{candidate}"
    return _state_pair_from_entry(criteria.get(key), key, candidate)


def _criterion_matches_candidate(
    key: str,
    entry: object,
    criterion_prefix: str,
    candidate: TargetExecutionRef,
) -> bool:
    candidate_name = candidate.identity.rsplit("#", maxsplit=1)[-1]
    if key in {
        f"{criterion_prefix}{candidate.selector}",
        f"{criterion_prefix}{candidate.identity}",
        f"{criterion_prefix}{candidate_name}",
    }:
        return True
    params = _entry_params(entry) or {}
    sealed_selector = as_str(params.get("_target_selector"))
    authored_target = as_str(params.get("target"))
    return sealed_selector == candidate.selector or authored_target in (
        candidate.selector,
        candidate.identity,
    )


def _state_pair_for_candidate(
    criteria: Mapping[str, Any],
    criterion_prefix: str,
    candidate: TargetExecutionRef,
) -> TargetPair:
    matches = [
        (key, entry)
        for key, entry in criteria.items()
        if _criterion_matches_candidate(key, entry, criterion_prefix, candidate)
    ]
    if len(matches) > 1:
        raise ImplementationComparisonError(
            f"no unique {criterion_prefix.removesuffix('_')} criterion for "
            f"candidate Target {candidate.identity!r}"
        )
    if not matches:
        return TargetPair(candidate.selector, candidate.selector)
    key, entry = matches[0]
    return _state_pair_from_entry(entry, key, candidate.selector)


def _select_execution_ref(
    project_root: Path | str,
    target: str,
    flow: str,
    *,
    execution_selector: str | None = None,
) -> TargetExecutionRef:
    try:
        handle = select_target(project_root, target, for_flow=flow)
    except fusesoc_registry.FuseSocError as exc:
        raise ImplementationComparisonError(str(exc)) from exc
    if handle.doctor_private:
        raise ImplementationComparisonError(
            f"Doctor-private Target {handle.identity!r} cannot enter persisted comparison state"
        )
    return _execution_ref(handle, selector=execution_selector)


def _binding_for_candidate(
    contract: TargetContract,
    flow: str,
    criterion: str,
    candidate: TargetExecutionRef,
) -> ContractTargetBinding:
    matches = tuple(
        binding
        for binding in contract.bindings
        if binding.flow == flow
        and binding.criterion == criterion
        and binding.candidate == candidate.identity
    )
    if len(matches) != 1:
        raise ImplementationComparisonError(
            f"sealed contract has no unique {flow}/{criterion} binding for "
            f"candidate Target {candidate.selector!r}"
        )
    return matches[0]


def _sealed_plan(
    contract: TargetContract,
    project_root: Path | str,
    flow: str,
    criterion: str,
    state_pair: TargetPair,
    candidate: TargetExecutionRef,
) -> TargetPairPlan:
    state_baseline = _select_execution_ref(project_root, state_pair.baseline, flow)
    binding = _binding_for_candidate(contract, flow, criterion, candidate)
    if state_baseline.identity != binding.baseline:
        raise ImplementationComparisonError(
            f"{criterion}_{state_pair.candidate} baseline Target metadata does not "
            "match the sealed contract"
        )
    if contract.schema < SCHEMA_VERSION:
        return _make_plan(flow, state_baseline, candidate, binding)
    if not binding.baseline_selector.strip() or not binding.candidate_selector.strip():
        raise ImplementationComparisonError(
            f"sealed {flow}/{criterion} binding for {candidate.identity!r} "
            "has an empty callable selector"
        )
    baseline = _select_execution_ref(
        project_root,
        binding.baseline_selector,
        flow,
        execution_selector=binding.baseline_selector,
    )
    sealed_candidate = _select_execution_ref(
        project_root,
        binding.candidate_selector,
        flow,
        execution_selector=binding.candidate_selector,
    )
    if baseline.identity != binding.baseline or sealed_candidate.identity != binding.candidate:
        raise ImplementationComparisonError(
            f"sealed {flow}/{criterion} selectors do not resolve to their sealed identities"
        )
    return _make_plan(flow, baseline, sealed_candidate, binding)


def target_pair_plans_for_handles(
    criteria: Mapping[str, Any],
    criterion_prefix: str,
    candidates: Sequence[TargetHandle],
    *,
    flow: str,
    contract: TargetContract | None = None,
) -> tuple[TargetPairPlan, ...]:
    """Build plans from already-normalized public Flow candidates."""
    plans: list[TargetPairPlan] = []
    seen: set[str] = set()
    for handle in candidates:
        if flow not in handle.drivable_by:
            raise ImplementationComparisonError(
                f"candidate Target {handle.identity!r} cannot be driven by the {flow!r} Flow"
            )
        candidate = _execution_ref(handle)
        state_pair = _state_pair_for_candidate(criteria, criterion_prefix, candidate)
        if contract is not None:
            if handle.doctor_private:
                raise ImplementationComparisonError(
                    f"Doctor-private Target {handle.identity!r} cannot enter a sealed plan"
                )
            plan = _sealed_plan(
                contract,
                handle.project_root,
                flow,
                criterion_prefix.removesuffix("_"),
                state_pair,
                candidate,
            )
        else:
            baseline = (
                candidate
                if state_pair.baseline == handle.selector
                else _select_execution_ref(
                    handle.project_root,
                    state_pair.baseline,
                    flow,
                )
            )
            plan = _make_plan(flow, baseline, candidate, None)
        if candidate.identity in seen:
            raise ImplementationComparisonError(
                f"candidate Target {handle.selector!r} duplicates canonical identity "
                f"{candidate.identity!r}"
            )
        seen.add(candidate.identity)
        plans.append(plan)
    return tuple(plans)


def target_pair_plans_for_candidates(
    criteria: Mapping[str, Any],
    criterion_prefix: str,
    candidates: Sequence[str],
    *,
    contract: TargetContract | None = None,
    project_root: Path | str | None = None,
    flow: str = "",
) -> tuple[TargetPairPlan, ...]:
    """Validate the complete requested pair batch before any Flow setup."""
    plans: list[TargetPairPlan] = []
    seen: set[str] = set()
    for candidate in candidates:
        state_pair = _state_pair(criteria, criterion_prefix, candidate)
        if contract is not None:
            if project_root is None or not flow:
                raise ImplementationComparisonError(
                    "Target contract comparison requires project root and flow"
                )
            plan = _sealed_plan(
                contract,
                project_root,
                flow,
                criterion_prefix.removesuffix("_"),
                state_pair,
                _select_execution_ref(project_root, state_pair.candidate, flow),
            )
        elif project_root is not None and flow:
            candidate_ref = _select_execution_ref(project_root, state_pair.candidate, flow)
            baseline_ref = _select_execution_ref(project_root, state_pair.baseline, flow)
            plan = _make_plan(flow, baseline_ref, candidate_ref, None)
        else:
            candidate_ref = TargetExecutionRef(state_pair.candidate, state_pair.candidate, "")
            baseline_ref = TargetExecutionRef(state_pair.baseline, state_pair.baseline, "")
            plan = _make_plan(flow, baseline_ref, candidate_ref, None)
        if plan.candidate.identity in seen:
            raise ImplementationComparisonError(
                f"candidate Target {candidate!r} duplicates canonical identity "
                f"{plan.candidate.identity!r}"
            )
        seen.add(plan.candidate.identity)
        plans.append(plan)
    return tuple(plans)


def target_pairs_for_candidates(
    criteria: Mapping[str, Any],
    criterion_prefix: str,
    candidates: Sequence[str],
    *,
    contract: TargetContract | None = None,
    project_root: Path | str | None = None,
    flow: str = "",
) -> tuple[TargetPairPlan, ...]:
    """Compatibility name for the canonical pair-plan factory."""
    return target_pair_plans_for_candidates(
        criteria,
        criterion_prefix,
        candidates,
        contract=contract,
        project_root=project_root,
        flow=flow,
    )


def target_pair_for_candidate(
    plans: Sequence[TargetPairPlan], candidate_identity: str
) -> TargetPairPlan:
    """Return exactly one plan for a canonical candidate identity or raise."""
    matches = tuple(plan for plan in plans if plan.candidate.identity == candidate_identity)
    if len(matches) != 1:
        raise ImplementationComparisonError(
            f"no unique Target pair plan for candidate identity {candidate_identity!r}"
        )
    return matches[0]


def target_plan_for_handle(
    plans: Sequence[TargetPairPlan],
    handle: TargetHandle,
    *,
    flow: str,
    sealed: bool,
) -> TargetPairPlan:
    """Look up a canonical plan, defaulting only for standalone execution."""
    if plans:
        return target_pair_for_candidate(plans, handle.identity)
    if sealed:
        raise ImplementationComparisonError(
            f"sealed {flow} execution has no Target pair plan for {handle.selector!r}"
        )
    candidate = _execution_ref(handle)
    return _make_plan(flow, candidate, candidate, None)


def selected_target_handle(
    handles: Mapping[str, TargetHandle],
    project_root: Path | str,
    target: str,
    *,
    flow: str,
) -> TargetHandle:
    """Reuse a selected handle in its Project, otherwise reselect there."""
    root = Path(project_root).resolve()
    selected = handles.get(target)
    if selected is not None and selected.project_root == root:
        return selected
    return select_target(root, target, for_flow=flow)


def candidate_execution_refs(
    handles: Sequence[TargetHandle],
    plans: Sequence[TargetPairPlan],
) -> dict[str, TargetExecutionRef]:
    """Index planned candidate references by each Flow's selected key."""
    return {
        handle.selector: target_pair_for_candidate(plans, handle.identity).candidate
        for handle in handles
    }


def baseline_execution_context(
    plans: Sequence[TargetPairPlan],
    project_root: Path | str,
) -> tuple[dict[str, TargetHandle], dict[str, TargetExecutionRef]]:
    """Reselect and identity-check every planned baseline in its checkout."""
    handles: dict[str, TargetHandle] = {}
    references: dict[str, TargetExecutionRef] = {}
    for plan in plans:
        ref = plan.baseline
        try:
            handle = select_target(project_root, ref.selector, for_flow=plan.flow)
        except fusesoc_registry.FuseSocError as exc:
            raise ImplementationComparisonError(
                f"baseline Target {ref.selector!r} cannot be selected: {exc}"
            ) from exc
        if handle.identity != ref.identity or handle.vlnv != ref.vlnv:
            raise ImplementationComparisonError(
                f"baseline Target {ref.selector!r} resolves to {handle.identity!r}, "
                f"expected {ref.identity!r}"
            )
        prior = references.get(ref.selector)
        if prior is not None and prior != ref:
            raise ImplementationComparisonError(
                f"baseline selector {ref.selector!r} has conflicting planned identities"
            )
        handles[ref.selector] = handle
        references[ref.selector] = ref
    return handles, references


def resolve_target_execution_ref(
    handle: TargetHandle,
    ref: TargetExecutionRef,
    *,
    build_root: Path | str,
) -> fusesoc_registry.ResolvedTarget:
    """Resolve the plan's exact callable selector after handle verification."""
    if handle.identity != ref.identity or handle.vlnv != ref.vlnv:
        raise ImplementationComparisonError(
            f"Target execution reference {ref.identity!r} does not match "
            f"selected handle {handle.identity!r}"
        )
    return fusesoc_registry.resolve_target(
        ref.selector,
        project_root=handle.project_root,
        build_root=build_root,
        vlnv=ref.vlnv,
    )
