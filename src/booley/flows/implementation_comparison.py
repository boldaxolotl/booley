"""Canonical Target-pair plans for baseline-relative implementation Criteria."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True, init=False)
class TargetPairPlan:
    """Validated baseline/candidate execution authority for one Flow."""

    flow: str
    baseline: TargetExecutionRef
    candidate: TargetExecutionRef
    binding: ContractTargetBinding | None

    def __new__(cls) -> TargetPairPlan:
        raise TypeError("TargetPairPlan values are created by the pair-plan factory")

    @property
    def sealed(self) -> bool:
        """Whether a unique persisted binding is this plan's authority."""
        return self.binding is not None

    def evidence_pair(self) -> TargetPair:
        """Return selector compatibility fields for existing report readers."""
        return TargetPair(self.baseline.selector, self.candidate.selector)


def _execution_ref(handle: TargetHandle) -> TargetExecutionRef:
    return TargetExecutionRef(handle.identity, handle.selector, handle.vlnv)


def _make_plan(
    flow: str,
    baseline: TargetExecutionRef,
    candidate: TargetExecutionRef,
    binding: ContractTargetBinding | None,
) -> TargetPairPlan:
    plan = object.__new__(TargetPairPlan)
    object.__setattr__(plan, "flow", flow)
    object.__setattr__(plan, "baseline", baseline)
    object.__setattr__(plan, "candidate", candidate)
    object.__setattr__(plan, "binding", binding)
    return plan


def _state_pair(criteria: Mapping[str, Any], criterion_prefix: str, candidate: str) -> TargetPair:
    """Read the persisted pair, preserving standalone equal-Target behavior."""
    entry = criteria.get(f"{criterion_prefix}{candidate}")
    params = as_dict(getattr(entry, "params", None)) if entry is not None else None
    if params is None or BASELINE_TARGET_PARAM not in params:
        return TargetPair(candidate, candidate)
    baseline = as_str(params[BASELINE_TARGET_PARAM])
    if baseline is None or not baseline.strip():
        raise ImplementationComparisonError(
            f"{criterion_prefix}{candidate} has invalid baseline Target metadata"
        )
    return TargetPair(baseline.strip(), candidate)


def _select_execution_ref(project_root: Path | str, target: str, flow: str) -> TargetExecutionRef:
    try:
        handle = select_target(project_root, target, for_flow=flow)
    except fusesoc_registry.FuseSocError as exc:
        raise ImplementationComparisonError(str(exc)) from exc
    if handle.doctor_private:
        raise ImplementationComparisonError(
            f"Doctor-private Target {handle.identity!r} cannot enter persisted comparison state"
        )
    return _execution_ref(handle)


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
    baseline = _select_execution_ref(project_root, binding.baseline_selector, flow)
    sealed_candidate = _select_execution_ref(project_root, binding.candidate_selector, flow)
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
        candidate = _execution_ref(handle)
        state_pair = _state_pair(criteria, criterion_prefix, handle.selector)
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
