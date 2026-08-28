"""Target-pair selection for baseline-relative implementation Criteria."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict, as_str
from booley.dev_support.criteria import BASELINE_TARGET_PARAM, TargetPair
from booley.fusesoc import fusesoc_registry
from booley.ticket_board.target_contract import SCHEMA_VERSION, TargetContract


class ImplementationComparisonError(ValueError):
    """Persisted criterion metadata cannot define an executable Target pair."""


def _state_pair(criteria: Mapping[str, Any], criterion_prefix: str, candidate: str) -> TargetPair:
    """Read the persisted pair, preserving equal-Target behavior."""
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


def _canonical_selector(project_root: Path | str, target: str) -> str:
    ref = fusesoc_registry.resolve_ref(project_root, target)
    return f"{ref.vlnv}#{ref.name}"


def _sealed_pair(
    contract: TargetContract,
    project_root: Path | str,
    flow: str,
    criterion: str,
    state_pair: TargetPair,
) -> TargetPair:
    """Resolve one execution pair from sealed identities and selectors."""
    try:
        candidate = _canonical_selector(project_root, state_pair.candidate)
        state_baseline = _canonical_selector(project_root, state_pair.baseline)
    except fusesoc_registry.FuseSocError as exc:
        raise ImplementationComparisonError(str(exc)) from exc
    matches = tuple(
        binding
        for binding in contract.bindings
        if binding.flow == flow
        and binding.criterion == criterion
        and binding.candidate == candidate
    )
    if len(matches) != 1:
        raise ImplementationComparisonError(
            f"sealed contract has no unique {flow}/{criterion} binding for "
            f"candidate Target {state_pair.candidate!r}"
        )
    sealed = matches[0]
    if state_baseline != sealed.baseline:
        raise ImplementationComparisonError(
            f"{criterion}_{state_pair.candidate} baseline Target metadata does not "
            "match the sealed contract"
        )
    if contract.schema >= SCHEMA_VERSION:
        return TargetPair(sealed.baseline_selector, sealed.candidate_selector)
    # Schema 3 did not persist callable selectors, so retain the authored
    # spelling after proving that it resolves to the sealed identity.
    return state_pair


def target_pairs_for_candidates(
    criteria: Mapping[str, Any],
    criterion_prefix: str,
    candidates: Sequence[str],
    *,
    contract: TargetContract | None = None,
    project_root: Path | str | None = None,
    flow: str = "",
) -> tuple[TargetPair, ...]:
    """Resolve selected candidates; missing metadata preserves equal-Target behavior."""
    pairs: list[TargetPair] = []
    seen: dict[str, str] = {}
    for candidate in candidates:
        pair = _state_pair(criteria, criterion_prefix, candidate)
        if contract is not None:
            if project_root is None or not flow:
                raise ImplementationComparisonError(
                    "Target contract comparison requires project root and flow"
                )
            pair = _sealed_pair(
                contract,
                project_root,
                flow,
                criterion_prefix.removesuffix("_"),
                pair,
            )
        baseline = pair.baseline
        prior = seen.get(candidate)
        if prior is not None and prior != baseline:
            raise ImplementationComparisonError(
                f"candidate Target {candidate!r} has conflicting baselines "
                f"{prior!r} and {baseline!r}"
            )
        seen[candidate] = baseline
        pairs.append(pair)
    return tuple(pairs)


def target_pair_for_candidate(pairs: Sequence[TargetPair], candidate: str) -> TargetPair:
    """Return one resolved pair, defaulting only for equal-Target runs."""
    return next(
        (pair for pair in pairs if pair.candidate == candidate),
        TargetPair(candidate, candidate),
    )
