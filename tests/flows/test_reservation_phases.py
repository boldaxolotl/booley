"""A running Job's Memory Reservation shrinks as its phases finish (#3240, ADR 0087)."""

from __future__ import annotations

import importlib
import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.job_estimates import JobRequest
from booley.flows.memory_admission import plan_memory_claim
from booley.runtime import job_slots
from booley.runtime.job_slots import HOLDING, QUEUED, SlotStore
from booley.runtime.resource_history import HistoryKey
from tests.test_job_slots import FakeWorld, make_store, spawn


class _LazyPhases:
    """``booley.flows.reservation_phases``, imported on first use so a missing module fails the test."""

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("booley.flows.reservation_phases"), name)


rp = _LazyPhases()

BUDGET_MIB = 10_000.0
TARGET = "core"


def _record(phase: str, peak_mib: float, test_run: str = "") -> dict[str, Any]:
    return {
        "key": {"target": TARGET, "phase": phase, "flags": [], "test_run": test_run},
        "peak_memory_mb": peak_mib,
        "wall_s": 1.0,
    }


# History peaks of 6,553.6 and 40 MiB are 8,192 and 50 MiB with the estimate margin.
SIM_HISTORY = [_record("sim_build", 6553.6), _record("sim_run", 40, "smoke")]
SIM_REQUEST = (JobRequest("sim", TARGET, test_runs=("smoke",)),)


def _reservation(token: job_slots.SlotToken) -> float | None:
    """The reservation the store holds for *token*, read back from disk."""
    (held,) = (
        t for t in _store_of(token).observe(job_slots.CLASS_MEMORY)[0] if t.pid == token.pid
    )
    assert held.memory is not None
    return held.memory.reservation_mb


_STORES: dict[int, SlotStore] = {}


def _store_of(token: job_slots.SlotToken) -> SlotStore:
    return _STORES[token.pid]


def _start(root: Path, world: FakeWorld, pid: int, plan) -> tuple[SlotStore, job_slots.SlotToken]:
    """Submit *plan*'s claim as simulated process *pid*."""
    store = make_store(root, world)
    spawn(world, pid)
    token = store.submit(job_slots.CLASS_MEMORY, pid=pid, memory=plan.claim)
    _STORES[pid] = store
    return store, token


def _shrinker(plan, store: SlotStore, token: job_slots.SlotToken) -> Any:
    assert plan.claim.reservation_mb is not None
    return rp.ReservationShrinker(
        rp.PhasePlan(plan.phases, plan.claim.reservation_mb), store, token
    )


def test_sim_job_holds_build_estimate_then_run_estimate(tmp_path: Path) -> None:
    world = FakeWorld()
    plan = plan_memory_claim(SIM_REQUEST, SIM_HISTORY, BUDGET_MIB, None)
    store, token = _start(tmp_path / "slots", world, 101, plan)
    assert store.refresh(token).state == HOLDING
    with rp.shrinking(_shrinker(plan, store, token)):
        assert _reservation(token) == 8192
        rp.report_phase_end(TARGET, "sim_build")
        assert _reservation(token) == 50
        # The last planned run ends: the claim is held through reporting.
        rp.report_phase_end(TARGET, "sim_run", tests=("smoke",))
        assert _reservation(token) == 50


def test_queued_job_starts_when_build_phase_ends(tmp_path: Path) -> None:
    world = FakeWorld()
    plan = plan_memory_claim(SIM_REQUEST, SIM_HISTORY, BUDGET_MIB, None)
    root = tmp_path / "slots"
    store, token = _start(root, world, 101, plan)
    assert store.refresh(token).state == HOLDING
    spawn(world, 102)
    waiter = store.submit(
        job_slots.CLASS_MEMORY,
        pid=102,
        memory=job_slots.MemoryClaim(budget_mb=BUDGET_MIB, reservation_mb=5000),
    )
    assert store.refresh(waiter).state == QUEUED
    with rp.shrinking(_shrinker(plan, store, token)):
        rp.report_phase_end(TARGET, "sim_build")
    assert store.refresh(waiter).state == HOLDING


def _three_phase_plan(tmp_path: Path):
    """A Job of one build (800 MiB) and two runs of different tests (300 and 100 MiB)."""
    records = [
        _record("sim_build", 640),
        _record("sim_run", 240, "a"),
        _record("sim_run", 80, "b"),
    ]
    requests = (JobRequest("sim", TARGET, test_runs=("a", "b")),)
    return plan_memory_claim(requests, records, BUDGET_MIB, None)


def test_no_phase_sequence_grows_the_reservation() -> None:
    plan = _three_phase_plan(Path())
    ends = [
        (TARGET, "sim_build", ""),
        (TARGET, "sim_run", "a"),
        (TARGET, "sim_run", "b"),
        # Unplanned, unknown and repeated ends.
        (TARGET, "sim_run", "c"),
        ("other", "sim_build", ""),
        (TARGET, "sim_build", ""),
    ]
    for ordering in itertools.permutations(ends):
        phase_plan = rp.PhasePlan(plan.phases, plan.claim.reservation_mb or 0.0)
        held = [phase_plan.held_mb]
        for target, phase, test_run in ordering:
            lowered = phase_plan.finish(target, phase, test_run)
            if lowered is not None:
                phase_plan.accept(lowered)
            held.append(phase_plan.held_mb)
        assert held == sorted(held, reverse=True), ordering
        # Whatever the order, the reservation stays within the planned estimates.
        assert 100.0 <= phase_plan.held_mb <= 800.0


def test_phase_plan_counts_repeated_keys_and_ignores_unplanned_ends() -> None:
    key = HistoryKey(TARGET, "sim_run", ("x",), "t")
    build = HistoryKey(TARGET, "sim_build", ("x",), "")
    phase_plan = rp.PhasePlan([(build, 100.0), (key, 40.0), (key, 40.0)], 100.0)
    assert phase_plan.finish(TARGET, "sim_run", "other") is None
    assert phase_plan.finish(TARGET, "sim_build") == 40.0
    phase_plan.accept(40.0)
    # Two planned runs of one key: the first end leaves the key pending.
    assert phase_plan.finish(TARGET, "sim_run", "t") is None
    assert phase_plan.finish(TARGET, "sim_run", "t") is None
    # Nothing is pending any more: a further end changes nothing.
    assert phase_plan.finish(TARGET, "sim_run", "t") is None
    assert phase_plan.held_mb == 40.0


def test_non_positive_remaining_estimate_changes_nothing() -> None:
    build = HistoryKey(TARGET, "sim_build", (), "")
    run = HistoryKey(TARGET, "sim_run", (), "t")
    assert rp.PhasePlan([(build, 10.0), (run, 0.0)], 10.0).finish(TARGET, "sim_build") is None


def test_failed_store_write_keeps_the_held_value(tmp_path: Path) -> None:
    world = FakeWorld()
    plan = plan_memory_claim(SIM_REQUEST, SIM_HISTORY, BUDGET_MIB, None)
    store, token = _start(tmp_path / "slots", world, 101, plan)
    store.release(token)  # the entry is gone: the shrink is refused
    phase_plan = rp.PhasePlan(plan.phases, 8192.0)
    rp.ReservationShrinker(phase_plan, store, token).phase_ended(TARGET, "sim_build")
    assert phase_plan.held_mb == 8192.0


def test_phase_end_without_a_shrinker_is_a_no_op() -> None:
    assert rp._active.get() is None
    rp.report_phase_end(TARGET, "sim_build")
    rp.report_phase_end(TARGET, "synth", tests=("t",))


def test_only_sim_synth_fpga_plans_have_phases() -> None:
    records = [
        _record("sim_build", 80),
        _record("sim_run", 40, "t"),
        _record("sim_prepare", 10),
        _record("sim_postprocess", 10),
        _record("synth", 90),
        _record("fpga_impl", 70),
        _record("lint", 30),
        _record("echo_flow", 30),
    ]

    def phases(*requests: JobRequest, history=records) -> tuple[Any, ...]:
        return plan_memory_claim(requests, history, BUDGET_MIB, None).phases

    assert len(phases(JobRequest("sim", TARGET, test_runs=("t", "t")))) == 3
    assert len(phases(JobRequest("synth", TARGET), JobRequest("fpga", TARGET))) == 2
    assert phases(JobRequest("sim", TARGET, test_runs=("t",), flags=("coverage",))) == ()
    assert phases(JobRequest("lint", TARGET)) == ()
    assert phases(JobRequest("echo_flow", TARGET)) == ()
    # A sim Job that mixes in a lint request holds its claim to the end.
    assert phases(JobRequest("synth", TARGET), JobRequest("lint", TARGET)) == ()
    # A run-alone claim (unknown phase) has nothing to shrink.
    assert phases(JobRequest("synth", TARGET), history=[]) == ()


class _Recorder:
    """Collects the phase ends a Flow reports."""

    def __init__(self) -> None:
        self.ends: list[tuple[str, str, str]] = []

    def phase_ended(self, target: str, phase: str, tests=()) -> None:
        self.ends.append((target, phase, ",".join(tests)))


@pytest.fixture
def recorder():
    seen = _Recorder()
    with rp.shrinking(seen):  # type: ignore[arg-type]
        yield seen


def _synth_flow(role: str | None):
    from booley.flows.synth.flow import AsicSynthesizeFlow

    flow = AsicSynthesizeFlow()
    if role is not None:
        flow._execution_role = role
    return flow


@pytest.mark.parametrize(
    ("role", "reported"), [(None, True), ("candidate", True), ("baseline", False)]
)
def test_synth_target_end_is_reported_unless_baseline(
    role: str | None, reported: bool, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _synth_flow(role)
    monkeypatch.setattr(flow, "_synthesize_target", lambda _target: (None, ""))
    flow._run_single_config("a")
    assert recorder.ends == ([("a", "synth", "")] if reported else [])


def test_synth_target_end_is_reported_when_the_run_raises(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _synth_flow(None)

    def boom(_target: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(flow, "_synthesize_target", boom)
    with pytest.raises(RuntimeError):
        flow._run_single_config("a")
    assert recorder.ends == [("a", "synth", "")]


@pytest.mark.parametrize(("role", "reported"), [("candidate", True), ("baseline", False)])
def test_fpga_target_end_is_reported_unless_baseline(
    role: str, reported: bool, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.fpga.flow import FpgaImplFlow

    flow = FpgaImplFlow()
    flow._execution_role = role
    monkeypatch.setattr(flow, "_implement_target", lambda _target: SimpleNamespace())
    flow._run_single_target("a")
    assert recorder.ends == ([("a", "fpga_impl", "")] if reported else [])
