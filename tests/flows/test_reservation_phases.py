"""A running Job's Memory Reservation shrinks as its phases finish (#3240, ADR 0087)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from booley.flows import reservation_phases as rp
from booley.flows.job_estimates import JobRequest
from booley.flows.memory_admission import plan_memory_claim
from booley.runtime import job_slots
from booley.runtime.job_slots import HOLDING, QUEUED, SlotStore
from booley.runtime.resource_history import HistoryKey
from tests.test_job_slots import FakeWorld, make_store, spawn

BUDGET_MIB = 10_000.0
TARGET = "core"


def _record(phase: str, peak_mib: float, test_run: str = "") -> dict[str, Any]:
    return {
        "key": {"target": TARGET, "phase": phase, "flags": [], "test_run": test_run},
        "peak_memory_mb": peak_mib,
        "wall_s": 1.0,
    }


# History peaks of 6,553.6 and 40 MiB are 8,192 and 50 MiB with the estimate margin.
# FuseSoC setup (8 MiB, 10 with the margin) is a planned phase of every sim Job but never
# reports its end, so it stays reserved until the Job ends (#3787).
SIM_HISTORY = [
    _record("sim_prepare", 8),
    _record("sim_build", 6553.6),
    _record("sim_run", 40, "smoke"),
]
SIM_REQUEST = (JobRequest("sim", TARGET, test_runs=("smoke",)),)


def _reservation(store: SlotStore, token: job_slots.SlotToken) -> float | None:
    """The reservation *store* holds for *token*, read back from disk."""
    (held,) = (t for t in store.observe(job_slots.CLASS_MEMORY)[0] if t.pid == token.pid)
    assert held.memory is not None
    return held.memory.reservation_mb


def _start(root: Path, world: FakeWorld, pid: int, plan) -> tuple[SlotStore, job_slots.SlotToken]:
    """Submit *plan*'s claim as simulated process *pid*."""
    store = make_store(root, world)
    spawn(world, pid)
    token = store.submit(job_slots.CLASS_MEMORY, pid=pid, memory=plan.claim)
    return store, token


def _shrinker(plan, store: SlotStore, token: job_slots.SlotToken) -> Any:
    assert plan.claim.reservation_mb is not None
    return rp.ReservationShrinker(
        rp.PhasePlan(plan.phases, plan.claim.reservation_mb), store, token
    )


def test_sim_job_holds_build_estimate_then_run_estimate(tmp_path: Path) -> None:
    world = FakeWorld()
    plan = _sim_plan()
    store, token = _start(tmp_path / "slots", world, 101, plan)
    assert store.refresh(token).state == HOLDING
    with rp.shrinking(_shrinker(plan, store, token)):
        assert _reservation(store, token) == 8192
        rp.report_phase_end(TARGET, "sim_build")
        assert _reservation(store, token) == 50
        # The last planned run ends. Setup never reports its end, so its estimate
        # (10 MiB) stays pending and is what the Job holds through reporting.
        rp.report_phase_end(TARGET, "sim_run", tests=("smoke",))
        assert _reservation(store, token) == 10


def test_queued_job_starts_when_build_phase_ends(tmp_path: Path) -> None:
    world = FakeWorld()
    plan = _sim_plan()
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


def _sim_plan():
    return plan_memory_claim(SIM_REQUEST, SIM_HISTORY, BUDGET_MIB, None)


def _three_phase_plan(tmp_path: Path):
    """A Job of one build (800 MiB) and two runs of different tests (300 and 100 MiB)."""
    records = [
        _record("sim_prepare", 8),
        _record("sim_build", 640),
        _record("sim_run", 240, "a"),
        _record("sim_run", 80, "b"),
    ]
    requests = (JobRequest("sim", TARGET, test_runs=("a", "b")),)
    return plan_memory_claim(requests, records, BUDGET_MIB, None)


@pytest.mark.parametrize(
    "requests",
    [
        (JobRequest("sim", TARGET, test_runs=("a",), flags=("coverage",)),),
        (JobRequest("lint", TARGET),),
        (JobRequest("sim", TARGET, test_runs=("a",)), JobRequest("lint", TARGET)),
    ],
    ids=["coverage", "lint", "sim-with-lint"],
)
def test_claims_that_cannot_shrink_have_no_phases(requests) -> None:
    records = [
        _record(phase, 30, "a" if phase == "sim_run" else "")
        for phase in ("sim_prepare", "sim_build", "sim_run", "sim_postprocess", "lint")
    ]
    assert plan_memory_claim(requests, records, BUDGET_MIB, None).phases == ()


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
    plan = _sim_plan()
    store, token = _start(tmp_path / "slots", world, 101, plan)
    store.release(token)  # the entry is gone: the shrink is refused
    phase_plan = rp.PhasePlan(plan.phases, 8192.0)
    rp.ReservationShrinker(phase_plan, store, token).phase_ended(TARGET, "sim_build")
    assert phase_plan.held_mb == 8192.0


def test_phase_end_without_a_shrinker_is_a_no_op() -> None:
    rp.report_phase_end(TARGET, "sim_build")
    rp.report_phase_end(TARGET, "synth", tests=("t",))


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


# Private seam: the per-Target step runs only after Yosys configuration; its body is
# replaced to test the report around it.
@pytest.mark.parametrize(
    ("role", "reported", "raises"),
    [
        (None, True, False),
        ("candidate", True, False),
        ("baseline", False, False),
        (None, True, True),
    ],
)
def test_synth_target_end_is_reported_unless_baseline(
    role: str | None,
    reported: bool,
    raises: bool,
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _synth_flow(role)

    def synthesize(_target: str):
        if raises:
            raise RuntimeError("boom")
        return None, ""

    monkeypatch.setattr(flow, "_synthesize_target", synthesize)
    if raises:
        with pytest.raises(RuntimeError):
            flow._run_single_config("a")
    else:
        flow._run_single_config("a")
    assert recorder.ends == ([("a", "synth", "")] if reported else [])


# Private seam: the per-Target step runs only after Vivado configuration.
@pytest.mark.parametrize(("role", "reported"), [("candidate", True), ("baseline", False)])
def test_fpga_target_end_is_reported_unless_baseline(
    role: str, reported: bool, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.fpga.flow import FpgaImplFlow, FpgaMetrics

    flow = FpgaImplFlow()
    flow._execution_role = role
    monkeypatch.setattr(flow, "_implement_target", lambda _target: FpgaMetrics())
    flow._run_single_target("a")
    assert recorder.ends == ([("a", "fpga_impl", "")] if reported else [])
