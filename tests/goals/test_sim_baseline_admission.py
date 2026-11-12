"""A SIM baseline Job measures under a background-priority Job Class slot.

The detached supervisor holds the claim while its child simulates, so queued
interactive claims overtake it, a holder is never displaced, and Job status
reports the wait. Foreign claims are owned by a helper process so a stalled
lease can only ever signal it, never the pytest worker.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from booley.flows.endpoint_admission import AdmissionGate
from booley.flows.sim.flow import SimulateFlow
from booley.goals.sim_baseline_store import SCHEMA, write_document
from booley.mcp import sim_baseline_execution as execution
from booley.mcp import sim_baseline_jobs as jobs
from booley.runtime import detached_job, job_records, job_slots
from booley.runtime.job_observation import JobObserver
from booley.runtime.job_observation import _observer as observer_module
from booley.runtime.pid import ProcessObservation, ProcessState
from tests.goals.conftest import SMOKE_RUN, enter_goals
from tests.goals.test_sim_baseline import _schedule

pytestmark = pytest.mark.timeout(180)

#: The scene replaces ``subprocess.Popen`` for the baseline child; helpers need the real one.
_REAL_POPEN = subprocess.Popen

#: Upper bound for every polled condition; reached only when the test is already failing.
WAIT_DEADLINE_S = 30.0


def _job_class() -> str:
    return SimulateFlow()._resolve_job_class()


def _wait_until(condition, *, finished: threading.Event | None = None) -> None:
    """Poll *condition* up to the deadline; fail at once when the baseline already finished."""
    ticker = threading.Event()
    waited = 0.0
    while not condition():
        assert finished is None or not finished.is_set(), "baseline finished without waiting"
        assert waited < WAIT_DEADLINE_S, "condition not reached"
        ticker.wait(0.01)
        waited += 0.01


class _Driver(threading.Thread):
    """Runs *steps* beside the blocked baseline and re-raises its failure on ``finish``."""

    def __init__(self, steps) -> None:
        super().__init__(daemon=True)
        self._steps = steps
        self.failure: BaseException | None = None

    def run(self) -> None:
        try:
            self._steps()
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            self.failure = exc

    def finish(self) -> None:
        self.join(timeout=WAIT_DEADLINE_S)
        assert not self.is_alive(), "driver did not finish"
        if self.failure is not None:
            raise self.failure


@pytest.fixture
def scene(layout, monkeypatch):
    """A scheduled SIM baseline whose child is faked, over the real slot store."""
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    scene = SimpleNamespace(**vars(layout), record=record, launches=[])
    path, request = _schedule(scene)
    write_document(
        path.parent / "simulation-result.json",
        {"schema": SCHEMA, "rows": [], "stamps": [], "reason": ""},
    )
    scene.path, scene.request = path, request
    scene.root = job_slots.slots_dir(layout.control)
    scene.store = job_slots.SlotStore(scene.root)
    scene.popen_envs = []
    scene.on_popen = lambda: None
    scene.popen_returned = threading.Event()

    def popen(_command, *, env, **_kwargs):
        scene.popen_envs.append(env)
        scene.on_popen()
        return SimpleNamespace(pid=os.getpid(), wait=lambda **_kw: 0)

    monkeypatch.setattr(execution.subprocess, "Popen", popen)
    monkeypatch.setattr(execution, "terminate_process_group", lambda *_args: None)
    return scene


@pytest.fixture
def foreign_owner():
    """A live process that owns every foreign claim of a test."""
    process = _REAL_POPEN([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def _foreign_claim(scene, owner, role: str):
    argv = job_slots._proc_cmdline(owner.pid) or []
    return scene.store.submit(_job_class(), pid=owner.pid, argv=argv, role=role)


def _execute(scene):
    return execution.execute_child(scene.worktree, "top", (SMOKE_RUN,), scene.path.parent)


def _entries(scene):
    holders, waiters = scene.store.observe(_job_class())
    return holders, waiters


def test_baseline_measures_under_a_background_slot_of_its_flow_job_class(scene):
    seen = []
    scene.on_popen = lambda: seen.append(_entries(scene))
    _execute(scene)
    ((holders, waiters),) = seen
    assert [(token.role, token.job_class, token.pid) for token in holders] == [
        ("background", _job_class(), os.getpid())
    ]
    assert waiters == []
    assert _entries(scene) == ([], [])


def test_waiting_interactive_claim_is_admitted_before_a_waiting_baseline(scene, foreign_owner):
    holder = _foreign_claim(scene, foreign_owner, job_slots.ROLE_INTERACTIVE)
    assert scene.store.refresh(holder).state == job_slots.HOLDING
    finished = threading.Event()
    admitted = {}

    def steps():
        interactive = None
        try:
            _wait_until(lambda: _entries(scene)[1], finished=finished)
            assert [token.role for token in _entries(scene)[1]] == ["background"]
            interactive = _foreign_claim(scene, foreign_owner, job_slots.ROLE_INTERACTIVE)
            scene.store.release(holder)
            _wait_until(
                lambda: scene.store.refresh(interactive).state == job_slots.HOLDING,
                finished=finished,
            )
            # The baseline is still waiting behind the promoted interactive claim.
            admitted["popen_calls"] = len(scene.popen_envs)
        finally:
            scene.store.release(holder)
            if interactive is not None:
                scene.store.release(interactive)

    driver = _Driver(steps)
    driver.start()
    try:
        _execute(scene)
    finally:
        finished.set()
        driver.finish()
    assert admitted == {"popen_calls": 0}
    assert len(scene.popen_envs) == 1


def test_interactive_claim_never_preempts_a_holding_baseline(scene, foreign_owner):
    seen = {}

    def during_measurement():
        interactive = _foreign_claim(scene, foreign_owner, job_slots.ROLE_INTERACTIVE)
        seen["interactive"] = scene.store.refresh(interactive).state
        seen["holders"] = [token.role for token in _entries(scene)[0]]
        scene.store.release(interactive)

    scene.on_popen = during_measurement
    _execute(scene)
    assert seen == {"interactive": job_slots.QUEUED, "holders": ["background"]}


def test_sim_baseline_child_runs_the_simulation_flow_under_its_parents_claim(scene):
    seen = {}

    def during_measurement():
        # Worst case for the child: it resolves the very store its parent claimed in.
        environment = scene.popen_envs[-1] | {
            "BOOLEY_DIR": str(scene.control),
            "BOOLEY_SLOTS_DIR": str(scene.root),
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            AdmissionGate(SimulateFlow()).enter() as context,
        ):
            seen["mode"] = context.mode
            seen["holders"] = len(_entries(scene)[0])

    scene.on_popen = during_measurement
    _execute(scene)
    assert seen == {"mode": "unmanaged", "holders": 1}


def _rewind_job_start(scene, remaining_s: float) -> None:
    """Make the Job's wall-clock budget (3900 s minus 180 s) leave *remaining_s*."""
    jobs_root = Path(scene.request["jobs_root"])
    record = job_records.read_record(scene.request["job_id"], jobs_root)
    started = datetime.now(UTC) - timedelta(seconds=jobs.BASELINE_TIMEOUT_S - 180 - remaining_s)
    record.started_at = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    job_records.write_record(record, jobs_root)


def test_baseline_supervisor_does_not_inherit_the_launching_executions_identity(monkeypatch):
    monkeypatch.setenv("BOOLEY_RUNTIME_EXECUTION_ID", "launching-session-execution")
    assert "BOOLEY_RUNTIME_EXECUTION_ID" not in detached_job.detached_environment()


def test_baseline_gives_up_its_slot_wait_at_its_budget(scene, foreign_owner):
    holder = _foreign_claim(scene, foreign_owner, job_slots.ROLE_INTERACTIVE)
    assert scene.store.refresh(holder).state == job_slots.HOLDING
    _rewind_job_start(scene, remaining_s=5)
    try:
        with pytest.raises(TimeoutError, match="while waiting for a heavy slot"):
            _execute(scene)
    finally:
        scene.store.release(holder)
    assert scene.popen_envs == []
    assert _entries(scene) == ([], [])


@contextmanager
def _sigterm_restored():
    previous = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.skipif(sys.platform != "linux", reason="Job process identity is captured on Linux")
def test_waiting_baseline_job_reads_as_queued_in_job_status(scene, foreign_owner, monkeypatch):
    monkeypatch.setattr(
        observer_module,
        "observe_process",
        lambda *_args, **_kwargs: ProcessObservation(ProcessState.RUNNING),
    )

    def run_baseline(*_args):
        _execute(scene)  # waits for the slot like a real Job, then measures
        return []

    monkeypatch.setattr(execution, "run_baseline", run_baseline)
    holder = _foreign_claim(scene, foreign_owner, job_slots.ROLE_INTERACTIVE)
    assert scene.store.refresh(holder).state == job_slots.HOLDING
    jobs_root = Path(scene.request["jobs_root"])
    observer = JobObserver(scene.control, slots_root=scene.root)
    finished = threading.Event()
    states = []

    def steps():
        try:
            _wait_until(lambda: _entries(scene)[1], finished=finished)
            states.append(observer.read((jobs_root,)).jobs[0].state)
        finally:
            scene.store.release(holder)

    driver = _Driver(steps)
    driver.start()
    with _sigterm_restored():
        try:
            assert jobs.supervise(scene.path) == 0
        finally:
            finished.set()
            driver.finish()
    assert states == ["queued"]
    record = job_records.read_record(scene.request["job_id"], jobs_root)
    assert record.status == job_records.STATUS_DONE


# --- memory admission behind BOOLEY_DEV_MEMORY_ADMISSION (#3701) ---------------------

#: Every phase of ``top`` peaked at 400 MiB, so its estimate is 500 MiB (x1.25 margin).
_RESERVATION_MB = 500.0
_BUDGET_MB = 1000.0


@pytest.fixture
def memory_scene(scene, monkeypatch):
    """The scene with the switch set, a 1,000 MiB Memory Budget and a history for ``top``."""
    from booley.runtime.resource_history import SCHEMA

    monkeypatch.setenv("BOOLEY_DEV_MEMORY_ADMISSION", "1")
    with (scene.control / "booley.toml").open("a", encoding="utf-8") as config:
        config.write('\n[jobs]\nmemory_budget = "1000m"\n')
    records = [
        {
            "key": {"target": "top", "phase": phase, "flags": [], "test_run": test_run},
            "peak_memory_mb": 400.0,
            "wall_s": 1.0,
        }
        for phase, test_run in (("sim_build", ""), ("sim_run", "smoke"))
    ]
    history = scene.control / "runtime" / "resources" / "history.json"
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps({"$schema": SCHEMA, "records": records}), encoding="utf-8")
    return scene


def _foreign_memory_claim(scene, owner, claim, role=job_slots.ROLE_INTERACTIVE):
    argv = job_slots._proc_cmdline(owner.pid) or []
    return scene.store.submit(
        job_slots.CLASS_MEMORY, pid=owner.pid, argv=argv, role=role, memory=claim
    )


def _memory_entries(scene):
    return scene.store.observe(job_slots.CLASS_MEMORY)


def test_baseline_holds_a_background_memory_reservation_under_the_switch(memory_scene):
    scene = memory_scene
    seen = []
    scene.on_popen = lambda: seen.append((_memory_entries(scene), _entries(scene)))

    _execute(scene)

    (((holders, waiters), heavy),) = seen
    assert [(token.role, token.job_class, token.pid) for token in holders] == [
        ("background", job_slots.CLASS_MEMORY, os.getpid())
    ]
    assert holders[0].memory == job_slots.MemoryClaim(
        budget_mb=_BUDGET_MB, reservation_mb=_RESERVATION_MB
    )
    assert waiters == []
    # It is not a heavy Job Slot.
    assert heavy == ([], [])
    assert _memory_entries(scene) == ([], [])


def test_waiting_interactive_memory_claim_overtakes_a_waiting_baseline(
    memory_scene, foreign_owner
):
    scene = memory_scene
    # 600 + 500 MiB do not fit 1,000 MiB: the baseline waits behind the holder.
    big = job_slots.MemoryClaim(budget_mb=_BUDGET_MB, reservation_mb=600.0)
    holder = _foreign_memory_claim(scene, foreign_owner, big)
    assert scene.store.refresh(holder).state == job_slots.HOLDING
    finished = threading.Event()
    admitted = {}

    def steps():
        interactive = None
        try:
            _wait_until(lambda: _memory_entries(scene)[1], finished=finished)
            assert [token.role for token in _memory_entries(scene)[1]] == ["background"]
            interactive = _foreign_memory_claim(scene, foreign_owner, big)
            scene.store.release(holder)
            _wait_until(
                lambda: scene.store.refresh(interactive).state == job_slots.HOLDING,
                finished=finished,
            )
            # The baseline is still waiting behind the promoted interactive claim.
            admitted["popen_calls"] = len(scene.popen_envs)
        finally:
            scene.store.release(holder)
            if interactive is not None:
                scene.store.release(interactive)

    driver = _Driver(steps)
    driver.start()
    try:
        _execute(scene)
    finally:
        finished.set()
        driver.finish()
    assert admitted == {"popen_calls": 0}
    assert len(scene.popen_envs) == 1


def test_baseline_is_not_admitted_while_a_run_alone_job_holds(memory_scene, foreign_owner):
    scene = memory_scene
    alone = job_slots.MemoryClaim(budget_mb=_BUDGET_MB, run_alone=True)
    holder = _foreign_memory_claim(scene, foreign_owner, alone)
    assert scene.store.refresh(holder).state == job_slots.HOLDING
    finished = threading.Event()
    seen = {}

    def steps():
        try:
            _wait_until(lambda: _memory_entries(scene)[1], finished=finished)
            # Plenty of budget is left, but a run-alone Job blocks every other claim.
            seen["popen_calls_while_held"] = len(scene.popen_envs)
        finally:
            scene.store.release(holder)

    driver = _Driver(steps)
    driver.start()
    try:
        _execute(scene)
    finally:
        finished.set()
        driver.finish()
    assert seen == {"popen_calls_while_held": 0}
    assert len(scene.popen_envs) == 1


def test_baseline_child_reuses_its_memory_admission(memory_scene):
    scene = memory_scene
    seen = {}

    def during_measurement():
        environment = scene.popen_envs[-1] | {
            "BOOLEY_DIR": str(scene.control),
            "BOOLEY_SLOTS_DIR": str(scene.root),
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            AdmissionGate(SimulateFlow()).enter() as context,
        ):
            seen["mode"] = context.mode
            seen["inherited"] = environment[job_slots.INHERITED_SLOT_ENV].split("/")[0]
            seen["holders"] = len(_memory_entries(scene)[0])

    scene.on_popen = during_measurement
    _execute(scene)
    assert seen == {"mode": "unmanaged", "inherited": "memory", "holders": 1}


def test_each_family_says_what_its_baseline_job_runs(scene, monkeypatch):
    from booley.flows import target_test_plan_loading
    from booley.flows.job_estimates import JobRequest

    (request,) = execution.sim_job_requests(scene.worktree, "top", (SMOKE_RUN,))
    assert (request.flow, request.target, request.test_runs) == ("sim", "top", ("smoke",))
    assert execution.sim_job_requests(scene.worktree, "not-a-target", (SMOKE_RUN,)) is None

    def refuse(*_args):
        raise ValueError("tests.toml cannot be applied")

    monkeypatch.setattr(target_test_plan_loading, "load_checkout_target_test_plan", refuse)
    assert execution.sim_job_requests(scene.worktree, "top", ()) is None

    units = ({"name": "synth_after", "fingerprint": "f"},)
    assert execution.EXECUTORS["synth"].job_requests(scene.worktree, "synth_after", units) == (
        JobRequest("synth", "synth_after"),
    )
