"""Invocation-scoped Criterion truth for Flow and Specialist report headlines."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from booley.criteria.state import CriterionChange
from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.targets.catalog import selector_names_match
from booley.targets.domain import criterion_matches_target
from booley.targets.selection import selectors_name_distinct_cores

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState
    from booley.flows.prepared_invocation import PreparedInvocation
    from booley.targets.domain import TargetHandle


@dataclass(frozen=True)
class ReportScope:
    """What one prepared invocation can judge: its Targets and planned Criterion keys."""

    targets: frozenset[str]
    planned: frozenset[str]
    uncertain: frozenset[str] = frozenset()


@dataclass
class ReportCriteria:
    """Frozen invocation scope plus effective evaluations, independent of durability."""

    targets: frozenset[str] = frozenset()
    planned: frozenset[str] = frozenset()
    uncertain: frozenset[str] = frozenset()
    known: bool = True
    frozen: bool = False
    evaluated: dict[str, bool] = field(default_factory=dict)

    def record(self, changes: Iterable[CriterionChange]) -> None:
        for change in changes:
            self.evaluated[change.key] = change.met

    def project(self, outcome: EndpointOutcome) -> None:
        outcome.criterion_key = ""
        outcome.criterion_met = None
        keys = self.planned | self.evaluated.keys()
        if (
            not self.known
            or self.uncertain - self.evaluated.keys()
            or len(self.targets) > 1
            or len(keys) != 1
        ):
            return
        key = next(iter(keys))
        if key in self.evaluated:
            outcome.criterion_key = key
            outcome.criterion_met = self.evaluated[key]


def freeze(endpoint: EndpointState, invocation: PreparedInvocation) -> None:
    """Freeze the prepared invocation's report scope, without changing invocation validation."""
    if endpoint.endpoint_kind not in {"flow", "specialist"}:
        return
    ledger = endpoint._report_criteria
    if ledger.frozen:
        return
    ledger.frozen = True
    ledger.targets = frozenset(endpoint._requested_targets())
    try:
        scope = invocation.report_scope(endpoint)
    except (FuseSocError, OSError, ValueError):
        # Reporting lookups must not replace the endpoint's own admission errors.
        ledger.known = False
        return
    ledger.targets = scope.targets
    ledger.planned = scope.planned
    ledger.uncertain = scope.uncertain


def planned_scope(
    endpoint: EndpointState,
    handles: tuple[TargetHandle, ...],
    families: set[str],
    conventional: set[str],
) -> ReportScope:
    """Plan the Criterion keys of *families* for the resolved *handles* (or requested tokens)."""
    tokens = frozenset(endpoint._requested_targets())
    targets = frozenset(handle.identity for handle in handles) if handles else tokens
    if endpoint.name == "reviewer":
        return ReportScope(targets, frozenset({endpoint._criterion_key()}))
    families = set(families)
    keys: set[str] = set()
    uncertain: set[str] = set()
    if "elaborate_standalone" in families and (
        "elaborate_standalone" in endpoint.state.criteria or not endpoint.state.strict_criteria
    ):
        keys.add("elaborate_standalone")
    families.discard("elaborate_standalone")
    for handle in handles:
        keys.update(target_keys(endpoint, handle, families, conventional))
    # Overrides use stored selector bindings without re-running preparation.
    if not handles:
        for token in endpoint._requested_targets():
            keys.update(_fallback_keys(endpoint, token, families, conventional, uncertain))
    return ReportScope(targets, frozenset(keys), frozenset(uncertain))


def _named_keys(endpoint, name: str, families: set[str], conventional: set[str]) -> set[str]:
    keys: set[str] = set()
    detail = _selection_detail(endpoint)
    for family in families:
        generic = f"{family}_{name}"
        aliases = endpoint.state.flow_key_aliases.get(generic, [])
        if generic in endpoint.state.criteria:
            keys.add(generic)
        elif aliases:
            keys.update(
                alias
                for alias in aliases
                if alias in endpoint.state.criteria
                and endpoint.state._alias_matches_run(alias, detail)
            )
        elif generic.rsplit("_", 1)[0] in endpoint.state.criteria:
            keys.add(generic.rsplit("_", 1)[0])
        elif not endpoint.state.strict_criteria and family in conventional:
            keys.add(generic)

    return keys


def target_keys(
    endpoint, handle: TargetHandle, families: set[str], conventional: set[str]
) -> set[str]:
    keys = _named_keys(endpoint, handle.name, families, conventional)
    for key, entry in endpoint.state.criteria.items():
        if not any(key == family or key.startswith(f"{family}_") for family in families):
            continue
        if criterion_matches_target(
            entry.params or {}, identity=handle.identity, selector=handle.selector
        ):
            keys.add(key)
    for family in families:
        generic = f"{family}_{handle.name}"
        if generic not in endpoint.state.criteria and any(
            key != generic and (key == family or key.startswith(f"{family}_")) for key in keys
        ):
            keys.discard(generic)
    return keys


def _selection_detail(endpoint: EndpointState) -> dict[str, Any]:
    selector = getattr(endpoint.args, "test", None)
    selected = [selector] if isinstance(selector, str) else list(selector or ())
    return {
        "test_selector": selector
        if isinstance(selector, str)
        else ("partial" if selected else "all"),
        "selected_tests": selected,
    }


def _eligible_family(key: str, families: set[str]) -> bool:
    return any(key == family or key.startswith(f"{family}_") for family in families)


def _fallback_keys(
    endpoint,
    token: str,
    families: set[str],
    conventional: set[str],
    uncertain: set[str],
) -> set[str]:
    keys = _named_keys(endpoint, token.rsplit("#", 1)[-1], families, conventional)
    detail = _selection_detail(endpoint)
    for key, entry in endpoint.state.criteria.items():
        if not _eligible_family(key, families):
            continue
        params = entry.params or {}
        selected = detail["selected_tests"]
        if key.startswith("cycle_count_") and selected and params.get("test") not in selected:
            continue
        if params.get("test_selector") not in {
            None,
            "all",
        } and not endpoint.state._alias_matches_run(key, detail):
            continue
        if params.get("_target_selector") != token and params.get("target") != token:
            expected = params.get("_target_selector", params.get("target"))
            bound = params.get("target")
            if not isinstance(bound, str):
                bound = expected
            if (
                key not in keys
                and isinstance(expected, str)
                and selector_names_match(expected, token)
                and not selectors_name_distinct_cores(bound, token)
            ):
                # A name or core inequality rules a Target out; equal names with
                # compatible cores cannot be resolved without the catalog.
                # Actual evaluation resolves only this key, not other potential keys.
                uncertain.add(key)
            continue
        keys.add(key)
    return keys


def project(endpoint: EndpointState, outcome: EndpointOutcome) -> None:
    """Normalize only endpoints owned by the common headline contract."""
    if endpoint.endpoint_kind not in {"flow", "specialist"}:
        return
    if (
        not endpoint._acceptance_recorder.acceptance_identity()
        or not endpoint._report_criteria.frozen
    ):
        outcome.criterion_key = ""
        outcome.criterion_met = None
        return
    endpoint._report_criteria.project(outcome)
