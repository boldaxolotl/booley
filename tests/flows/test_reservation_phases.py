"""A running Job's Memory Reservation shrinks as its phases finish (#3240, ADR 0087)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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


SYNTH_ENDS = [("a", phase, "") for phase in ("sv2v", "yosys", "openroad", "synth")]


# Private seam: the per-Target step runs only after Yosys configuration; its body is
# replaced to test the report around it.
def test_synth_target_end_is_reported_unless_baseline(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from booley.flows import edam
    from booley.flows.synth.flow import SynthMetrics

    cases = [
        (None, True, None),
        ("candidate", True, None),
        ("baseline", False, "raises"),
        (None, True, "raises"),
        (None, True, "lease"),
    ]
    for role, reported, failure in cases:
        recorder.ends.clear()
        flow = _synth_flow(role)
        flow.parse_args(["--target", "a", "--work-dir", str(tmp_path), "--timeout-ms", "1000"])

        def synthesize(_target: str, failure: str | None = failure):
            if failure == "raises":
                raise RuntimeError("boom")
            return SynthMetrics(), ""

        monkeypatch.setattr(flow, "_synthesize_target", synthesize)
        publish = lambda _metrics: None  # noqa: E731
        if failure == "raises":
            with pytest.raises(RuntimeError):
                flow._run_target("a", publish)
        elif failure == "lease":
            # The Flow's own lease fails after its budget: its Target still ends.
            with edam.work_root_lease(flow._synth_work_root("a"), timeout_s=0):
                flow._run_target("a", publish)
        else:
            flow._run_target("a", publish)
        assert recorder.ends == (SYNTH_ENDS if reported else []), (role, failure)


SYNTH_CORE = """CAPI=2:
name: acme:ip:chip:1.0
filesets:
  rtl:
    files: [chip.sv]
    file_type: systemVerilogSource
targets:
  physical:
    flow: generic
    flow_options: {tool: yosys}
    filesets: [rtl]
    toplevel: chip
  logical:
    flow: generic
    flow_options: {tool: yosys, synth_mode: logical}
    filesets: [rtl]
    toplevel: chip
  slang:
    flow: generic
    flow_options: {tool: yosys, frontend: slang}
    filesets: [rtl]
    toplevel: chip
  broken:
    flow: generic
    flow_options: {tool: yosys, synth_mode: fast}
    filesets: [rtl]
    toplevel: chip
"""


def test_synth_job_requests_plan_the_tools_the_target_runs(tmp_path: Path) -> None:
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.targets.catalog import TargetCatalog

    (tmp_path / "chip.sv").write_bytes(b"module chip; endmodule\n")
    (tmp_path / "chip.core").write_bytes(SYNTH_CORE.encode())
    cases = [
        ("physical", [], ("sv2v", "yosys", "openroad")),
        ("logical", [], ("sv2v", "yosys")),
        ("slang", [], ("yosys", "openroad")),
        ("physical", ["--frontend", "slang"], ("yosys", "openroad")),
        # An invalid mode plans the whole command; the run reports the error.
        ("broken", [], ()),
        # The baseline's mode and frontend are unknown until it runs: plan every tool.
        ("logical", ["--baseline", "HEAD"], ("sv2v", "yosys", "openroad")),
    ]
    for selector, argv, phases in cases:
        flow = AsicSynthesizeFlow()
        flow.parse_args(["--target", selector, "--work-dir", str(tmp_path), *argv])
        handle = TargetCatalog.build(tmp_path).select(selector, for_flow="synth")

        assert flow.memory_job_requests((handle, handle)) == (
            JobRequest("synth", selector, split_phases=phases),
        ), (selector, argv)


# Private seam: the per-Target step runs only after Vivado configuration.
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
