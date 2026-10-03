"""Invocation-scoped Criterion truth for Flow and Specialist report headlines."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from booley.criteria.state import CriterionChange
from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.targets.domain import criterion_matches_target

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState
    from booley.targets.domain import TargetHandle


class PreparedReportSimulation(Protocol):
    """Read-only candidate scope supplied by simulation preparation."""

    targets: tuple[TargetHandle, ...]
    resume: Any
    selected_targets: tuple[str, ...]
    test_names_map: Mapping[str, list[str]]


@dataclass(frozen=True)
class ReportTarget:
    """Candidate identity retained when a resume has no catalog binding."""

    identity: str
    selector: str
    name: str


@dataclass
class ReportCriteria:
    """Frozen invocation scope plus effective evaluations, independent of durability."""

    targets: frozenset[str] = frozenset()
    planned: frozenset[str] = frozenset()
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
        if not self.known or len(self.targets) > 1 or len(keys) != 1:
            return
        key = next(iter(keys))
        if key in self.evaluated:
            outcome.criterion_key = key
            outcome.criterion_met = self.evaluated[key]


def freeze(endpoint: EndpointState, simulation: PreparedReportSimulation | None = None) -> None:
    """Use prepared candidate authorities once, without changing invocation validation."""
    ledger = endpoint._report_criteria
    if ledger.frozen:
        return
    ledger.frozen = True
    owner = getattr(endpoint, "flow", endpoint)
    if simulation is not None and not hasattr(simulation, "targets"):
        simulation = None
    tokens = frozenset(endpoint._requested_targets())
    ledger.targets = tokens
    try:
        handles, identities = _candidate_scope(owner, simulation)
        if identities is not None:
            ledger.targets = identities
        elif tokens and "_run" not in owner.__dict__:
            from booley.targets.catalog import TargetCatalog

            handles = TargetCatalog.build(Path(endpoint.args.work_dir)).select_many(
                ",".join(sorted(tokens)),
                for_flow=endpoint.name if endpoint.endpoint_kind == "flow" else None,
            )
            ledger.targets = frozenset(handle.identity for handle in handles)
        ledger.planned = frozenset(_planned_keys(endpoint, handles, simulation))
    except (FuseSocError, OSError, ValueError):
        # Reporting lookups must not replace the endpoint's own admission errors.
        ledger.known = not endpoint.satisfies


def _candidate_scope(owner, simulation):
    if simulation is not None:
        if simulation.resume is not None:
            manifest = simulation.resume.candidate.manifest
            binding = simulation.resume.binding_for(manifest)
            if binding is not None:
                return (binding.handle,), frozenset({binding.handle.identity})
            target = manifest.document["target"]
            candidate = ReportTarget(
                f"{target['vlnv']}#{target['name']}", str(target["selector"]), str(target["name"])
            )
            return (candidate,), frozenset({candidate.identity})
        handles = simulation.targets[: len(simulation.selected_targets)]
        return handles, frozenset(handle.identity for handle in handles)
    selection = getattr(owner, "_prepared_target_selection", None)
    if (
        selection is not None
        and "_run" not in owner.__dict__
        and selection.matches(owner.args.work_dir, owner.args.target)
    ):
        return selection.handles, frozenset(handle.identity for handle in selection.handles)
    return (), None


def _families(endpoint: EndpointState, simulation) -> set[str]:
    if endpoint.name != "sim":
        return set(endpoint.satisfies)
    mode = str(getattr(endpoint.args, "mode", "simulate"))
    if mode == "elab_only_standalone":
        return {"elaborate_standalone"}
    if mode == "elab_only":
        return {"elab_pass"}
    families = {"sim_pass", "cycle_count"}
    coverage = bool(getattr(endpoint.args, "coverage", False))
    if simulation is not None and simulation.resume is not None:
        items = simulation.resume.candidate.manifest.document["work_items"]
        coverage = bool(items) and items[0].get("kind") == "coverage_aggregate"
    if coverage:
        families.add("coverage")
    if simulation is None and endpoint.state._file_path is not None:
        families.add("elab_pass")
    return families


def _planned_keys(
    endpoint: EndpointState, handles: tuple[TargetHandle, ...], simulation
) -> set[str]:
    owner = getattr(endpoint, "flow", endpoint)
    if endpoint.name == "reviewer":
        return {owner._criterion_key()}
    families = _families(endpoint, simulation)
    if "elaborate_standalone" in families:
        return {"elaborate_standalone"}
    keys: set[str] = set()
    conventional = _conventional_families(endpoint, simulation)
    for handle in handles:
        mapped = _target_keys(endpoint, handle, families, conventional)
        if endpoint.name == "sim" and simulation is not None:
            mapped = _campaign_keys(endpoint, handle, mapped, simulation)
        keys.update(mapped)
    # Overrides use stored selector bindings without re-running preparation.
    if not handles:
        for token in endpoint._requested_targets():
            keys.update(_fallback_keys(endpoint, token, families, conventional))
    return keys


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
        elif family in endpoint.state.criteria:
            keys.add(family)
        elif not endpoint.state.strict_criteria and family in conventional:
            keys.add(generic)

    return keys


def _conventional_families(endpoint: EndpointState, simulation) -> set[str]:
    owner = getattr(endpoint, "flow", endpoint)
    if "_run" in owner.__dict__:
        return set()
    families = {"synth": {"synthesis_ok"}, "fpga": {"fpga_impl_ok"}, "lint": {"lint_clean"}}
    if endpoint.name != "sim":
        return families.get(endpoint.name, set())
    mode = str(getattr(endpoint.args, "mode", "simulate"))
    if mode == "elab_only":
        return {"elab_pass"}
    if endpoint.state._file_path is None:
        return {"sim_pass"}
    return {"sim_pass", "elab_pass"} if simulation is None else set()


def _target_keys(
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
    keys.update(_binding_keys(endpoint, handle.identity, handle.selector, families))
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


def _binding_keys(endpoint, identity: str | None, selector: str, families: set[str]) -> set[str]:
    keys: set[str] = set()
    for binding in endpoint.flow_acceptance.bindings:
        if binding.flow != endpoint.name or binding.candidate_selector != selector:
            continue
        if identity is not None and binding.candidate != identity:
            continue
        key = binding.criterion_key
        if endpoint.name != "sim" or _eligible_family(key, families):
            keys.add(key)
    return keys


def _fallback_keys(endpoint, token: str, families: set[str], conventional: set[str]) -> set[str]:
    keys = _named_keys(endpoint, token.rsplit("#", 1)[-1], families, conventional)
    detail = _selection_detail(endpoint)
    for key, entry in endpoint.state.criteria.items():
        if not _eligible_family(key, families):
            continue
        params = entry.params or {}
        if params.get("_target_selector") != token and params.get("target") != token:
            continue
        selected = detail["selected_tests"]
        if key.startswith("cycle_count_") and selected and params.get("test") not in selected:
            continue
        if params.get("test_selector") not in {
            None,
            "all",
        } and not endpoint.state._alias_matches_run(key, detail):
            continue
        keys.add(key)
    keys.update(_binding_keys(endpoint, None, token, families))
    return keys


def _campaign_keys(endpoint, handle, keys: set[str], simulation) -> set[str]:
    from booley.config.project_config import lookup_target_section

    if simulation.resume is not None:
        document = simulation.resume.candidate.manifest.document
        suite = document["required_suite"]
        registered = set(suite["names"])
        selected = {
            item["test"] for item in document["work_items"] if item.get("test") is not None
        }
    else:
        registered = set(lookup_target_section(simulation.test_names_map, handle.selector) or [])
        requested = getattr(endpoint.args, "test", None)
        selected = set((requested,) if isinstance(requested, str) else requested or registered)
        skip = getattr(endpoint.args, "skip", None)
        selected.difference_update((skip or "").split(","))
    sim_keys = endpoint.flow.report_simulation_criterion_keys(
        handle.identity, handle.selector, handle.name, registered, selected
    )
    return {
        key
        for key in keys
        if (not key.startswith("sim_pass") or key in sim_keys or endpoint.state._file_path is None)
        and (
            not key.startswith("cycle_count_")
            or endpoint.state.criteria[key].params.get("test") in selected
        )
    }


def project(endpoint: EndpointState, outcome: EndpointOutcome) -> None:
    """Normalize only endpoints owned by the common headline contract."""
    if endpoint.endpoint_kind not in {"flow", "specialist"}:
        return
    freeze(endpoint)
    endpoint._report_criteria.project(outcome)
