"""Contract tests for the deep Simulation execution module."""

from __future__ import annotations

import pytest

from booley.flows.sim.execution import (
    DefaultSelection,
    InvalidSimulationRequestError,
    NamedTests,
    SimulationOptions,
)


def test_named_tests_preserve_order() -> None:
    selection = NamedTests(("reset", "interrupt"))

    assert selection.names == ("reset", "interrupt")


@pytest.mark.parametrize("names", [(), ("reset", "reset"), ("",)])
def test_named_tests_reject_invalid_selections(names: tuple[str, ...]) -> None:
    with pytest.raises(InvalidSimulationRequestError):
        NamedTests(names)


def test_default_selection_is_explicit() -> None:
    assert isinstance(DefaultSelection(), DefaultSelection)


def test_options_reject_invalid_timeout_and_verbosity() -> None:
    with pytest.raises(InvalidSimulationRequestError, match="timeout"):
        SimulationOptions(timeout_ms=0)
    with pytest.raises(InvalidSimulationRequestError, match="verbosity"):
        SimulationOptions(result_verbosity="verbose")
