# ruff: noqa: F811  (the scene fixtures are imported, then named as test arguments)
"""A Goal baseline Job holds a background Memory Reservation under the switch (#3701).

Lives beside the Booley code it exercises rather than in ``tests/goals``, which the
broad suite does not collect. It reuses that directory's scene and helpers.
"""

from __future__ import annotations

import json
import os
import threading
from unittest import mock

import pytest

from booley.flows.endpoint_admission import AdmissionGate
from booley.flows.sim.flow import SimulateFlow
from booley.mcp import sim_baseline_execution as execution
from booley.runtime import job_slots
from tests.goals.conftest import SMOKE_RUN, layout  # noqa: F401  (fixture, used by scene)
from tests.goals.test_sim_baseline_admission import (  # noqa: F401  (fixtures and helpers)
    _Driver,
    _entries,
    _execute,
    _wait_until,
    foreign_owner,
    scene,
)

pytestmark = pytest.mark.timeout(180)

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
        budget_mb=_BUDGET_MB,
        reservation_mb=_RESERVATION_MB,
        wall_s=2.5,  # sim_build + sim_run
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


def test_baseline_child_inherits_the_job_thread_count(memory_scene, foreign_owner, monkeypatch):
    from booley.runtime import cpu_budget

    scene = memory_scene
    monkeypatch.setattr(cpu_budget, "_affinity_cpu_count", lambda: 8)
    monkeypatch.setattr(cpu_budget, "_read_text", lambda _path: None)
    # One other Job holds a (small) Memory Reservation beside the baseline's own.
    other = job_slots.MemoryClaim(budget_mb=_BUDGET_MB, reservation_mb=100.0)
    holder = _foreign_memory_claim(scene, foreign_owner, other)
    assert scene.store.refresh(holder).state == job_slots.HOLDING
    try:
        _execute(scene)
    finally:
        scene.store.release(holder)

    # 8 cores shared by the other Job and the baseline: counted after the baseline's claim.
    assert [env["BOOLEY_INTERNAL_JOB_THREADS"] for env in scene.popen_envs] == ["4"]
