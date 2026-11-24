"""Memory admission of sim, synth, fpga, lint and Custom Flow Jobs (#3701, ADR 0087)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows import memory_admission as admission
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
    return admission.plan_memory_claim(requests, records, budget_mib, error)


@pytest.fixture
def slots(tmp_path: Path) -> tuple[FakeWorld, Any]:
    """A simulated process table and the slot store over it."""
    world = FakeWorld()
    return world, make_store(tmp_path / "slots", world)


def _join(store, world: FakeWorld, pid: int, plan):
    """Start simulated process *pid* and submit the claim of *plan*."""
    spawn(world, pid)
    return store.submit(job_slots.CLASS_MEMORY, pid=pid, memory=plan.claim)


def test_job_threads_at_start_skips_only_the_items_own_flow_token(
    slots: tuple[FakeWorld, Any],
) -> None:
    world, store = slots
    records = _history(("a", 40), ("b", 40), ("c", 40))
    own_flow, other_flow, sized = (
        _join(store, world, 101 + index, _plan(_synth(target), records))
        for index, target in enumerate("abc")
    )
    assert all(store.refresh(token).state == HOLDING for token in (own_flow, other_flow, sized))
    job_threads_at_start = admission.job_threads_at_start

    # Every holder counts for another Job: three holders on six cores.
    assert job_threads_at_start(store, 6) == 2
    # An item leaves out its own Flow's token only: two holders remain.
    assert job_threads_at_start(store, 6, own_flow=own_flow) == 3


def test_unknown_job_waits_for_an_empty_budget_runs_alone_and_blocks(
    slots: tuple[FakeWorld, Any],
) -> None:
    world, store = slots
    records = _history(("a", 400))
    holder = _join(store, world, 101, _plan(_synth("a"), records))
    # A phase never recorded in the Project, and an absent request list, cannot be sized.
    unknown = _plan((JobRequest("lint", "a"),), records)
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


_OVER = "Memory estimate 2500 MiB exceeds the Memory Budget of 1000 MiB; this Job ran alone."
_NO_BUDGET = "Memory Budget unavailable (no MemTotal); this Job ran alone."
_UNESTIMATED = (JobRequest("sim", "t", test_runs=("x",)),)
_NOTE = (
    "No memory measurement yet for {}; this Job ran alone, "
    "and later runs are sized from its measurement."
)


# A 2,000 MiB peak is a 2,500 MiB estimate against 1,000 MiB. ``MemoryClaim`` refuses a
# reservation of 0, so a zero peak cannot hold a share. One known and one unknown phase:
# the Job still needs the whole budget. A size-dependent phase never borrows another
# Target's peak (#4536): its Target's first run runs alone and says so.
@pytest.mark.parametrize(
    ("requests", "records", "budget", "error", "warning", "unmeasured"),
    [
        (_synth("big"), _history(("big", 2000)), 1000.0, None, _OVER, None),
        (_synth("a"), _history(("a", 0)), BUDGET_MIB, None, None, None),
        (_UNESTIMATED, [_record("t", "sim_build", 100)], BUDGET_MIB, None, None, None),
        (_synth("a"), _history(("a", 400)), None, "no MemTotal", _NO_BUDGET, None),
        (
            _synth("chip"),
            _history(("block", 400)),
            BUDGET_MIB,
            None,
            _NOTE.format("synth on Target chip"),
            "synth on Target chip",
        ),
        (
            (JobRequest("fpga", "chip"),),
            [_record("block", "fpga_impl", 400)],
            BUDGET_MIB,
            None,
            _NOTE.format("fpga_impl on Target chip"),
            "fpga_impl on Target chip",
        ),
        (
            _synth("big", "chip"),
            _history(("big", 2000)),
            1000.0,
            None,
            f"{_OVER}\n{_NOTE.format('synth on Target chip')}",
            "synth on Target chip",
        ),
    ],
    ids=[
        "larger-than-budget",
        "zero-peak",
        "unestimated-phase",
        "unavailable-budget",
        "unmeasured-synth-target",
        "unmeasured-fpga-target",
        "over-budget-and-unmeasured",
    ],
)
def test_job_without_a_fitting_estimate_runs_alone(
    requests: tuple[JobRequest, ...],
    records: list[dict[str, Any]],
    budget: float | None,
    error: str | None,
    warning: str | None,
    unmeasured: str | None,
) -> None:
    plan = _plan(requests, records, budget, error)

    assert plan.claim.run_alone
    assert plan.warning == warning
    assert plan.claim.unmeasured == unmeasured


def test_job_larger_than_the_budget_holds_the_store_alone(slots: tuple[FakeWorld, Any]) -> None:
    world, store = slots
    plan = _plan(_synth("big"), _history(("big", 2000)), budget_mib=1000.0)

    assert store.refresh(_join(store, world, 101, plan)).state == HOLDING


def _booley_dir(tmp_path: Path) -> Path:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    return booley_dir


def _write_history(booley_dir: Path, document: object) -> None:
    path = booley_dir / "runtime" / "resources" / "history.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _measure(booley_dir: Path | None, requests):
    return admission.measure_memory_claim(booley_dir, requests)


def test_measure_reads_the_history_and_the_pinned_budget(
    tmp_path: Path, pin_memory_budget: Callable[[int], None]
) -> None:
    pin_memory_budget(1200)
    booley_dir = _booley_dir(tmp_path)
    _write_history(booley_dir, {"$schema": SCHEMA, "records": _history(("a", 400))})

    plan = _measure(booley_dir, _synth("a"))

    assert (plan.claim.reservation_mb, plan.claim.budget_mb) == (500.0, 1200.0)


def test_unreadable_history_reads_as_empty_and_the_job_runs_alone(
    tmp_path: Path, pin_memory_budget: Callable[[int], None]
) -> None:
    pin_memory_budget(1200)
    booley_dir = _booley_dir(tmp_path)
    _write_history(booley_dir, {"$schema": "foreign", "records": []})

    assert _measure(booley_dir, _synth("a")).claim.run_alone
    # No Booley Directory has no history at all.
    assert _measure(None, _synth("a")).claim.run_alone


def test_unreadable_memory_runs_alone_with_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(**_: object) -> object:
        raise OSError("MemTotal unreadable")

    monkeypatch.setattr(admission, "compute_memory_budget", unreadable)

    plan = _measure(None, _synth("a"))

    assert plan.claim.run_alone
    assert plan.warning == "Memory Budget unavailable (MemTotal unreadable); this Job ran alone."


# --- request producers -------------------------------------------------------------


_PAIR_CORE = """CAPI=2:
name: acme:lib:toy:1.0
targets:
  synth_before: {flow: generic, flow_options: {tool: yosys}}
  synth_after: {flow: generic, flow_options: {tool: yosys}}
"""


def _synth_flow(tmp_path: Path):
    """A synth Flow and its ``synth_after`` Target handle."""
    (tmp_path / "toy.core").write_text(_PAIR_CORE, encoding="utf-8")
    handle = TargetCatalog.build(tmp_path).select("synth_after", for_flow="synth")
    return AsicSynthesizeFlow(), handle


def _selection(handle: Any, *, matches: bool = True) -> Any:
    return SimpleNamespace(handles=(handle,), matches=lambda *_args: matches)


def test_target_selection_invocation_sizes_its_prepared_targets(tmp_path: Path) -> None:
    flow, handle = _synth_flow(tmp_path)
    flow._args = SimpleNamespace(
        work_dir=tmp_path, target="synth_after", baseline=None, frontend=None
    )

    def invocation(selection: Any) -> TargetSelectionInvocation:
        # Private seam: the Flow's run step is the callable the invocation wraps.
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


def _resolve(
    monkeypatch: pytest.MonkeyPatch, flow: SimulateFlow, *, refuse: bool = False
) -> list[str]:
    """Replace Test Run resolution with a recorded stand-in; returns the Targets asked."""
    asked: list[str] = []

    def resolve(target: str, _map: Any) -> tuple[()]:
        if refuse:
            raise ValueError("no such Test")
        asked.append(target)
        return ()

    # Private seam: resolving Test Runs reads a registered test plan from a real
    # Simulation Target; the stand-in fixes what resolution returns.
    monkeypatch.setattr(
        flow, "_registered_test_plan", lambda target, _map: SimpleNamespace(target=target)
    )
    monkeypatch.setattr(flow, "_resolve_test_runs", resolve)
    monkeypatch.setattr(
        "booley.flows.sim.flow.sim_job_request",
        lambda plan, runs, *, flags, pre_sim_commands=False: JobRequest(
            "sim",
            plan.target,
            test_runs=("smoke",),
            flags=flags,
            pre_sim_commands=pre_sim_commands,
        ),
    )
    return asked


@pytest.mark.parametrize(
    ("prepared", "refuse", "expected", "asked"),
    [
        # A baseline on a selected Target re-runs that Target's keys: no extra request.
        pytest.param(
            _prepared(baselines=("top",)),
            False,
            (JobRequest("sim", "top", test_runs=("smoke",)),),
            ["top"],
            id="resolved",
        ),
        # A resume has no Test Runs yet.
        pytest.param(_prepared(resume=object()), False, None, [], id="resume"),
        pytest.param(_prepared(), True, None, [], id="unresolvable"),
    ],
)
def test_simulation_record_is_sized_from_its_resolved_test_runs(
    monkeypatch: pytest.MonkeyPatch,
    prepared: PreparedSimulationEndpoint,
    refuse: bool,
    expected: tuple[JobRequest, ...] | None,
    asked: list[str],
) -> None:
    flow = _sim_flow()
    seen = _resolve(monkeypatch, flow, refuse=refuse)

    assert flow.simulation_record_job_requests(prepared) == expected
    assert seen == asked


def test_work_item_claim_reserves_its_build_only_when_it_builds() -> None:
    """A work item reserves its run; its build too only when it builds inside itself (#3772)."""
    records = [
        _record("top", "sim_prepare", 50),
        _record("top", "sim_build", 400),
        _record("top", "sim_run", 100, test_run="smoke"),
    ]
    request = JobRequest("sim", "top", test_runs=("smoke",))

    def size(request, *, build, records=records, budget=BUDGET_MIB, error=None):
        return admission.plan_work_item_claim(
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
        claim, _ = admission.plan_work_item_claim(
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

    # Private seam: resolving Test Runs reads a registered test plan from a real
    # Simulation Target; the stand-in fixes what resolution returns.
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


def _specialist_record(name: str, peak_mib: float | None) -> dict[str, Any]:
    """A Specialist's history record: the Target key fields plus the Specialist's name."""
    record = _record("", f"specialist:{name}", 0.0)
    record["key"]["specialist"] = name
    record["peak_memory_mb"] = peak_mib
    return record


def test_specialist_claim_reserves_its_own_history() -> None:
    records = [
        _specialist_record("reviewer", 300),
        _specialist_record("reviewer", 346),
        _specialist_record("reviewer", None),  # an unmeasured run says nothing
        _specialist_record("coverage_analyst", 900),
        _record("a", "synth", 2000),
    ]

    plan = admission.plan_specialist_claim("reviewer", records, BUDGET_MIB)

    # 346 MiB x the estimator's 1.25 margin.
    assert plan.claim == job_slots.MemoryClaim(
        budget_mb=BUDGET_MIB, reservation_mb=432.5, companion=True
    )
    assert plan.warning is None and plan.phases == ()


def test_specialist_claim_without_history_reserves_the_default() -> None:
    # The largest peak measured in #4067 (362 MiB), with the estimate margin.
    records = [_specialist_record("reviewer", None), _record("a", "synth", 900)]
    for name, history in (("reviewer", records), ("coverage_analyst", [])):
        plan = admission.plan_specialist_claim(name, history, 4000.0)
        assert plan.claim == job_slots.MemoryClaim(
            budget_mb=4000.0, reservation_mb=452.5, companion=True
        )


@pytest.mark.parametrize(
    ("swap_bytes", "synth_mib", "specialist_mib"),
    [
        pytest.param(4 * 1024**3, 1100.0, 440.0, id="swap"),
        pytest.param(0, 1250.0, 500.0, id="no-swap"),
    ],
)
def test_estimate_margin_follows_the_sandbox_swap_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_bytes: int,
    synth_mib: float,
    specialist_mib: float,
) -> None:
    from tests.memory_budget_support import budget_of

    monkeypatch.setattr(
        admission,
        "compute_memory_budget",
        lambda **_: budget_of(8000 * 1024**2, swap_bytes=swap_bytes),
    )
    booley_dir = _booley_dir(tmp_path)
    specialist = _specialist_record("reviewer", 346)
    specialist["peak_memory_with_swap_mb"] = 400.0  # swapped 54 MiB out
    records = [*_history(("a", 1000)), _record("", "sim_build", 1000), specialist]
    _write_history(booley_dir, {"$schema": SCHEMA, "records": records})

    assert _measure(booley_dir, _synth("a")).claim.reservation_mb == pytest.approx(synth_mib)
    sized = admission.MemorySizing(booley_dir).plan_work_item(JobRequest("synth", "a"), build=True)
    assert sized.reservation_mb == pytest.approx(synth_mib)
    specialist_claim = admission.measure_specialist_claim(booley_dir, "reviewer").claim
    assert specialist_claim.reservation_mb == pytest.approx(specialist_mib)


def test_specialist_and_coordinator_claims_stay_bounded() -> None:
    huge = [_specialist_record("reviewer", 4000)]
    # (records, budget MiB, claim budget, reservation)
    cases: list[tuple[list[dict[str, Any]], float | None, float, float]] = [
        ([], None, 452.5, 452.5),  # budget unreadable
        (huge, None, 452.5, 452.5),  # unreadable budget ignores history
        (huge, 4000.0, 4000.0, 452.5),  # estimate over budget
        ([], 452.5, 452.5, 226.25),  # budget equal to the default
        ([], 300.0, 300.0, 150.0),  # budget below the default
        ([_specialist_record("reviewer", 200)], 300.0, 300.0, 150.0),  # history capped
        ([], 0.0, 0.0, 0.0),  # budget zero
    ]
    for records, budget_mib, claim_budget, reservation in cases:
        claim = admission.plan_specialist_claim("reviewer", records, budget_mib).claim

        assert (claim.budget_mb, claim.reservation_mb) == (claim_budget, reservation)
        assert claim.companion and not claim.run_alone
        if budget_mib:
            assert claim.reservation_mb is not None and claim.reservation_mb < budget_mib
        coordinator = admission.plan_coordinator_claim(budget_mib).claim
        assert coordinator == job_slots.MemoryClaim(
            budget_mb=budget_mib or 0.0, reservation_mb=0.0, companion=True
        )


def test_job_threads_leave_companion_holders_out(slots: tuple[FakeWorld, Any]) -> None:
    world, store = slots
    records = _history(("a", 40), ("b", 40))
    tokens = [
        _join(store, world, 101 + index, _plan(_synth(target), records))
        for index, target in enumerate("ab")
    ]
    for index in range(2):
        spawn(world, 201 + index)
        claim = admission.plan_specialist_claim("reviewer", [], BUDGET_MIB).claim
        tokens.append(store.submit(job_slots.CLASS_MEMORY, pid=201 + index, memory=claim))
    assert [store.refresh(token).state for token in tokens] == [HOLDING] * 4

    # Two Flow claims and two Specialists over 8 cores: only the Flows divide them.
    assert admission.job_threads_at_start(store, 8) == 4
