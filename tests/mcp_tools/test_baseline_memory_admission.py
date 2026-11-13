# ruff: noqa: F811  (the scene fixtures are imported, then named as test arguments)
"""A Goal baseline Job holds a background Memory Reservation under the switch (#3701).

Lives beside the Booley code it exercises rather than in ``tests/goals``, which the
broad suite does not collect. It reuses that directory's scene and helpers.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from booley.flows.endpoint_admission import AdmissionGate
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.goals.sim_baseline_store import SCHEMA, SIM_BASELINE, SYNTH_BASELINE, write_document
from booley.mcp import job_results
from booley.mcp import sim_baseline_execution as execution
from booley.mcp import sim_baseline_jobs as jobs
from booley.mcp import synth_baseline_execution as synth_execution
from booley.runtime import job_records, job_slots
from booley.runtime.endpoint_execution import OOM_REQUEUE_REASON
from tests.goals.conftest import SMOKE_RUN, layout  # noqa: F401  (fixture, used by scene)
from tests.goals.test_sim_baseline_admission import (  # noqa: F401  (fixtures and helpers)
    _REAL_POPEN,
    _Driver,
    _entries,
    _execute,
    _rewind_job_start,
    _sigterm_restored,
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


def test_baseline_memory_claim_names_its_job_flow_and_target(memory_scene):
    scene = memory_scene
    seen = []
    scene.on_popen = lambda: seen.append(_memory_entries(scene)[0])

    _execute(scene)

    ((holder,),) = seen
    assert holder.job == job_slots.JobLabel(scene.request["job_id"], "sim", "top")


def test_waiting_baseline_narrates_its_memory_wait(tmp_path, capsys):
    store = job_slots.SlotStore(tmp_path / "slots")
    narrate = execution._slot_narrator(store, job_slots.CLASS_MEMORY)

    narrate(0)  # no claim of this process: promoted since the tick, nothing to say
    assert capsys.readouterr().err == ""

    holder = store.submit(
        job_slots.CLASS_MEMORY,
        pid=os.getpid(),
        memory=job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=900.0),
        job=job_slots.JobLabel("run-1", "synth", "a"),
    )
    assert store.refresh(holder).state == job_slots.HOLDING
    # A second claim by the same live process queues: 900 + 400 MiB do not fit 1,000 MiB.
    waiter = store.submit(
        job_slots.CLASS_MEMORY,
        pid=os.getpid(),
        memory=job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=400.0),
    )
    assert store.refresh(waiter).state == job_slots.QUEUED

    narrate(0)

    assert capsys.readouterr().err == (
        "[slot] baseline waiting for memory (position 1): needs 400 MiB; 100 MiB free "
        "of the 1,000 MiB Memory Budget; largest holders: run-1 (synth, a) 900 MiB\n"
    )


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


# --- requeue of an OOM-killed baseline, once, alone (#3764) -----------------------------


def _attempt_result(*rows, oom_killed=False, reason=""):
    """What one child attempt writes: *rows* plus whether its own commands were OOM-killed."""
    return {
        "schema": SCHEMA,
        "rows": list(rows),
        "stamps": [],
        "reason": reason,
        "oom_killed": oom_killed,
    }


def _unavailable_row(reason="baseline Test Run did not complete"):
    return {**SIM_BASELINE.unit_document(SMOKE_RUN), "grade": "unavailable", "reason": reason}


def _script_attempts(scene, *results, seen=None):
    """Make launch N write ``results[N - 1]``; *seen* collects what each launch could observe."""
    output = scene.path.parent

    def on_popen():
        if seen is not None:
            seen.append(
                SimpleNamespace(
                    holders=_memory_entries(scene)[0],
                    child_request=json.loads((output / "simulation-request.json").read_text()),
                )
            )
        write_document(output / "simulation-result.json", results[len(scene.popen_envs) - 1])

    scene.on_popen = on_popen


def _record_of(scene):
    """The baseline Job record as a status reader finds it, owned by this process."""
    record = job_records.read_record(scene.request["job_id"], Path(scene.request["jobs_root"]))
    record.pid = os.getpid()
    return record


def test_oom_killed_baseline_is_requeued_once_to_run_alone(memory_scene):
    scene = memory_scene
    seen = []
    _script_attempts(
        scene, _attempt_result(oom_killed=True), _attempt_result(_unavailable_row()), seen=seen
    )

    _execute(scene)

    first, second = seen
    # Attempt 1 is armed and holds the planned reservation; the killed claim is gone by attempt 2.
    assert first.child_request["armed"] is True
    ((claim,),) = [first.holders]
    assert (claim.memory.reservation_mb, claim.memory.run_alone) == (_RESERVATION_MB, False)
    assert claim.queue_reason is None
    # Attempt 2 runs alone with the whole budget and says why, and is never armed again.
    assert "armed" not in second.child_request
    ((requeued,),) = [second.holders]
    assert requeued.memory == job_slots.MemoryClaim(
        budget_mb=_BUDGET_MB, run_alone=True, wall_s=2.5
    )
    assert requeued.queue_reason == OOM_REQUEUE_REASON
    assert requeued.role == job_slots.ROLE_BACKGROUND
    assert _memory_entries(scene) == ([], [])
    document = json.loads(scene.path.read_text())
    assert document["oom_requeue"] == OOM_REQUEUE_REASON and document["status"] == "pending"


def test_requeued_baseline_shows_the_requeue_reason_while_it_waits(
    memory_scene, foreign_owner, monkeypatch, capsys
):
    scene = memory_scene
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(scene.root))
    other = job_slots.MemoryClaim(budget_mb=_BUDGET_MB, reservation_mb=100.0)
    holder = []
    results = [_attempt_result(oom_killed=True), _attempt_result(_unavailable_row())]

    def on_popen():
        if len(scene.popen_envs) == 1:
            # A small foreign Job fits beside attempt 1, then blocks the run-alone claim.
            holder.append(_foreign_memory_claim(scene, foreign_owner, other))
        write_document(
            scene.path.parent / "simulation-result.json", results[len(scene.popen_envs) - 1]
        )

    scene.on_popen = on_popen
    finished = threading.Event()
    phases = []

    def steps():
        try:
            # The first waiter is attempt 1's own claim before it is promoted; wait for the requeue.
            _wait_until(
                lambda: any(token.queue_reason for token in _memory_entries(scene)[1]),
                finished=finished,
            )
            phases.append(job_results._phase_for_record(_record_of(scene)))
        finally:
            scene.store.release(holder[0])

    driver = _Driver(steps)
    driver.start()
    try:
        _execute(scene)
    finally:
        finished.set()
        driver.finish()
    (phase,) = phases
    assert phase.startswith("QUEUED (position 1): waits to run alone")
    assert phase.endswith(OOM_REQUEUE_REASON)
    assert f"[requeue] baseline {OOM_REQUEUE_REASON}" in capsys.readouterr().err
    assert len(scene.popen_envs) == 2


def test_requeued_baseline_granted_at_once_shows_the_reason_while_running(
    memory_scene, monkeypatch
):
    scene = memory_scene
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(scene.root))
    phases = []
    _script_attempts(scene, _attempt_result(oom_killed=True), _attempt_result())
    write_result = scene.on_popen

    def on_popen():
        phases.append(job_results._phase_for_record(_record_of(scene)))
        write_result()

    scene.on_popen = on_popen

    _execute(scene)

    assert phases == ["RUNNING", f"RUNNING ({OOM_REQUEUE_REASON})"]


def test_held_claim_without_a_requeue_reason_reads_plain_running(tmp_path):
    store = job_slots.SlotStore(tmp_path)
    token = store.submit(
        job_slots.CLASS_MEMORY,
        pid=os.getpid(),
        memory=job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=1.0),
    )
    try:
        assert store.refresh(token).state == job_slots.HOLDING
        with mock.patch.dict(os.environ, {"BOOLEY_SLOTS_DIR": str(tmp_path)}):
            record = SimpleNamespace(pid=os.getpid())
            assert job_results._phase_for_record(record) == "RUNNING"
    finally:
        store.release(token)


def _simulation_request(tmp_path, **extra):
    return {
        "schema": SCHEMA,
        "checkout": str(tmp_path),
        "target": "top",
        "identity": {"runs": [SIM_BASELINE.unit_document(SMOKE_RUN)]},
        "output": str(tmp_path),
    } | extra


def _flow_result(*, oom_killed):
    return SimpleNamespace(outcome=SimpleNamespace(oom_killed=oom_killed, report_text="sim: done"))


def test_simulation_child_reports_an_own_oom_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulateFlow, "execute", lambda *_a, **_k: _flow_result(oom_killed=True))

    killed = execution.simulate(_simulation_request(tmp_path))
    monkeypatch.setattr(SimulateFlow, "execute", lambda *_a, **_k: _flow_result(oom_killed=False))
    clean = execution.simulate(_simulation_request(tmp_path))

    assert (killed["oom_killed"], clean["oom_killed"]) == (True, False)
    assert clean["reason"] == "sim: done"


def test_armed_simulation_child_stops_reading_results_after_an_own_oom_kill(tmp_path, monkeypatch):
    from booley.flows.sim import unpublished_results

    def execute(_flow, _request, *, adapter):
        adapter.campaigns.append(SimpleNamespace(manifest_path=tmp_path, manifest_sha256="x"))
        return _flow_result(oom_killed=True)

    def aborted_campaign(*_args):
        raise ValueError("the killed Campaign has no complete results")

    monkeypatch.setattr(SimulateFlow, "execute", execute)
    monkeypatch.setattr(unpublished_results, "read_unpublished_results", aborted_campaign)

    armed = execution.simulate(_simulation_request(tmp_path, armed=True))

    assert (armed["oom_killed"], armed["rows"], armed["stamps"]) == (True, [], [])
    # Unarmed, the child reads as it always did: the unreadable Campaign raises.
    with pytest.raises(ValueError, match="no complete results"):
        execution.simulate(_simulation_request(tmp_path))


def _flow_report(*, oom_killed: bool):
    """An ``AsicSynthesizeFlow.execute`` fake that publishes a canonical report."""

    def execute(_flow, request, *, adapter):
        if not oom_killed:
            request.report_dir.mkdir(parents=True)
            (request.report_dir / "synth_top.json").write_text(
                json.dumps({"implementation": {"marker": "envelope"}}), encoding="utf-8"
            )
        return _flow_result(oom_killed=oom_killed)

    return execute


def test_synthesis_child_reports_an_own_oom_kill(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    monkeypatch.setattr(AsicSynthesizeFlow, "execute", _flow_report(oom_killed=False))
    assert synth_execution._synthesize(tmp_path, "top", reports, 1000) == (
        {"marker": "envelope"},
        False,
    )
    monkeypatch.setattr(AsicSynthesizeFlow, "execute", _flow_report(oom_killed=True))
    # Armed and killed: the report the kill may have left missing is never read.
    assert synth_execution._synthesize(tmp_path, "top", tmp_path / "b", 1000, armed=True) == (
        None,
        True,
    )
    with pytest.raises(ValueError, match="expected 1, got 0"):
        synth_execution._synthesize(tmp_path, "top", tmp_path / "b", 1000)


def test_synthesis_child_marks_its_result_with_the_kill(tmp_path, monkeypatch):
    unit = {"name": "top", "fingerprint": "f"}
    envelope = {
        "status": {"infra_error": None, "termination": "completed", "passed": True},
        "recipe": {"fingerprint": "f"},
        "metrics": {},
        "provenance": {},
    }
    monkeypatch.setattr(execution, "_remaining_seconds", lambda _output: 1000.0)
    monkeypatch.setattr(execution, "contents_stamp", lambda *_args: "stamp")
    monkeypatch.setattr(
        synth_execution, "DEFAULT_RESOLVERS", SimpleNamespace(fingerprint=lambda *_a, **_k: {})
    )
    request = _simulation_request(tmp_path) | {
        "schema": SYNTH_BASELINE.schema,
        "identity": {"runs": [unit]},
    }

    monkeypatch.setattr(synth_execution, "_synthesize", lambda *_a, armed: (envelope, True))
    unarmed = synth_execution.measure_synth(request)
    monkeypatch.setattr(synth_execution, "_synthesize", lambda *_a, armed: (None, armed))
    armed = synth_execution.measure_synth(request | {"armed": True})

    assert unarmed["oom_killed"] is True and unarmed["rows"][0]["grade"] == "pass"
    assert (armed["oom_killed"], armed["rows"]) == (True, [])


def test_baseline_oom_killed_again_while_alone_ends_with_that_attempt(memory_scene):
    scene = memory_scene
    seen = []
    row = _unavailable_row("baseline Test Run oom")
    _script_attempts(
        scene,
        _attempt_result(oom_killed=True),
        _attempt_result(row, oom_killed=True),
        seen=seen,
    )

    result = _execute(scene)

    # Exactly two launches: the alone attempt is never armed, so it is never requeued again.
    assert len(seen) == len(scene.popen_envs) == 2
    assert "armed" not in seen[1].child_request
    assert result["rows"] == [row] and result["oom_requeue"] == OOM_REQUEUE_REASON


def test_unavailable_row_of_a_requeued_baseline_names_the_requeue(scene):
    run, family = SMOKE_RUN, SIM_BASELINE
    proof = {"contents": "c", "protected_digest": "p"}
    observed = _attempt_result(_unavailable_row("baseline Test Run oom"), oom_killed=True)

    plain = execution._publish_run(scene.control, family, "top", run, proof, None, observed)
    noted = execution._publish_run(
        scene.control,
        family,
        "top",
        run,
        proof,
        None,
        observed | {"oom_requeue": OOM_REQUEUE_REASON},
    )

    assert plain["reason"] == "baseline Test Run oom"
    assert noted["reason"] == f"baseline Test Run oom ({OOM_REQUEUE_REASON})"
    key = family.result_key("top", run, "c", "p")
    assert execution.cached_result(scene.control, key, family) is None


def test_requeued_baseline_returns_only_the_final_attempt_result(memory_scene):
    scene = memory_scene
    first = {**SIM_BASELINE.unit_document(SMOKE_RUN), "grade": "pass"}
    final = {**SIM_BASELINE.unit_document(SMOKE_RUN), "grade": "fail"}
    _script_attempts(scene, _attempt_result(first, oom_killed=True), _attempt_result(final))

    result = _execute(scene)

    assert result["rows"] == [final]


def test_requeued_baseline_never_reads_the_killed_attempt_result(memory_scene):
    scene = memory_scene
    output = scene.path.parent
    stale = _attempt_result(_unavailable_row("from the killed attempt"), oom_killed=True)

    def on_popen():
        if len(scene.popen_envs) == 1:
            (output / "reports").mkdir()
            (output / "reports" / "synth_top.json").write_text("{}", encoding="utf-8")
            write_document(output / "simulation-result.json", stale)
        # Attempt 2 dies before it writes a result.

    scene.on_popen = on_popen

    with pytest.raises(RuntimeError, match="requeued after out-of-memory") as raised:
        _execute(scene)

    assert isinstance(raised.value.__cause__, OSError)
    assert len(scene.popen_envs) == 2
    assert not (output / "reports").exists()
    assert (output / "reports-killed" / "synth_top.json").exists()


def test_requeued_baseline_gets_a_fresh_budget_after_a_long_first_attempt(memory_scene):
    scene = memory_scene
    output = scene.path.parent
    results = [_attempt_result(oom_killed=True), _attempt_result()]

    def popen(_command, *, env, **_kwargs):
        scene.popen_envs.append(env)
        attempt = len(scene.popen_envs)

        def wait(**_kwargs):
            if attempt == 1:  # the first attempt used up its whole budget before it was killed
                _rewind_job_start(scene, remaining_s=-80)
            write_document(output / "simulation-result.json", results[attempt - 1])
            return 0

        return SimpleNamespace(pid=os.getpid(), wait=wait)

    with mock.patch.object(execution.subprocess, "Popen", popen):
        _execute(scene)

    assert len(scene.popen_envs) == 2
    assert execution._remaining_seconds(output) >= 3600


def test_requeued_baseline_wait_to_run_alone_does_not_spend_its_budget(
    memory_scene, foreign_owner
):
    scene = memory_scene
    output = scene.path.parent
    other = job_slots.MemoryClaim(budget_mb=_BUDGET_MB, reservation_mb=100.0)
    holder = []
    results = [_attempt_result(oom_killed=True), _attempt_result()]

    def popen(_command, *, env, **_kwargs):
        scene.popen_envs.append(env)
        attempt = len(scene.popen_envs)

        def wait(**_kwargs):
            if attempt == 1:
                # The first attempt used up its budget before it was killed, and a small
                # foreign Job that fits beside it will block the run-alone claim.
                _rewind_job_start(scene, remaining_s=-80)
                holder.append(_foreign_memory_claim(scene, foreign_owner, other))
            write_document(output / "simulation-result.json", results[attempt - 1])
            return 0

        return SimpleNamespace(pid=os.getpid(), wait=wait)

    finished = threading.Event()

    def budget_left() -> bool:
        try:
            return execution._remaining_seconds(output) > 3000
        except TimeoutError:
            return False

    def steps():
        try:
            _wait_until(lambda: _memory_entries(scene)[1], finished=finished)
            # Waiting restarts the budget rather than spending what the first attempt left.
            _wait_until(budget_left, finished=finished)
        finally:
            scene.store.release(holder[0])

    driver = _Driver(steps)
    driver.start()
    try:
        with mock.patch.object(execution.subprocess, "Popen", popen):
            result = _execute(scene)
    finally:
        finished.set()
        driver.finish()
    assert len(scene.popen_envs) == 2 and result["rows"] == []


def test_requeued_baseline_that_waits_too_long_to_run_alone_names_that_limit(
    memory_scene, foreign_owner, monkeypatch
):
    scene = memory_scene
    other = job_slots.MemoryClaim(budget_mb=_BUDGET_MB, reservation_mb=100.0)
    holder = []
    results = [_attempt_result(oom_killed=True), _attempt_result()]

    def on_popen():
        if len(scene.popen_envs) == 1:
            # A small foreign Job fits beside attempt 1, then blocks the run-alone claim.
            holder.append(_foreign_memory_claim(scene, foreign_owner, other))
        write_document(
            scene.path.parent / "simulation-result.json", results[len(scene.popen_envs) - 1]
        )

    scene.on_popen = on_popen
    monkeypatch.setattr(execution, "QUEUE_WAIT_LIMIT_S", 0.5)
    try:
        with pytest.raises(RuntimeError, match="waited over .* to run alone") as raised:
            _execute(scene)
    finally:
        scene.store.release(holder[0])

    assert "requeued after out-of-memory" in str(raised.value)
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert len(scene.popen_envs) == 1
    assert _memory_entries(scene) == ([], [])


def test_non_oom_baseline_failure_is_not_requeued(memory_scene):
    scene = memory_scene
    row = _unavailable_row("baseline Test Run failed to build")
    seen = []
    _script_attempts(scene, _attempt_result(row), seen=seen)

    result = _execute(scene)

    # The attempt was armed (it held a Memory Reservation), yet nothing asked for a rerun.
    assert seen[0].child_request["armed"] is True
    assert len(scene.popen_envs) == 1
    assert result["rows"] == [row] and "oom_requeue" not in result


def test_baseline_oom_kill_is_not_requeued_with_the_switch_unset(memory_scene, monkeypatch):
    scene = memory_scene
    seen = []
    _script_attempts(
        scene,
        _attempt_result(oom_killed=True),
        _attempt_result(oom_killed=True),
        _attempt_result(),
        seen=seen,
    )

    monkeypatch.delenv("BOOLEY_DEV_MEMORY_ADMISSION")
    result = _execute(scene)

    # The child claims an OOM kill, but without the switch it holds no Memory Reservation.
    assert len(scene.popen_envs) == 1
    assert "armed" not in seen[0].child_request
    assert result["oom_killed"] is True and "oom_requeue" not in result
    assert _entries(scene) == ([], [])
    # The switch alone decides: the same child result is requeued once it is set.
    monkeypatch.setenv("BOOLEY_DEV_MEMORY_ADMISSION", "1")
    assert _execute(scene)["oom_requeue"] == OOM_REQUEUE_REASON
    assert len(scene.popen_envs) == 3


def test_restarting_the_budget_of_a_job_without_a_record_is_refused(scene):
    request = {"jobs_root": scene.request["jobs_root"], "job_id": "no-such-job"}

    with pytest.raises(ValueError, match="no valid watchdog start time"):
        execution._restart_budget(request)


@pytest.mark.skipif(os.name != "posix", reason="the supervisor reads Job identity on POSIX")
def test_supervised_requeued_baseline_keeps_the_requeue_in_its_final_request(
    memory_scene, monkeypatch
):
    scene = memory_scene
    fake_child = execution.subprocess.Popen

    def popen(command, *args, **kwargs):
        # Only the measurement child is faked; the supervisor's Git calls run for real.
        if "booley.mcp.sim_baseline_execution" in command:
            return fake_child(command, **kwargs)
        return _REAL_POPEN(command, *args, **kwargs)

    monkeypatch.setattr(execution.subprocess, "Popen", popen)
    _script_attempts(scene, _attempt_result(oom_killed=True), _attempt_result(oom_killed=True))

    with _sigterm_restored():
        assert jobs.supervise(scene.path) == 2

    document = json.loads(scene.path.read_text())
    assert len(scene.popen_envs) == 2
    assert document["oom_requeue"] == OOM_REQUEUE_REASON
    assert document["status"] == "unavailable"
    assert document["results"][0]["reason"].endswith(f"({OOM_REQUEUE_REASON})")
