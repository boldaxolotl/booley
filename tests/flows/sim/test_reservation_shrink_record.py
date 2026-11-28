"""A Simulation Record's Memory Reservation shrinks as its build and runs end (#3240, ADR 0087).

The real ``SimulateFlow`` runs on the fake-Verilator Project of the build-reuse
tests. The shrinker is built the way admission builds it: ``plan_memory_claim``
over the Flow's own ``simulation_record_job_requests``, so the request keys must match
the keys the report sites use.
"""

from __future__ import annotations

import importlib
import itertools
import os
from pathlib import Path
from typing import Any

import pytest

from booley.flows import job_estimates
from booley.flows.memory_admission import plan_memory_claim
from booley.flows.sim import live_progress
from booley.flows.sim.execution import engine
from booley.flows.sim.flow import SimulateFlow
from booley.runtime import job_slots
from tests.flows.sim import test_verilator_build_reuse_record as reuse
from tests.test_job_slots import FakeWorld, make_store, spawn

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Verilator build reuse is proven in the POSIX Sandbox"
)


class _LazyPhases:
    """``booley.flows.reservation_phases``, imported on first use so a missing module fails the test."""

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("booley.flows.reservation_phases"), name)


rp = _LazyPhases()

# The Project fixture of the build-reuse tests, shared by name.
flow_project = reuse.flow_project

BUDGET_MIB = 20_000.0
# History peaks of 6,553.6 and 40 MiB are 8,192 and 50 MiB with the estimate margin.
BUILD_PEAK_MIB = 6553.6
RUN_PEAK_MIB = 40.0


@pytest.fixture
def planned() -> list[tuple[str, ...]]:
    """The phases each Job's claim planned, in run order."""
    return []


@pytest.fixture
def probes(
    request: pytest.FixtureRequest,
    planned: list[tuple[str, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, float | None]]:
    """Run every Flow under a real claim; record the reservation at each build and run start.

    Indirect parameter ``False`` leaves the ``sim_pre_run`` records out of the history.
    """
    pre_sim_measured = getattr(request, "param", True)
    world = FakeWorld()
    store = make_store(tmp_path / "slots", world)
    pids = itertools.count(101)
    seen: list[tuple[str, float | None]] = []

    def held_mib() -> float | None:
        (token,) = store.observe(job_slots.CLASS_MEMORY)[0]
        assert token.memory is not None
        return token.memory.reservation_mb

    original_run = SimulateFlow.run_prepared_simulation

    def run_under_claim(self, prepared, admission):
        requests = self.simulation_record_job_requests(prepared)
        assert requests
        records = [
            {
                "key": key._asdict(),
                "peak_memory_mb": BUILD_PEAK_MIB if key.phase == "sim_build" else RUN_PEAK_MIB,
                "wall_s": 1.0,
            }
            for request in requests
            for key in job_estimates.phase_occurrences(request)
            if pre_sim_measured or key.phase != "sim_pre_run"
        ]
        plan = plan_memory_claim(requests, records, BUDGET_MIB, None)
        assert plan.phases
        planned.append(tuple(key.phase for key, _ in plan.phases))
        pid = next(pids)
        spawn(world, pid)
        token = store.submit(job_slots.CLASS_MEMORY, pid=pid, memory=plan.claim)
        assert store.refresh(token).state == job_slots.HOLDING
        phase_plan = rp.PhasePlan(plan.phases, plan.claim.reservation_mb)
        try:
            with rp.shrinking(rp.ReservationShrinker(phase_plan, store, token)):
                return original_run(self, prepared, admission)
        finally:
            store.release(token)

    original_observe = engine.observe_stage

    def observe(target, stage, **kwargs):
        if stage in ("building", "executing"):
            seen.append((stage, held_mib()))
        return original_observe(target, stage, **kwargs)

    monkeypatch.setattr(SimulateFlow, "run_prepared_simulation", run_under_claim)
    monkeypatch.setattr(engine, "observe_stage", observe)
    return seen


def test_reused_build_drops_reservation_without_building(
    flow_project: Path, tmp_path: Path, probes: list[tuple[str, float | None]]
) -> None:
    reports = tmp_path / "reports"
    reuse._assert_built(reuse._flow_sim(flow_project, reports, "first"), "missing provenance")
    assert probes == [("building", 8192.0), ("executing", 50.0)]
    probes.clear()
    reuse._assert_hit(reuse._flow_sim(flow_project, reports, "first"))
    # No build ran, and the build was counted out all the same.
    assert probes == [("executing", 50.0)]


def test_baseline_attempt_reports_no_phase_end(tmp_path: Path) -> None:
    ends: list[tuple[str, str, tuple[str, ...]]] = []

    class Recorder:
        def phase_ended(self, target, phase, tests=()):
            ends.append((target, phase, tuple(tests)))

    with rp.shrinking(Recorder()):  # type: ignore[arg-type]
        live_progress.end_phase("t", "sim_build")
        with live_progress.attempt_scope("t", role="baseline"):
            live_progress.end_phase("t", "sim_build")
            live_progress.end_phase("t", "sim_run", tests=("x",))
        with live_progress.attempt_scope("t"):
            live_progress.end_phase("t", "sim_run", tests=("x",))
    assert ends == [("t", "sim_build", ()), ("t", "sim_run", ("x",))]


@pytest.mark.parametrize(
    ("probes", "expected"),
    [
        pytest.param(True, [("building", 8192.0), ("executing", 50.0)], id="measured"),
        pytest.param(False, [("building", 8192.0), ("executing", 8192.0)], id="unmeasured"),
    ],
    indirect=["probes"],
)
def test_pre_sim_commands_reserve_their_own_estimate(
    flow_project: Path,
    tmp_path: Path,
    probes: list[tuple[str, float | None]],
    planned: list[tuple[str, ...]],
    expected: list[tuple[str, float]],
) -> None:
    """The build ends when it settles; measured Pre-Sim Commands no longer hold its estimate.

    Until the Target's Pre-Sim Commands are measured, they reserve the build's.
    """
    reuse._configure(flow_project, reuse._RUN_CWD_FIRMWARE)
    reuse._flow_sim(flow_project, tmp_path / "reports", "first")
    assert probes == expected
    # The Job planned its Pre-Sim Commands as a phase of their own.
    assert [phases.count("sim_pre_run") for phases in planned] == [1], planned
