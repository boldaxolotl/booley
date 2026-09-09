"""Literal boundary tests for the approved public timeout observation rule."""

import asyncio
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def oracle(monkeypatch):
    evaluator = Path(__file__).resolve().parents[2] / "qa/scenarios/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    return importlib.import_module("timeout_checks")


class PinTrace:
    def __init__(self, oracle, edges, cycle=101, blocked=False):
        self.oracle = oracle
        self.edges = edges
        self.cycle = cycle
        self.irqs = [64 if i in edges else 0 for i in range(cycle)]
        self.observations = []
        self.blocked = blocked

    async def tick(self):
        if self.blocked:
            raise self.oracle.ObservationBlockedError("Simulator unavailable")
        self.irqs.append(64 if self.cycle in self.edges else 0)
        self.cycle += 1

    def expect(self, observed, expected, reason):
        if observed != expected:
            raise self.oracle.CircuitMismatchError(reason)


@pytest.mark.parametrize("edge", [2020, 2276])
def test_timeout_accepts_inclusive_public_boundaries(oracle, edge):
    driver = PinTrace(oracle, {edge})
    assert asyncio.run(oracle.timed_event(driver, (100, 100), 99)) == edge


@pytest.mark.parametrize("edges", [{2019}, {2277}, set(), {2019, 2100}])
def test_timeout_rejects_first_early_late_or_missing_event(oracle, edges):
    driver = PinTrace(oracle, edges)
    with pytest.raises(oracle.CircuitMismatchError):
        asyncio.run(oracle.timed_event(driver, (100, 100), 99))


def test_timeout_checks_edges_recorded_during_bus_work(oracle):
    driver = PinTrace(oracle, {2020}, cycle=2300)
    assert asyncio.run(oracle.timed_event(driver, (100, 100), 99)) == 2020


def test_timeout_does_not_count_previous_latched_irq_as_new_event(oracle):
    driver = PinTrace(oracle, set(range(100, 300)))
    with pytest.raises(oracle.CircuitMismatchError):
        asyncio.run(oracle.timed_event(driver, (100, 100), 100))


def test_depth_measurement_uncertainty_only_widens_upper_window(oracle):
    assert oracle.event_window((100, 104)) == (2020, 2280)


def test_operational_failure_remains_blocked(oracle):
    driver = PinTrace(oracle, set(), blocked=True)
    with pytest.raises(oracle.ObservationBlockedError):
        asyncio.run(oracle.timed_event(driver, (100, 100), 99))


def test_slow_depth_observation_is_blocked_not_a_hardware_failure(oracle):
    class SlowDepthRead:
        cycle = 100
        last_acceptance = 100

        def schedule_rx(self, payload, nco):
            return 800

        async def read(self, address):
            self.last_acceptance += 9
            self.cycle += 9
            return 2 << 16

    with pytest.raises(oracle.ObservationBlockedError, match="observation gap"):
        asyncio.run(oracle.received_change(SlowDepthRead(), 0xA5, 2))


def test_timeout_manifest_preserves_concrete_fifo_depths():
    from qa.scenarios.uart.evaluator.cases import materialize

    manifest = materialize("0123456789abcdef0123456789abcdef")
    for item in manifest["cases"]:
        if item["operation"] == "timeout":
            parameters = item["parameters"]
            assert len(parameters["payload"]) == (
                64 if parameters["mode"] == "full-drop-no-reset" else 2
            )
