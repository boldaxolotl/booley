"""A mutation campaign's builds and simulations hold Memory Reservations (#4115).

The campaign is one light Job; each variant build and each simulation of the
Target's tests claims memory of its own from the Simulation Target's Resource
History, runs alone when it cannot be sized, waits in queue order, and is
withdrawn when the campaign is cancelled.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.job_estimates import JobRequest
from booley.flows.memory_admission import MemorySizing
from booley.flows.mutation.flow import MutationFlow
from booley.flows.prepared_invocation import OrdinaryRunInvocation
from booley.runtime import job_slots, resource_history
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome
from booley.runtime.job_records import _proc_cmdline
from booley.runtime.oom_priority import COORDINATOR_GAP, tool_adj
from booley.runtime.timefmt import utc_now_rfc3339
from tests.flows.mutation.test_flow import _fake_proc, _make_endpoint
from tests.flows.mutation.test_runtime_inputs import _make_project, _Project
from tests.memory_budget_support import pin_budget
from tests.oom_priority_support import FakeProc, inert_oom_proc  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="slot entries are matched to the live process's POSIX argv"
)

_TARGET = "default"


def _record(phase: str, peak_mib: float, test_run: str = "") -> dict[str, Any]:
    """A history record with only the fields the estimator reads."""
    return {
        "key": {"target": _TARGET, "phase": phase, "flags": [], "test_run": test_run},
        "peak_memory_mb": peak_mib,
        "wall_s": 1.0,
        "finished_at": utc_now_rfc3339(),
    }


@dataclass
class _Managed:
    """A managed admission over a real slot store with a 1,000 MiB Memory Budget."""

    store: job_slots.SlotStore
    booley_dir: Path
    cancellation: Callable[[], bool]
    held: list[job_slots.SlotToken] = field(default_factory=list[job_slots.SlotToken])

    def _cancelled(self) -> bool:
        return self.cancellation()  # read late, so a test can set it after the context exists

    def record_history(self) -> None:
        # Estimates are the peak plus a quarter: 100 -> 125, 400 -> 500, 200 -> 250.
        resource_history.append_records(
            self.booley_dir,
            [_record("sim_prepare", 100), _record("sim_build", 400), _record("sim_run", 200)],
        )

    def context(self) -> AdmissionContext:
        pid = os.getpid()
        light = self.store.acquire(job_slots.CLASS_LIGHT, pid=pid, argv=_proc_cmdline(pid) or [])
        self.held.append(light)
        return AdmissionContext(
            mode="managed",
            slot_store=self.store,
            outer_token=light,
            role=job_slots.ROLE_INTERACTIVE,
            execution_id=None,
            timeout_seconds=None,
            cancellation=self._cancelled,
            memory_sizing=MemorySizing(self.booley_dir),
        )

    def hold_budget(self) -> job_slots.SlotToken:
        """Fill the budget, so the next claim queues."""
        claim = job_slots.MemoryClaim(budget_mb=1000.0, run_alone=True)
        token = self.store.submit(job_slots.CLASS_MEMORY, pid=os.getpid(), memory=claim)
        assert self.store.refresh(token).state == job_slots.HOLDING
        self.held.append(token)
        return token


@pytest.fixture
def managed(
    tmp_path: Path,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Managed]:
    booley_dir = tmp_path / "admission"
    booley_dir.mkdir()
    pin_budget(monkeypatch, 1000)
    store = job_slots.SlotStore(tmp_path / "slots")
    admission = _Managed(store, booley_dir, lambda: False)
    yield admission
    for token in admission.held:
        store.release(token)  # stops each lease's renewal thread


@dataclass
class _Observed:
    """What the memory class held while one fake tool process ran."""

    holders: list[tuple[int, job_slots.MemoryClaim | None]]
    waiters: int
    oom_adj: int | None


def _watch(
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    proc: FakeProc,
) -> list[_Observed]:
    """Replace the tools' ``subprocess.run`` with one that records the memory class."""
    seen: list[_Observed] = []

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        holders, waiters = managed.store.snapshot(job_slots.CLASS_MEMORY)
        seen.append(
            _Observed(
                [(holder.pid, holder.memory) for holder in holders],
                len(waiters),
                proc.values.get(os.getpid()),
            )
        )
        return _fake_proc(rc=0, stdout="ok")

    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", run)
    return seen


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Project:
    project = _make_project(tmp_path, monkeypatch)
    project.elab()  # a real (stand-in) build, before the tools are watched
    return project


def _campaign(
    monkeypatch: pytest.MonkeyPatch,
    project: _Project,
    context: AdmissionContext,
    *tools: Callable[[MutationFlow], object],
) -> EndpointOutcome:
    """Run *tools* as the campaign, through the invocation the endpoint prepared and admitted."""
    endpoint = project.endpoint

    def run() -> EndpointOutcome:
        for tool in tools:
            tool(endpoint)
        return EndpointOutcome(exit_code=0)

    monkeypatch.setattr(endpoint, "_run", run)
    return endpoint.prepare_endpoint_invocation().run(context)


def _build(tmp_path: Path) -> Callable[[MutationFlow], object]:
    return lambda endpoint: endpoint._run_elab(_TARGET, tmp_path, tmp_path / "variant")


def _simulate(tmp_path: Path, project: _Project) -> Callable[[MutationFlow], object]:
    return lambda endpoint: endpoint._run_target_test_suite(_TARGET, tmp_path, project.build, "tb")


def _reserved(seen: _Observed) -> list[float | None]:
    return [claim.reservation_mb for _pid, claim in seen.holders if claim is not None]


def test_builds_and_simulations_hold_a_reservation_sized_from_the_target_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    managed.record_history()
    project = _project(tmp_path, monkeypatch)
    seen = _watch(monkeypatch, managed, inert_oom_proc)

    _campaign(
        monkeypatch, project, managed.context(), _build(tmp_path), _simulate(tmp_path, project)
    )

    build, simulation = seen
    # The build reserves its phases' largest estimate; the simulation not the shared build's.
    assert [pid for pid, _claim in build.holders] == [os.getpid()]
    assert _reserved(build) == [500.0]
    assert [pid for pid, _claim in simulation.holders] == [os.getpid()]
    assert _reserved(simulation) == [250.0]
    # Each claim is released when its tool ends.
    assert managed.store.snapshot(job_slots.CLASS_MEMORY) == ([], [])


def test_unsized_build_and_simulation_run_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    project = _project(tmp_path, monkeypatch)  # no Resource History
    seen = _watch(monkeypatch, managed, inert_oom_proc)

    _campaign(
        monkeypatch, project, managed.context(), _build(tmp_path), _simulate(tmp_path, project)
    )

    assert len(seen) == 2
    for observed in seen:
        assert [pid for pid, _claim in observed.holders] == [os.getpid()]
        claim = observed.holders[0][1]
        assert claim is not None and claim.run_alone and claim.reservation_mb is None
    assert managed.store.snapshot(job_slots.CLASS_MEMORY) == ([], [])


def test_campaign_processes_rank_as_the_held_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    managed.record_history()
    project = _project(tmp_path, monkeypatch)
    inert_oom_proc.add(os.getpid(), value=0)
    seen = _watch(monkeypatch, managed, inert_oom_proc)

    _campaign(monkeypatch, project, managed.context(), _build(tmp_path))

    # The only claim is the newest of none: the first tool level, the coordinator just below.
    assert seen[0].oom_adj == tool_adj(0) - COORDINATOR_GAP
    assert inert_oom_proc.values[os.getpid()] == 0


def test_queued_claim_is_withdrawn_when_the_campaign_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    _withdraw_queued_claim(tmp_path, monkeypatch, managed, inert_oom_proc, "cancellation")


def test_queued_claim_is_withdrawn_when_booley_cancel_removes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    _withdraw_queued_claim(tmp_path, monkeypatch, managed, inert_oom_proc, "booley_cancel")


def test_queued_simulation_claim_is_withdrawn_when_the_campaign_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    _withdraw_queued_claim(tmp_path, monkeypatch, managed, inert_oom_proc, "cancellation", True)


def _withdraw_queued_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
    withdrawn_by: str,
    simulation: bool = False,
) -> None:
    project = _project(tmp_path, monkeypatch)
    managed.hold_budget()
    ran = _watch(monkeypatch, managed, inert_oom_proc)

    def cancel() -> bool:
        if not managed.store.snapshot(job_slots.CLASS_MEMORY)[1]:
            return False  # not queued yet
        if withdrawn_by == "cancellation":
            return True
        return not managed.store.cancel_waiter(os.getpid())

    managed.cancellation = cancel

    tool = _simulate(tmp_path, project) if simulation else _build(tmp_path)
    outcome = _campaign(monkeypatch, project, managed.context(), tool)

    assert outcome.exit_code == EXIT_ERROR
    assert "RESULT: ERROR (cancelled: " in outcome.report_text
    assert "waited for memory" in outcome.report_text
    assert ran == []  # the tool never started
    holders, waiters = managed.store.snapshot(job_slots.CLASS_MEMORY)
    assert len(holders) == 1 and waiters == []  # only the budget holder; the entry is gone


def test_a_run_outside_admission_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: _Managed
) -> None:
    from booley.flows.mutation.memory_claims import CampaignClaims

    flow = _make_endpoint(tmp_path, monkeypatch)
    asked: list[str] = []

    def request() -> JobRequest | None:
        asked.append("asked")
        return None

    unmanaged = AdmissionContext(
        mode="unmanaged",
        slot_store=None,
        outer_token=None,
        role=job_slots.ROLE_INTERACTIVE,
        execution_id=None,
        timeout_seconds=None,
        cancellation=lambda: False,
    )
    # No admission, an unmanaged one, and a managed one that lends no sizing size nothing.
    unsized = replace(managed.context(), memory_sizing=None)
    for claims in (CampaignClaims(), CampaignClaims(unmanaged), CampaignClaims(unsized)):
        with claims.hold(request, build=True):
            assert managed.store.snapshot(job_slots.CLASS_MEMORY) == ([], [])
    assert asked == []

    # An admission-less invocation leaves the Flow's claims unmanaged after its run.
    monkeypatch.setattr(flow, "_run", lambda: EndpointOutcome(exit_code=0))
    assert flow.prepare_endpoint_invocation().run(None).exit_code == 0
    with flow._claims.hold(request, build=True):
        pass
    assert asked == []


def test_a_target_that_cannot_be_sized_runs_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _make_endpoint(tmp_path, monkeypatch)

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise OSError("no catalog")

    monkeypatch.setattr("booley.flows.mutation.flow.TargetCatalog.build", refuse)
    unit = SimpleNamespace(test_name=None, selected_tests=())

    assert flow._build_job_request("anything", tmp_path) is None
    assert flow._unit_job_request("anything", tmp_path, tmp_path, unit, True) is None  # type: ignore[arg-type]


def test_the_invocation_reports_the_scope_of_an_ordinary_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _make_endpoint(tmp_path, monkeypatch)
    invocation = flow.prepare_endpoint_invocation()

    assert invocation.borrows_admission
    assert invocation.report_scope(flow) == OrdinaryRunInvocation(flow._run).report_scope(flow)


def test_an_item_that_ran_alone_over_budget_warns_in_the_campaign_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    from booley.flows.endpoint_session import _attach_memory_warning

    resource_history.append_records(
        managed.booley_dir,
        [_record("sim_prepare", 100), _record("sim_build", 4000), _record("sim_run", 200)],
    )
    project = _project(tmp_path, monkeypatch)
    _watch(monkeypatch, managed, inert_oom_proc)
    context = managed.context()

    outcome = _campaign(monkeypatch, project, context, _build(tmp_path))
    _attach_memory_warning(outcome, context)

    assert "exceeds the Memory Budget of 1000 MiB; this Job ran alone." in outcome.report_text
    assert "memory_admission_warning" in outcome.detail
