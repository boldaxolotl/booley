"""A mutation campaign's builds and simulations hold Memory Reservations (#4115).

The campaign is one memory-admitted Job; each variant build and each simulation of the
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
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.job_estimates import JobRequest
from booley.flows.memory_admission import MemorySizing
from booley.flows.mutation.memory_claims import CampaignClaims
from booley.runtime import job_slots, resource_history
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome
from booley.runtime.job_records import _proc_cmdline
from booley.runtime.oom_priority import COORDINATOR_GAP, tool_adj
from booley.runtime.timefmt import utc_now_rfc3339
from tests.flows.mutation.test_flow import (
    _SCOPE,
    _fake_proc,
    _make_endpoint,
    _patch_fake_simulation,
    _patch_resolve_target,
    _prepare_scope_files,
)
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

    # Private seam: the slot store matches an entry to the live process's argv and
    # `job_records` has no public argv reader (production reads it the same way).
    def context(self, job: job_slots.JobLabel | None = None) -> AdmissionContext:
        pid = os.getpid()
        # The coordinating Job: a memory-class companion claim that reserves nothing.
        coordinator = self.store.acquire(
            job_slots.CLASS_MEMORY,
            pid=pid,
            argv=_proc_cmdline(pid) or [],
            memory=job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=0.0, companion=True),
            job=job,
        )
        self.held.append(coordinator)
        return AdmissionContext(
            mode="managed",
            slot_store=self.store,
            outer_token=coordinator,
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


def _admitted_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context: AdmissionContext | None
) -> EndpointOutcome:
    """Run a whole one-Mutant campaign through the invocation the endpoint prepared and admitted.

    Only the tools' ``subprocess.run`` is replaced (by ``_watch`` beforehand): the baseline
    and the Mutant each build and simulate, so four tools claim memory.
    """
    _prepare_scope_files(tmp_path, [_SCOPE])
    _patch_resolve_target(monkeypatch)
    endpoint = _make_endpoint(
        tmp_path, monkeypatch, target=_TARGET, scope=_SCOPE, count=1, min_detected=0
    )
    return endpoint.prepare_endpoint_invocation().run(context)


def _items(seen: _Observed) -> list[tuple[int, job_slots.MemoryClaim | None]]:
    """The holders but the coordinator's own zero companion claim: the build or simulation."""
    return [(pid, claim) for pid, claim in seen.holders if claim is None or not claim.companion]


def _only_the_coordinator_holds(managed: _Managed) -> bool:
    """No build or simulation claim is left: the coordinator's own claim is the one holder."""
    holders, waiters = managed.store.snapshot(job_slots.CLASS_MEMORY)
    return [token.memory for token in holders] == [managed.held[0].memory] and waiters == []


def _reserved(seen: _Observed) -> list[float | None]:
    return [claim.reservation_mb for _pid, claim in _items(seen) if claim is not None]


def test_builds_and_simulations_hold_a_reservation_sized_from_the_target_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    managed.record_history()
    seen = _watch(monkeypatch, managed, inert_oom_proc)

    _admitted_campaign(tmp_path, monkeypatch, managed.context())

    # The baseline and the Mutant each build, then simulate.
    assert len(seen) == 4
    for observed in seen:
        assert [pid for pid, _claim in _items(observed)] == [os.getpid()]
    # A build reserves its phases' largest estimate; a simulation not the shared build's.
    assert [_reserved(observed) for observed in seen] == [[500.0], [250.0], [500.0], [250.0]]
    # Each claim is released when its tool ends.
    assert _only_the_coordinator_holds(managed)


def test_unsized_build_and_simulation_run_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    seen = _watch(monkeypatch, managed, inert_oom_proc)  # no Resource History

    _admitted_campaign(tmp_path, monkeypatch, managed.context())

    assert len(seen) == 4
    for observed in seen:
        assert [pid for pid, _claim in _items(observed)] == [os.getpid()]
        claim = _items(observed)[0][1]
        assert claim is not None and claim.run_alone and claim.reservation_mb is None
    assert _only_the_coordinator_holds(managed)


def test_a_job_queued_behind_a_build_or_simulation_names_the_mutation_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    managed.record_history()
    said: list[str] = []

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=900.0)
        waiter = managed.store.submit(job_slots.CLASS_MEMORY, pid=os.getpid(), memory=claim)
        try:
            wait = managed.store.memory_wait(os.getpid())
            assert wait is not None
            said.append(wait.describe())
        finally:
            managed.store.release(waiter)
        return _fake_proc(rc=0, stdout="ok")

    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", run)
    job = job_slots.JobLabel("run-7", "mutation", _TARGET)

    _admitted_campaign(tmp_path, monkeypatch, managed.context(job))

    build = (
        "needs 900 MiB; 500 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-7 (mutation, default) 500 MiB"
    )
    simulation = (
        "needs 900 MiB; 750 MiB free of the 1,000 MiB Memory Budget; "
        "largest holders: run-7 (mutation, default, <default>) 250 MiB"
    )
    # The baseline, then the Mutant: each builds, then simulates.
    assert said == [build, simulation, build, simulation]


def test_campaign_processes_rank_as_the_held_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    managed.record_history()
    inert_oom_proc.add(os.getpid(), value=0)
    seen = _watch(monkeypatch, managed, inert_oom_proc)

    _admitted_campaign(tmp_path, monkeypatch, managed.context())

    # The only claim is the newest of none: the first tool level, the coordinator just below.
    assert seen[0].oom_adj == tool_adj(0) - COORDINATOR_GAP
    assert inert_oom_proc.values[os.getpid()] == 0


@pytest.mark.parametrize("withdrawn_by", ["cancellation", "booley_cancel"])
def test_queued_claim_is_withdrawn_when_the_campaign_is_cancelled_or_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
    withdrawn_by: str,
) -> None:
    # The coordinator's claim comes first: a run-alone holder would stop it.
    context = managed.context()
    managed.hold_budget()
    ran = _watch(monkeypatch, managed, inert_oom_proc)

    def cancel() -> bool:
        if not managed.store.snapshot(job_slots.CLASS_MEMORY)[1]:
            return False  # not queued yet
        if withdrawn_by == "cancellation":
            return True
        return not managed.store.cancel_waiter(os.getpid())

    managed.cancellation = cancel

    outcome = _admitted_campaign(tmp_path, monkeypatch, context)

    assert outcome.exit_code == EXIT_ERROR
    assert "RESULT: ERROR (cancelled: " in outcome.report_text
    assert "waited for memory" in outcome.report_text
    assert ran == []  # the baseline build never started
    holders, waiters = managed.store.snapshot(job_slots.CLASS_MEMORY)
    # Only the budget holder and the coordinator's own claim; the queued entry is gone.
    assert len(holders) == 2 and waiters == []


def test_a_run_outside_admission_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: _Managed
) -> None:
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
        with claims.hold(request, build=True, target=_TARGET):
            assert _only_the_coordinator_holds(managed)
    assert asked == []

    # A whole campaign without admission runs and claims nothing.
    fake = _patch_fake_simulation(monkeypatch)
    _prepare_scope_files(tmp_path, [_SCOPE])
    endpoint = _make_endpoint(tmp_path, monkeypatch, scope=_SCOPE, count=1, min_detected=0)
    assert endpoint.prepare_endpoint_invocation().run(None).exit_code == 0
    assert fake.builds == [None, 1]
    assert _only_the_coordinator_holds(managed)


def test_a_target_that_cannot_be_sized_runs_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Private seam: campaign resolution refuses a Target the catalog cannot read before any
    # build, so only a direct call reaches the request builders' `None`.
    flow = _make_endpoint(tmp_path, monkeypatch)

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise OSError("no catalog")

    monkeypatch.setattr("booley.flows.mutation.flow.TargetCatalog.build", refuse)
    unit = SimpleNamespace(test_name=None, selected_tests=())

    assert flow._build_job_request("anything", tmp_path) is None
    assert flow._unit_job_request("anything", tmp_path, tmp_path, unit, True) is None  # type: ignore[arg-type]


def test_an_item_that_ran_alone_over_budget_warns_in_the_campaign_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: _Managed,
    inert_oom_proc: FakeProc,  # noqa: F811  (pytest fixture re-declared as a parameter)
) -> None:
    resource_history.append_records(
        managed.booley_dir,
        [_record("sim_prepare", 100), _record("sim_build", 4000), _record("sim_run", 200)],
    )
    _watch(monkeypatch, managed, inert_oom_proc)
    context = managed.context()
    _prepare_scope_files(tmp_path, [_SCOPE])
    _patch_resolve_target(monkeypatch)
    endpoint = _make_endpoint(
        tmp_path, monkeypatch, target=_TARGET, scope=_SCOPE, count=1, min_detected=0
    )
    invocation = endpoint.prepare_endpoint_invocation()

    # The Flow's public invocation attaches the warning to the campaign result.
    outcome = endpoint.invoke_endpoint(
        PreparedExecution(None, None, False, False, invocation),
        admission=context,
        started=0.0,
    )

    assert "exceeds the Memory Budget of 1000 MiB; this Job ran alone." in outcome.report_text
    assert "memory_admission_warning" in outcome.detail
