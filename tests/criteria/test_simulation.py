from __future__ import annotations

import pytest

from booley.criteria.simulation import resolve_simulation_criterion_contract


@pytest.mark.parametrize(
    ("params", "registered", "selected", "selector", "required", "minimum"),
    [
        ({"test_selector": "half"}, ["half", "full"], ["half"], "half", {"half"}, 1),
        ({"selector": "half"}, ["half", "full"], ["half"], "half", {"half"}, 1),
        ({"test_selector": "all"}, ["half", "full"], ["half"], "all", {"half", "full"}, 2),
        (
            {"test_selector": "half", "required_tests": []},
            ["half", "full"],
            ["half"],
            "half",
            set(),
            0,
        ),
        (
            {"test_selector": "half", "minimum_total": 2},
            ["half", "full"],
            ["half"],
            "half",
            {"half"},
            2,
        ),
        (
            {"test_selector": "all", "minimum_total": True},
            [],
            ["default"],
            "all",
            {"default"},
            None,
        ),
        (
            {"test_selector": "all", "minimum_total": "1"},
            [],
            ["default"],
            "all",
            {"default"},
            None,
        ),
    ],
)
def test_resolve_simulation_criterion_contract(
    params, registered, selected, selector, required, minimum
) -> None:
    contract = resolve_simulation_criterion_contract(params, registered, selected)

    assert contract.selector == selector
    assert contract.required_tests == required
    assert contract.minimum_total == minimum
