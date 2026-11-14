"""Memory admission of sim, synth, fpga, lint and Custom Flow Jobs (#3701, ADR 0087).

The module under test is imported inside each test body (through ``_admission``)
so a missing module fails the test itself, not its collection.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from booley.evidence.fields import BASELINE_TARGET_PARAM
from booley.flows.job_estimates import JobRequest
from booley.flows.prepared_invocation import OrdinaryRunInvocation, TargetSelectionInvocation
from booley.flows.sim.flow import PreparedSimulationEndpoint, SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.runtime import job_slots
from booley.runtime.job_slots import HOLDING, QUEUED, TokenState
from booley.runtime.resource_history import SCHEMA
from booley.targets.catalog import TargetCatalog
from tests.test_job_slots import FakeWorld, make_store, spawn

BUDGET_MIB = 1200.0


def _admission() -> ModuleType:
    return importlib.import_module("booley.flows.memory_admission")


def _record(
    target: str,
    phase: str,
    peak_mib: float,
    *,
    test_run: str = "",
    wall_s: float | None = 1.0,
) -> dict[str, Any]:
    """A history record with only the fields the estimator reads."""
    return {
        "key": {"target": target, "phase": phase, "flags": [], "test_run": test_run},
        "peak_memory_mb": peak_mib,
        "wall_s": wall_s,
    }


def _synth(*targets: str) -> tuple[JobRequest, ...]:
    return tuple(JobRequest("synth", target) for target in targets)


def _history(*peaks: tuple[str, float]) -> list[dict[str, Any]]:
    return [_record(target, "synth", peak) for target, peak in peaks]


def _plan(requests, records, budget_mib: float | None = BUDGET_MIB, error: str | None = None):
    return _admission().plan_memory_claim(requests, records, budget_mib, error)


def _join(store, world: FakeWorld, pid: int, plan):
    """Start simulated process *pid* and submit the claim of *plan*."""
    spawn(world, pid)
    return store.submit(job_slots.CLASS_MEMORY, pid=pid, memory=plan.claim)


def test_jobs_whose_reservations_fit_run_together_and_a_third_queues(tmp_path: Path) -> None:
    world = FakeWorld()
    store = make_store(tmp_path / "slots", world)
    # A 400 MiB peak is a 500 MiB estimate: two fit the 1,200 MiB budget, three do not.
    records = _history(("a", 400), ("b", 400), ("c", 400))
    plans = [_plan(_synth(target), records) for target in "abc"]

    assert [plan.claim.reservation_mb for plan in plans] == [500.0, 500.0, 500.0]
    first, second, third = (
        _join(store, world, 101 + index, plan) for index, plan in enumerate(plans)
    )

    assert store.refresh(first).state == HOLDING
    assert store.refresh(second).state == HOLDING
    assert store.refresh(third) == TokenState(QUEUED, position=0)
    store.release(first)
    assert store.refresh(third).state == HOLDING


def test_job_threads_at_start_skips_only_the_items_own_flow_token(tmp_path: Path) -> None:
    world = FakeWorld()
    store = make_store(tmp_path / "slots", world)
    records = _history(("a", 40), ("b", 40), ("c", 40))
    own_flow, other_flow, sized = (
        _join(store, world, 101 + index, _plan(_synth(target), records))
        for index, target in enumerate("abc")
    )
    assert all(store.refresh(token).state == HOLDING for token in (own_flow, other_flow, sized))
    job_threads_at_start = _admission().job_threads_at_start

    # Every holder counts for another Job: three holders on six cores.
    assert job_threads_at_start(store, 6) == 2
    # An item leaves out its own Flow's token only: two holders remain.
    assert job_threads_at_start(store, 6, own_flow=own_flow) == 3


def test_unknown_job_waits_for_an_empty_budget_runs_alone_and_blocks(tmp_path: Path) -> None:
    world = FakeWorld()
    store = make_store(tmp_path / "slots", world)
    records = _history(("a", 400))
    holder = _join(store, world, 101, _plan(_synth("a"), records))
    # A phase never recorded in the Project, and an absent request list, cannot be sized.
    unknown = _plan((JobRequest("fpga", "a"),), records)
    assert unknown.claim.run_alone and unknown.warning is None
    assert _plan(None, records).claim.run_alone
    assert _plan((), records).claim.run_alone
    waiting = _join(store, world, 102, unknown)
    assert store.refresh(holder).state == HOLDING
    assert store.refresh(waiting) == TokenState(QUEUED, position=0)

    store.release(holder)
    assert store.refresh(waiting).state == HOLDING
    # While it runs alone, even a small Job waits.
    small = _join(store, world, 103, _plan(_synth("a"), records))
    assert store.refresh(small) == TokenState(QUEUED, position=0)
    store.release(waiting)
    assert store.refresh(small).state == HOLDING


def test_only_a_job_with_its_own_wall_clock_history_jumps_ahead(tmp_path: Path) -> None:
    # (wall-clock in the jumper's history record, claim wall_s, state)
    cases = {"own": (48.0, 60.0, HOLDING), "borrowed": (None, None, QUEUED)}
    for name, (jumper_wall_s, expected_wall_s, state) in cases.items():
        world = FakeWorld()
        store = make_store(tmp_path / name, world)
        # Estimates: holder 1,000 MiB for 600 s, front 500 MiB (blocked), jumper 100 MiB.
        records = [
            _record("holder", "synth", 800, wall_s=480.0),
            _record("front", "synth", 400, wall_s=480.0),
            _record("jumper", "synth", 80, wall_s=jumper_wall_s),
        ]
        plans = [_plan(_synth(target), records) for target in ("holder", "front", "jumper")]
        assert plans[2].claim.wall_s == expected_wall_s, name
        holder, front, jumper = (
            _join(store, world, 101 + index, plan) for index, plan in enumerate(plans)
        )

        assert store.refresh(holder).state == HOLDING, name
        assert store.refresh(front).state == QUEUED, name
        assert store.refresh(jumper).state == state, name


def test_job_larger_than_the_budget_runs_alone_with_a_warning(tmp_path: Path) -> None:
    # A 2,000 MiB peak is a 2,500 MiB estimate against a 1,000 MiB budget.
    plan = _plan(_synth("big"), _history(("big", 2000)), budget_mib=1000.0)

    assert plan.claim.run_alone
    assert plan.warning == (
        "Memory estimate 2500 MiB exceeds the Memory Budget of 1000 MiB; this Job ran alone."
    )
    world = FakeWorld()
    store = make_store(tmp_path / "slots", world)
    token = _join(store, world, 101, plan)
    assert store.refresh(token).state == HOLDING


def test_job_with_a_zero_peak_runs_alone() -> None:
    """``MemoryClaim`` refuses a reservation of 0, so such a Job cannot hold a share."""
    plan = _plan(_synth("a"), _history(("a", 0)))

    assert plan.claim.run_alone and plan.warning is None


def test_job_with_an_unestimated_phase_runs_alone() -> None:
    """One known phase and one unknown phase: the Job still needs the whole budget."""
    request = JobRequest("sim", "t", test_runs=("x",))
    plan = _plan((request,), [_record("t", "sim_build", 100)])

    assert plan.claim.run_alone and plan.warning is None


def test_unavailable_budget_runs_alone_with_a_warning() -> None:
    plan = _plan(_synth("a"), _history(("a", 400)), budget_mib=None, error="no MemTotal")

    assert plan.claim.run_alone
    assert plan.warning == "Memory Budget unavailable (no MemTotal); this Job ran alone."


def _booley_dir(tmp_path: Path, *, budget: str | None = "1200m") -> Path:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    if budget is not None:
        (booley_dir / "booley.toml").write_text(f'[jobs]\nmemory_budget = "{budget}"\n')
    return booley_dir


def _write_history(booley_dir: Path, document: object) -> None:
    path = booley_dir / "runtime" / "resources" / "history.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _measure(booley_dir: Path | None, requests, config: dict[str, object]):
    return _admission().measure_memory_claim(booley_dir, config, requests)


def test_measure_reads_the_history_and_the_configured_budget(tmp_path: Path) -> None:
    booley_dir = _booley_dir(tmp_path)
    _write_history(booley_dir, {"$schema": SCHEMA, "records": _history(("a", 400))})

    plan = _measure(booley_dir, _synth("a"), {"jobs": {"memory_budget": "1200m"}})

    assert (plan.claim.reservation_mb, plan.claim.budget_mb) == (500.0, 1200.0)


def test_unreadable_history_reads_as_empty_and_the_job_runs_alone(tmp_path: Path) -> None:
    booley_dir = _booley_dir(tmp_path)
    _write_history(booley_dir, {"$schema": "foreign", "records": []})
    config: dict[str, object] = {"jobs": {"memory_budget": "1200m"}}

    assert _measure(booley_dir, _synth("a"), config).claim.run_alone
    # No Booley Directory has no history at all.
    assert _measure(None, _synth("a"), config).claim.run_alone


def test_invalid_budget_configuration_runs_alone_with_a_warning(tmp_path: Path) -> None:
    plan = _measure(None, _synth("a"), {"jobs": {"memory_budget": "lots"}})

    assert plan.claim.run_alone
    assert plan.warning is not None and plan.warning.startswith("Memory Budget unavailable (")
    assert "memory_budget" in plan.warning


# --- request producers -------------------------------------------------------------


_PAIR_CORE = """CAPI=2:
name: acme:lib:toy:1.0
targets:
  synth_before: {flow: generic, flow_options: {tool: yosys}}
  synth_after: {flow: generic, flow_options: {tool: yosys}}
"""


def _synth_flow(tmp_path: Path, baseline: str | None):
    """A synth Flow over ``synth_after`` whose Criterion pairs it with *baseline*."""
    (tmp_path / "toy.core").write_text(_PAIR_CORE, encoding="utf-8")
    handle = TargetCatalog.build(tmp_path).select("synth_after", for_flow="synth")
    params = {} if baseline is None else {BASELINE_TARGET_PARAM: baseline}
    flow = AsicSynthesizeFlow()
    flow._state = SimpleNamespace(  # type: ignore[assignment]
        criteria={"synthesis_ok_synth_after": SimpleNamespace(params=params)}
    )
    return flow, handle


def test_synth_reservation_covers_a_paired_baseline_target(tmp_path: Path) -> None:
    flow, handle = _synth_flow(tmp_path, "synth_before")

    requests = flow.memory_job_requests((handle,))

    assert requests == _synth("synth_after", "synth_before")
    # The baseline half is the larger: 2,000 MiB x 1.25, not 400 MiB x 1.25.
    plan = _plan(requests, _history(("synth_after", 400), ("synth_before", 2000)), 4000.0)
    assert plan.claim.reservation_mb == 2500.0


def test_unpaired_target_is_its_own_baseline_and_requested_once(tmp_path: Path) -> None:
    flow, handle = _synth_flow(tmp_path, None)

    assert flow.memory_job_requests((handle,)) == _synth("synth_after")


def test_invalid_pairing_makes_the_job_unknown(tmp_path: Path) -> None:
    flow, handle = _synth_flow(tmp_path, "")

    assert flow.memory_job_requests((handle,)) is None


def _selection(handle: Any, *, matches: bool = True) -> Any:
    return SimpleNamespace(handles=(handle,), matches=lambda *_args: matches)


def test_target_selection_invocation_sizes_its_prepared_targets(tmp_path: Path) -> None:
    flow, handle = _synth_flow(tmp_path, None)
    flow._args = SimpleNamespace(work_dir=tmp_path, target="synth_after")

    def invocation(selection: Any) -> TargetSelectionInvocation:
        return TargetSelectionInvocation(flow, selection, flow._run)

    assert invocation(_selection(handle)).job_requests() == _synth("synth_after")
    # No selection, or one for another request, is not a known footprint.
    assert invocation(None).job_requests() is None
    assert invocation(_selection(handle, matches=False)).job_requests() is None


def test_lint_requests_every_target_it_lints() -> None:
    from booley.flows.job_estimates import HistoryKey, phase_keys
    from booley.flows.lint.flow import LintFlow
    from tests.target_test_support import make_target_handle

    handles = tuple(make_target_handle("/work", name, drivable_by=("lint",)) for name in "ab")

    requests = LintFlow().memory_job_requests(handles)

    assert requests == (JobRequest("lint", "a"), JobRequest("lint", "b"))
    assert phase_keys(requests[0]) == (HistoryKey("a", "lint", (), ""),)


def test_custom_flow_invocation_sizes_its_flow_phase() -> None:
    import argparse

    from booley.flows.base import BooleyFlow
    from booley.flows.job_estimates import HistoryKey, phase_keys
    from booley.runtime.endpoint_execution import EndpointOutcome

    class EchoFlow(BooleyFlow):
        name = "echo_flow"
        description = "Custom Flow for sizing tests"

        def _add_args(self, parser: argparse.ArgumentParser) -> None:
            """This Flow takes no options of its own."""

        def _run(self) -> EndpointOutcome:
            return EndpointOutcome()

    # With and without a Target: the request names the one given, else the empty Target.
    for target in ("echo", ""):
        flow = EchoFlow()
        flow._args = SimpleNamespace(target=target)  # type: ignore[assignment]

        invocation = flow.prepare_endpoint_invocation()

        assert isinstance(invocation, OrdinaryRunInvocation), target
        assert invocation.job_requests() == (JobRequest("echo_flow", target),), target
        # The key the recorder writes for an unlabelled Custom Flow command.
        (request,) = invocation.job_requests() or ()
        assert phase_keys(request) == (HistoryKey(target, "echo_flow", (), ""),), target


def test_ordinary_run_invocation_is_unknown() -> None:
    assert OrdinaryRunInvocation(lambda: None).job_requests() is None  # type: ignore[arg-type,return-value]


def _sim_flow(**args: Any) -> SimulateFlow:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(coverage=False, trace=False, work_dir=Path(), **args)
    return flow


def test_elab_only_sim_job_runs_no_simulator_process(tmp_path: Path) -> None:
    flow = _sim_flow()
    flow.args.coverage = True  # type: ignore[union-attr]
    handle = SimpleNamespace(selector="top")

    requests = flow.memory_job_requests((handle,))  # type: ignore[arg-type]

    assert requests == (JobRequest("sim", "top", test_runs=(), flags=("coverage",)),)


def _prepared(
    *, selected: tuple[str, ...] = ("top",), baselines: tuple[str, ...] = (), resume: object = None
) -> PreparedSimulationEndpoint:
    handles = tuple(SimpleNamespace(selector=name) for name in (*selected, *baselines))
    return PreparedSimulationEndpoint(handles, resume, selected, {"top": ["smoke"]})  # type: ignore[arg-type]


def _resolve(monkeypatch: pytest.MonkeyPatch, flow: SimulateFlow) -> list[str]:
    """Replace Test Run resolution with a recorded stand-in; returns the Targets asked."""
    asked: list[str] = []
    monkeypatch.setattr(
        flow, "_registered_test_plan", lambda target, _map: SimpleNamespace(target=target)
    )
    monkeypatch.setattr(
        flow, "_resolve_test_runs", lambda target, _map: asked.append(target) or ()
    )
    monkeypatch.setattr(
        "booley.flows.sim.flow.sim_job_request",
        lambda plan, runs, *, flags: JobRequest(
            "sim", plan.target, test_runs=("smoke",), flags=flags
        ),
    )
    return asked


def test_simulation_record_is_sized_from_its_resolved_test_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _sim_flow()
    asked = _resolve(monkeypatch, flow)

    # A baseline on a selected Target re-runs that Target's keys: no extra request.
    requests = flow.simulation_record_job_requests(_prepared(baselines=("top",)))

    assert requests == (JobRequest("sim", "top", test_runs=("smoke",)),)
    assert asked == ["top"]


def test_sim_job_with_unresolved_runs_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume, and a Cycle Count baseline on another Target, have no Test Runs yet."""
    flow = _sim_flow()
    asked = _resolve(monkeypatch, flow)

    assert flow.simulation_record_job_requests(_prepared(resume=object())) is None
    assert flow.simulation_record_job_requests(_prepared(baselines=("other",))) is None
    assert asked == []


def test_simulation_record_that_cannot_resolve_its_test_runs_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _sim_flow()

    def refuse(*_args: Any) -> Any:
        raise ValueError("no such Test")

    monkeypatch.setattr(flow, "_resolve_test_runs", refuse)
    monkeypatch.setattr(
        flow, "_registered_test_plan", lambda *_args: SimpleNamespace(target="top")
    )

    assert flow.simulation_record_job_requests(_prepared()) is None


def test_work_item_claim_reserves_its_build_only_when_it_builds() -> None:
    """A work item reserves its run; its build too only when it builds inside itself (#3772)."""
    records = [
        _record("top", "sim_prepare", 50),
        _record("top", "sim_build", 400),
        _record("top", "sim_run", 100, test_run="smoke"),
    ]
    request = JobRequest("sim", "top", test_runs=("smoke",))

    def size(request, *, build, records=records, budget=BUDGET_MIB, error=None):
        return _admission().plan_work_item_claim(
            request, build=build, records=records, budget_mb=budget, budget_error=error
        )

    settled, _ = size(request, build=False)
    building, _ = size(request, build=True)
    assert (settled.reservation_mb, settled.run_alone) == (125.0, False)
    assert (building.reservation_mb, building.run_alone) == (500.0, False)
    # An item whose Target does not resolve, or whose run has no estimate, runs alone.
    unknown, no_warning = size(None, build=False)
    assert (unknown, no_warning) == (
        job_slots.MemoryClaim(budget_mb=BUDGET_MIB, run_alone=True),
        None,
    )
    unestimated, _ = size(
        request,
        build=False,
        records=[_record("top", "sim_prepare", 50), _record("top", "sim_build", 400)],
    )
    assert unestimated.run_alone
    # Over the budget it runs alone, with the warning a Job gets; without a budget too.
    huge = [_record("top", "sim_prepare", 50), _record("top", "sim_run", 2000, test_run="smoke")]
    over, warning = size(request, build=False, records=huge)
    assert over.run_alone
    assert warning == (
        "Memory estimate 2500 MiB exceeds the Memory Budget of 1200 MiB; this Job ran alone."
    )
    _, unavailable = size(request, build=False, budget=None, error="no MemTotal")
    assert unavailable == "Memory Budget unavailable (no MemTotal); this Job ran alone."


def test_settled_work_item_reserves_sim_prepare_with_its_run() -> None:
    """A work item reusing a settled build still runs FuseSoC setup, so it reserves it (#3787)."""
    request = JobRequest("sim", "top", test_runs=("smoke",))

    def size(records):
        claim, _ = _admission().plan_work_item_claim(
            request, build=False, records=records, budget_mb=BUDGET_MIB, budget_error=None
        )
        return claim

    with_setup = size(
        [
            _record("top", "sim_prepare", 300),
            _record("top", "sim_build", 400),
            _record("top", "sim_run", 100, test_run="smoke"),
        ]
    )
    # The largest of setup (300) and run (100), plus the 25 % margin; the build is not reserved.
    assert (with_setup.reservation_mb, with_setup.run_alone) == (375.0, False)
    # No setup history yet: the unknown-phase rule applies and the item runs alone.
    without_setup = size(
        [_record("top", "sim_build", 400), _record("top", "sim_run", 100, test_run="smoke")]
    )
    assert without_setup.run_alone


def test_work_item_request_resolves_each_target_plan_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _sim_flow()
    flow.begin_invocation()
    plans: list[str] = []

    def plan_of(selector: str) -> Any:
        plans.append(selector)
        if selector == "gone":
            raise ValueError("no such Target")
        return SimpleNamespace(target=selector)

    monkeypatch.setattr(flow, "_target_test_plan", plan_of)
    monkeypatch.setattr(
        "booley.flows.sim.flow.sim_job_request",
        lambda plan, runs, *, flags: JobRequest(
            "sim", plan.target, test_runs=tuple(run.name for run in runs), flags=flags
        ),
    )
    runs = (SimpleNamespace(name="smoke"),)

    first = flow.work_item_job_request("top", runs, ("trace",))  # type: ignore[arg-type]
    second = flow.work_item_job_request("top", runs, ())  # type: ignore[arg-type]

    assert first == JobRequest("sim", "top", test_runs=("smoke",), flags=("trace",))
    assert second == JobRequest("sim", "top", test_runs=("smoke",))
    assert plans == ["top"]
    # A Target that does not resolve is unknown, not an error: its item runs alone.
    assert flow.work_item_job_request("gone", runs, ()) is None  # type: ignore[arg-type]


def test_job_thread_count_prefers_the_admission_then_the_inherited_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission()
    monkeypatch.delenv(admission.JOB_THREADS_ENV, raising=False)
    assert admission.job_thread_count() is None

    monkeypatch.setenv(admission.JOB_THREADS_ENV, "3")
    assert admission.job_thread_count() == 3
    with admission.fixed_job_threads(5):
        assert admission.job_thread_count() == 5
    assert admission.job_thread_count() == 3

    # Only Booley writes the variable: a malformed value is a defect, not ignored.
    monkeypatch.setenv(admission.JOB_THREADS_ENV, "many")
    with pytest.raises(ValueError, match="many"):
        admission.job_thread_count()
