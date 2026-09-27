"""Dependency-neutral simulation Criterion contract resolution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SimulationCriterionContract:
    """Normalized test requirements for one simulation Criterion."""

    selector: str
    required_tests: frozenset[str]
    minimum_total: int | None


def resolve_simulation_criterion_contract(
    params: Mapping[str, object],
    registered_tests: Iterable[str],
    selected_tests: Iterable[str],
) -> SimulationCriterionContract:
    """Resolve selector, required tests, and minimum evidence cardinality."""
    selector_value = params.get("test_selector") or params.get("selector") or "all"
    selector = selector_value if isinstance(selector_value, str) and selector_value else "all"
    registered = frozenset(registered_tests)
    selected = frozenset(selected_tests)
    required_value = params.get("required_tests")
    if isinstance(required_value, list) and all(isinstance(name, str) for name in required_value):
        required = frozenset(required_value)
    elif selector == "all" and registered:
        required = registered
    elif selector != "all":
        required = frozenset({selector})
    else:
        required = selected
    minimum_value = params.get("minimum_total", len(required))
    minimum_total = (
        minimum_value
        if isinstance(minimum_value, int) and not isinstance(minimum_value, bool)
        else None
    )
    return SimulationCriterionContract(selector, required, minimum_total)
