"""Target-pair plans for the implementation Flows' ``--baseline`` comparison."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import InitVar, dataclass
from pathlib import Path

from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetHandle


class ImplementationComparisonError(ValueError):
    """A Target comparison plan cannot be built or executed."""


@dataclass(frozen=True)
class TargetExecutionRef:
    """Checkout-independent Target identity and its callable selector."""

    identity: str
    selector: str
    vlnv: str


_PLAN_FACTORY_KEY = object()


@dataclass(frozen=True)
class TargetPairPlan:
    """Validated baseline/candidate Target references for one Flow."""

    flow: str
    baseline: TargetExecutionRef
    candidate: TargetExecutionRef
    _factory_key: InitVar[object]

    def __post_init__(self, _factory_key: object) -> None:
        if _factory_key is not _PLAN_FACTORY_KEY:
            raise TypeError("TargetPairPlan values are created by the pair-plan factory")


def _execution_ref(handle: TargetHandle) -> TargetExecutionRef:
    return TargetExecutionRef(handle.identity, handle.selector, handle.vlnv)


def _make_plan(
    flow: str, baseline: TargetExecutionRef, candidate: TargetExecutionRef
) -> TargetPairPlan:
    return TargetPairPlan(flow, baseline, candidate, _PLAN_FACTORY_KEY)


def target_pair_plans_for_handles(
    candidates: Sequence[TargetHandle], *, flow: str
) -> tuple[TargetPairPlan, ...]:
    """Pair each already-normalized public Flow candidate with itself."""
    plans: list[TargetPairPlan] = []
    seen: set[str] = set()
    for handle in candidates:
        if flow not in handle.drivable_by:
            raise ImplementationComparisonError(
                f"candidate Target {handle.identity!r} cannot be driven by the {flow!r} Flow"
            )
        candidate = _execution_ref(handle)
        if candidate.identity in seen:
            raise ImplementationComparisonError(
                f"candidate Target {handle.selector!r} duplicates canonical identity "
                f"{candidate.identity!r}"
            )
        seen.add(candidate.identity)
        plans.append(_make_plan(flow, candidate, candidate))
    return tuple(plans)


def target_pair_for_candidate(
    plans: Sequence[TargetPairPlan], candidate_identity: str
) -> TargetPairPlan:
    """Return exactly one plan for a canonical candidate identity or raise."""
    matches = tuple(plan for plan in plans if plan.candidate.identity == candidate_identity)
    if len(matches) != 1:
        raise ImplementationComparisonError(
            f"no unique Target comparison plan for candidate identity {candidate_identity!r}"
        )
    return matches[0]


def target_plan_for_handle(
    plans: Sequence[TargetPairPlan], handle: TargetHandle, *, flow: str
) -> TargetPairPlan:
    """Look up a canonical plan, defaulting to the Target paired with itself."""
    if plans:
        return target_pair_for_candidate(plans, handle.identity)
    candidate = _execution_ref(handle)
    return _make_plan(flow, candidate, candidate)


def selected_target_handle(
    handles: Mapping[str, TargetHandle], project_root: Path | str, target: str, *, flow: str
) -> TargetHandle:
    """Reuse a selected handle in its Project, otherwise reselect there."""
    root = Path(project_root).resolve()
    selected = handles.get(target)
    if selected is not None and selected.project_root == root:
        return selected
    return TargetCatalog.build(root).select(target, for_flow=flow)


def candidate_execution_refs(
    handles: Sequence[TargetHandle], plans: Sequence[TargetPairPlan]
) -> dict[str, TargetExecutionRef]:
    """Index planned candidate references by each Flow's selected key."""
    return {
        handle.selector: target_pair_for_candidate(plans, handle.identity).candidate
        for handle in handles
    }


def baseline_execution_context(
    plans: Sequence[TargetPairPlan], project_root: Path | str
) -> tuple[dict[str, TargetHandle], dict[str, TargetExecutionRef]]:
    """Reselect and identity-check every planned baseline in its checkout."""
    handles: dict[str, TargetHandle] = {}
    references: dict[str, TargetExecutionRef] = {}
    catalog = TargetCatalog.build(project_root)
    for plan in plans:
        ref = plan.baseline
        try:
            handle = catalog.select(ref.selector, for_flow=plan.flow)
        except FuseSocError as exc:
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
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> fusesoc_registry.ResolvedTarget:
    """Resolve the plan's exact callable selector after handle verification."""
    if handle.identity != ref.identity or handle.vlnv != ref.vlnv:
        raise ImplementationComparisonError(
            f"Target execution reference {ref.identity!r} does not match "
            f"selected handle {handle.identity!r}"
        )
    return fusesoc_registry.resolve_target_handle(
        handle,
        build_root=build_root,
        library_plan=fusesoc_registry.build_library_plan(handle.project_root, build_root),
        runner=runner,
    )
