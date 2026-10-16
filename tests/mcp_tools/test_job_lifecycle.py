"""Behavioral Job lifecycle tests without an MCP SDK or transport."""

from __future__ import annotations

# ruff: noqa: F811 — pytest fixture imports intentionally share parameter names.
import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.mcp import endpoint_process, job_results
from booley.mcp.call_context import CallContext
from booley.mcp.job_lifecycle import JobLifecycle
from booley.runtime import job_records, job_slots
from booley.runtime.handles import HandleStore
from booley.runtime.pid import is_pid_alive
from booley.runtime.timefmt import utc_now_rfc3339

SOURCE_ROOT = Path(__file__).parents[2] / "src"
pytestmark = pytest.mark.timeout(120)
JOB_TIMEOUT = 30
WAIT_TIMEOUT = 20


@pytest.fixture
def context(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    overrides = {
        "BOOLEY_RUNTIME_DIR": str(runtime),
        "BOOLEY_LOGS_DIR": str(tmp_path / "logs"),
        "BOOLEY_SLOTS_DIR": str(runtime / "jobs/slots"),
        "PYTHONPATH": str(SOURCE_ROOT),
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(job_results, "project_goal_store", lambda: None)
    monkeypatch.delenv("BOOLEY_MCP_NESTED", raising=False)
    return CallContext(tmp_path, runtime / "jobs", None, tmp_path / "logs", runtime, overrides)


class Activity:
    def __init__(self):
        self.starts = 0
        self.ends = 0

    def mark_mcp_endpoint_start(self):
        self.starts += 1

    def mark_mcp_endpoint_end(self):
        self.ends += 1


def command(script):
    return [sys.executable, "-c", textwrap.dedent(script)]


async def wait_until(predicate):
    deadline = time.monotonic() + WAIT_TIMEOUT
    while not predicate():
        assert time.monotonic() < deadline, "child did not reach the expected state"
        await asyncio.sleep(0.01)


async def stop_jobs(jobs):
    tasks = list(jobs._tasks.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)


def gated_command(context):
    return command(f"""
        from pathlib import Path
        import json, os, time
        ready = Path({str(context.work_dir / "ready")!r})
        release = Path({str(context.work_dir / "release")!r})
        ready.write_text('ready')
        deadline = time.monotonic() + 60
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not release.exists():
            raise RuntimeError('release handshake timed out')
        report = Path(os.environ['BOOLEY_RUNTIME_DIR']) / 'flow-reports/lint/1/report.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        temporary = report.with_suffix('.tmp')
        temporary.write_text(json.dumps({{
            'flow': 'lint', 'run_id': os.environ['BOOLEY_RUN_ID'], 'exit_code': 0, 'passed': True
        }}), encoding='utf-8')
        temporary.replace(report)
        print('completed')
    """)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "exit_code", "status"),
    [
        ("print('completed')", 0, "done"),
        ("import sys; print('failed', file=sys.stderr); sys.exit(1)", 1, "failed"),
    ],
)
async def test_inline_completion_and_nonzero_exit(context, script, exit_code, status):
    activity = Activity()
    jobs = JobLifecycle(activity)
    submission = await jobs.submit_and_wait(
        "lint", command(script), JOB_TIMEOUT, context=context, wait_seconds=WAIT_TIMEOUT
    )
    assert not submission.attached
    assert submission.poll.status == "finished"
    assert submission.poll.result.exit_code == exit_code
    rec = job_records.read_record(submission.poll.run_id, context.jobs_root)
    assert rec.status == status and rec.exit_code == exit_code
    assert activity.starts == activity.ends == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("broken", [False, True])
async def test_job_creation_allocates_handles_and_broken_maps_do_not_gate_execution(
    context, tmp_path, monkeypatch, synchronous, broken
):
    project = tmp_path / "project-data"
    project.mkdir()
    monkeypatch.setattr("booley.runtime.booley_dir.resolve_booley_dir", lambda: project)
    store = HandleStore(project, jobs=True)
    if broken:
        store.root.mkdir(parents=True)
        store.path.write_text("broken")
    jobs = JobLifecycle()
    if synchronous:
        result = await jobs.run_synchronous(
            "lint", command("print('done')"), JOB_TIMEOUT, context=context
        )
        assert result[0] == 0
    else:
        submission = await jobs.submit_and_wait(
            "lint",
            command("print('done')"),
            JOB_TIMEOUT,
            context=context,
            wait_seconds=WAIT_TIMEOUT,
        )
        assert submission.poll.result.exit_code == 0
        assert job_records.read_record(submission.poll.run_id, context.jobs_root) is not None
    if broken:
        assert store.path.read_text() == "broken"
    else:
        assert tuple(store.load().entries.values()) == ("J-LINT1",)


def test_goal_job_handle_uses_bound_project_directory(context, tmp_path, monkeypatch):
    from dataclasses import replace

    project = tmp_path / "bound-project"
    monkeypatch.setattr(
        "booley.runtime.booley_dir.resolve_booley_dir",
        lambda: pytest.fail("Goal handle must use its admitted binding"),
    )
    binding = SimpleNamespace(booley_dir=project, to_json=lambda: {})
    bound = replace(context, binding=binding)
    rec = job_records.JobRecord("goal-run", "sim", utc_now_rfc3339(), JOB_TIMEOUT)
    JobLifecycle._stamp_context(rec, bound)
    assert HandleStore(project, jobs=True).load().entries == {"goal-run": "J-SIM1"}


@pytest.mark.asyncio
async def test_launch_failure_is_terminal(context):
    jobs = JobLifecycle()
    submission = await jobs.submit_and_wait(
        "lint",
        ["__missing_booley_binary__"],
        JOB_TIMEOUT,
        context=context,
        wait_seconds=WAIT_TIMEOUT,
    )
    assert submission.poll.status == "finished"
    assert submission.poll.result.exit_code == 2
    assert "Subprocess launch failed" in submission.poll.result.stderr


@pytest.mark.asyncio
async def test_detached_poll_and_duplicate_attach_across_instances(context):
    jobs = JobLifecycle()
    cmd = gated_command(context)
    try:
        submission = await jobs.submit_and_wait(
            "lint", cmd, JOB_TIMEOUT, context=context, wait_seconds=0
        )
        run_id = submission.poll.run_id
        await wait_until((context.work_dir / "ready").exists)
        assert (await jobs.poll(run_id, 0)).status == "running"
        restarted = JobLifecycle()
        attached = await restarted.submit_and_wait(
            "lint", cmd, JOB_TIMEOUT, context=context, wait_seconds=0
        )
        assert attached.attached and attached.poll.run_id == run_id
        assert not attached.poll.tracked and attached.poll.status == "running"
        asyncio.get_running_loop().call_later(0.03, (context.work_dir / "release").touch)
        adopted = await restarted.poll(run_id, WAIT_TIMEOUT, disk_tick=0.01)
        assert adopted.status == "finished" and adopted.result.exit_code == 0
        assert adopted.result.report_fresh is True
        result = await jobs.poll(run_id, WAIT_TIMEOUT)
        assert result.result.stdout.strip() == "completed"
        assert len(jobs._tasks) == 1 and not restarted._tasks
    finally:
        await stop_jobs(jobs)


@pytest.mark.asyncio
async def test_timeout_persists_failure_and_balances_activity(context):
    activity = Activity()
    jobs = JobLifecycle(activity)
    submission = await jobs.submit_and_wait(
        "lint",
        command("import time; time.sleep(10)"),
        1,
        context=context,
        wait_seconds=WAIT_TIMEOUT,
    )
    result = submission.poll.result
    assert result.exit_code == 2
    assert "timed out after 1s" in result.stderr
    rec = job_records.read_record(submission.poll.run_id, context.jobs_root)
    assert rec.status == "failed" and not is_pid_alive(rec.pid)
    assert activity.starts == activity.ends == 1


@pytest.mark.asyncio
async def test_running_cancel_is_terminal_after_process_stops_and_root_stays_pinned(
    context, monkeypatch
):
    activity = Activity()
    jobs = JobLifecycle(activity)
    run_id = jobs.submit("lint", gated_command(context), JOB_TIMEOUT, context=context)
    try:
        await wait_until((context.work_dir / "ready").exists)
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(context.work_dir / "different-runtime"))
        assert await jobs.cancel(run_id) == "running"
        result = await jobs.poll(run_id)
        rec = job_records.read_record(run_id, context.jobs_root)
        assert result.result.exit_code == 130 and rec.status == "cancelled"
        assert not is_pid_alive(rec.pid)
        assert "CANCELLED" in result.result.stderr
        assert activity.starts == activity.ends == 1
        assert not (context.work_dir / "different-runtime/jobs").exists()
    finally:
        await stop_jobs(jobs)


def write_report(context, run_id, report):
    path = context.runtime_dir / "flow-reports/lint/1/report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, **report}))


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_restart_adopts_only_matching_terminal_report(context, foreign):
    run_id = "orphan"
    rec = job_records.JobRecord(run_id, "lint", utc_now_rfc3339(), 5)
    job_records.write_record(rec, context.jobs_root)
    write_report(context, "different" if foreign else run_id, {"exit_code": 0, "passed": True})
    jobs = JobLifecycle()
    jobs.reconcile_orphaned()
    result = await jobs.poll(run_id)
    assert result.status == "finished"
    assert result.result.exit_code == (2 if foreign else 0)
    assert result.result.report_fresh is not foreign
    assert job_records.read_record(run_id, context.jobs_root).status == (
        "failed" if foreign else "done"
    )


@pytest.mark.asyncio
async def test_adopted_cancel_keeps_completion_that_wins_race(context, monkeypatch):
    run_id = "adopted"
    rec = job_records.JobRecord(run_id, "lint", utc_now_rfc3339(), 5, pid=os.getpid())
    job_records.write_record(rec, context.jobs_root)
    jobs = JobLifecycle()

    async def finish_before_termination(_pid):
        assert job_records.read_record(run_id, context.jobs_root).status == "running"
        write_report(context, run_id, {"exit_code": 0, "passed": True})

    monkeypatch.setattr(jobs, "_cancel_process", finish_before_termination)
    assert await jobs.cancel(run_id) == "finished"
    assert (await jobs.poll(run_id)).result.exit_code == 0
    assert job_records.read_record(run_id, context.jobs_root).status == "done"


@pytest.mark.asyncio
async def test_unknown_job_does_not_borrow_latest_report(context):
    write_report(context, "different", {"exit_code": 0, "passed": True})
    jobs = JobLifecycle()
    assert (await jobs.poll("missing")).status == "unknown"
    assert await jobs.cancel("missing") is None


def test_import_and_execution_work_when_sdk_and_adapter_imports_are_blocked(context):
    script = textwrap.dedent(f"""
        import asyncio, importlib.abc, sys
        from pathlib import Path
        sys.path.insert(0, {str(SOURCE_ROOT)!r})
        class BlockSdk(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'mcp' or fullname.startswith('mcp.') or fullname == 'booley.mcp.server':
                    raise AssertionError('forbidden dependency: ' + fullname)
        sys.meta_path.insert(0, BlockSdk())
        from booley.mcp.call_context import CallContext
        from booley.mcp.job_lifecycle import JobLifecycle
        from booley.mcp.report_encoding import ReportEncoder
        root = Path({str(context.runtime_dir)!r})
        context = CallContext(None, root / 'jobs', None, None, root, {{}})
        async def run():
            jobs = JobLifecycle()
            run_id = jobs.submit('lint', [sys.executable, '-c', 'print("ok")'], {JOB_TIMEOUT}, context=context)
            state = await jobs.poll(run_id, {WAIT_TIMEOUT})
            assert state.result.exit_code == 0
            assert 'ok' in ReportEncoder().text_result(state.result.exit_code, state.result.stdout, '')
            assert ReportEncoder().structured_report({{'passed': True}})['passed'] is True
        asyncio.run(run())
        assert 'mcp' not in sys.modules and 'booley.mcp.server' not in sys.modules
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=context.work_dir,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class FastSupervisor(JobLifecycle):
    @staticmethod
    async def _run_process(cmd, **kwargs):
        return await endpoint_process._run_subprocess(
            cmd, watchdog_tick=0.01, cancel_grace=0.1, **kwargs
        )


def queued_command(context):
    return command(f"""
        from pathlib import Path
        import os, time
        from booley.runtime.job_slots import SlotStore, slots_dir
        store = SlotStore(slots_dir())
        deadline = time.monotonic() + 60
        token = store.acquire('heavy', pid=os.getpid(), timeout_s=30, poll_interval=0.01,
            on_submitted=lambda _: Path({str(context.work_dir / "queued")!r}).touch(),
            should_abort=lambda: time.monotonic() > deadline)
        try:
            Path({str(context.work_dir / "started")!r}).touch()
            print('completed')
        finally:
            store.release(token)
    """)


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw", [False, True])
async def test_real_queued_job_credit_and_withdrawal(context, withdraw):
    store = job_slots.SlotStore(job_slots.slots_dir())
    holder = store.acquire("heavy", pid=os.getpid(), timeout_s=60)
    jobs = FastSupervisor()
    try:
        run_id = jobs.submit("lint", queued_command(context), JOB_TIMEOUT, context=context)
        await wait_until((context.work_dir / "queued").exists)
        # Real-process admission and withdrawal use startup-safe budgets.
        # Exact queue-time credit is exercised with the controlled clock below.
        queued = await jobs.poll(run_id)
        assert queued.status == "running" and queued.phase.startswith("QUEUED")
        assert not (context.work_dir / "started").exists()
        if withdraw:
            assert await jobs.cancel(run_id) == "queued"
            assert (await jobs.poll(run_id)).result.exit_code == 130
            assert not (context.work_dir / "started").exists()
        else:
            store.release(holder)
            result = await jobs.poll(run_id, WAIT_TIMEOUT)
            assert result.result.exit_code == 0
            assert (context.work_dir / "started").exists()
    finally:
        store.release(holder)
        await stop_jobs(jobs)


def descendant_command(context):
    descendant_ready = context.work_dir / "descendant"
    child_script = f"""
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(descendant_ready)!r}).write_text(str(os.getpid()))
time.sleep(10)
"""
    return command(f"""
        import subprocess, sys, time
        from pathlib import Path
        child = subprocess.Popen([sys.executable, '-c', {child_script!r}],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while not Path({str(descendant_ready)!r}).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        ready = Path({str(context.work_dir / "ready")!r})
        pending = ready.with_suffix('.tmp')
        pending.write_text(str(child.pid))
        pending.replace(ready)
        time.sleep(10)
    """)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-tree observation")
def test_event_loop_shutdown_aborts_real_process_group(context, monkeypatch):
    events = []
    monkeypatch.setattr(endpoint_process, "_write_display_event", events.append)
    activity = Activity()
    jobs = FastSupervisor(activity)

    async def start():
        run_id = jobs.submit("lint", descendant_command(context), 10, context=context)
        await wait_until((context.work_dir / "ready").exists)
        return run_id

    run_id = asyncio.run(start())  # Same task teardown used by the stdio entry point.
    rec = job_records.read_record(run_id, context.jobs_root)
    descendant_pid = int((context.work_dir / "ready").read_text())
    asyncio.run(wait_until(lambda: not is_pid_alive(rec.pid) and not is_pid_alive(descendant_pid)))
    assert rec.status == "running"  # Shutdown is recoverable, never user cancellation.
    assert activity.starts == activity.ends == 1
    assert any(event["outcome"] == "aborted" for event in events)
    jobs.reconcile_orphaned()
    assert job_records.read_record(run_id, context.jobs_root).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False])
async def test_stale_report_cannot_vouch_for_terminal_job(context, legacy):
    rec = job_records.JobRecord(
        "current", "lint", utc_now_rfc3339(), 5, status="failed", exit_code=2
    )
    job_records.write_record(rec, context.jobs_root)
    path = context.runtime_dir / "flow-reports/lint.json"
    path.parent.mkdir(parents=True)
    report = {"exit_code": 0, "passed": True, "timestamp": "2000-01-01T00:00:00Z"}
    if not legacy:
        report["run_id"] = "foreign"
    path.write_text(json.dumps(report))
    result = (await JobLifecycle().poll(rec.run_id)).result
    assert result.exit_code == 2 and not result.report_fresh and result.report is None


def test_restart_preserves_existing_submit_deadline_for_unstamped_queued_child(context):
    rec = job_records.JobRecord(
        "queued-legacy", "lint", "2000-01-01T00:00:00Z", 5, pid=os.getpid()
    )
    store = job_slots.SlotStore(job_slots.slots_dir())
    token = store.submit("heavy", pid=os.getpid())  # Unpromoted, live waiter.
    job_records.write_record(rec, context.jobs_root)
    try:
        JobLifecycle().reconcile_orphaned()
        final = job_records.read_record(rec.run_id, context.jobs_root)
        assert final.status == "failed" and final.exit_code == 2
        assert is_pid_alive(rec.pid)
    finally:
        store.release(token)


@pytest.mark.asyncio
async def test_poll_exposes_only_current_nonterminal_checkpoint(context):
    from booley.flows.progress_lifecycle import progress_document, write_progress_json

    rec = job_records.JobRecord("live", "lint", utc_now_rfc3339(), 5, pid=os.getpid())
    job_records.write_record(rec, context.jobs_root)
    checkpoint = progress_document(
        flow="lint",
        run_id=rec.run_id,
        phase="running",
        targets=["a", "b"],
        completed_targets=["a"],
        detail={},
    )
    root = context.runtime_dir / "flow-reports/lint"
    write_progress_json(root / "1/progress.json", checkpoint)
    write_progress_json(root / "2/progress.json", {**checkpoint, "run_id": "foreign"})
    state = await JobLifecycle().poll(rec.run_id)
    assert state.status == "running" and state.result is None
    assert state.progress["run_id"] == rec.run_id
    assert state.progress["completed_targets"] == ["a"]
    assert state.progress["pending_targets"] == ["b"]
    assert state.progress["partial"] is True and state.progress["complete"] is False
    assert "passed" not in state.progress
    assert state.record.run_id == rec.run_id


@pytest.mark.asyncio
async def test_synchronous_execution_uses_same_supervisor_and_forgets_finished_record(context):
    jobs = JobLifecycle()
    result = await jobs.run_synchronous(
        "lint", command("print('inline')"), JOB_TIMEOUT, context=context
    )
    exit_code, stdout, stderr, timed_out = result
    assert exit_code == 0 and stdout.splitlines() == ["inline"]
    assert stderr == "" and timed_out is False
    assert job_records.list_records(context.jobs_root) == []


@pytest.mark.asyncio
async def test_queue_wait_does_not_consume_active_budget(context, monkeypatch):
    clock = SimpleNamespace(elapsed=0.0)
    clock.monotonic = lambda: clock.elapsed
    attempts = []
    active = []

    async def wait():
        if len(attempts) < 5:
            raise TimeoutError

    async def wait_for(awaitable, timeout):
        attempts.append(timeout)
        clock.elapsed += timeout
        return await awaitable

    monkeypatch.setattr(endpoint_process, "time", clock)
    monkeypatch.setattr(endpoint_process, "asyncio", SimpleNamespace(wait_for=wait_for))
    exited = await endpoint_process._wait_with_queue_credit(
        SimpleNamespace(wait=wait),
        2,
        lambda: len(attempts) < 4,
        lambda: active.append(clock.elapsed),
        tick_seconds=5,
    )
    assert exited is True
    assert attempts == [5, 5, 5, 5, 2]
    assert active == [20]
    assert clock.elapsed == 22


# Reuse the real Goal Project fixture across the adapter boundary.
from tests.goals.conftest import layout  # noqa: F401 — shared fixture


@pytest.mark.asyncio
async def test_bound_job_admission_refuses_pruning_or_removed_record(layout, monkeypatch):
    from dataclasses import replace

    from booley.goals.store import GoalStoreError
    from tests.goals.test_record_mutation import bound_context, fence, tree_bytes

    context = bound_context(layout)
    starts = []

    async def executor(*args, **kwargs):
        starts.append(True)
        return 0, "", "", False

    for stage in ("intent", "removed"):
        fence(context, stage)
        for synchronous in (False, True):
            activity = Activity()
            jobs = JobLifecycle(activity)
            monkeypatch.setattr(jobs, "_run_process", executor)
            before = tree_bytes(layout.control / "goals")
            refused = False
            try:
                if synchronous:
                    await jobs.run_synchronous("lint", command("pass"), 30, context=context)
                else:
                    jobs.submit("lint", command("pass"), 30, context=context)
                    await asyncio.sleep(0)
            except GoalStoreError:
                refused = True
            finally:
                await stop_jobs(jobs)
            assert refused and not starts and tree_bytes(layout.control / "goals") == before, (
                "bound Job admission crossed pruning fence"
            )
            assert activity.starts == activity.ends == 0
    # A valid owner whose durable write fails must never reach the executor.
    # Rebuild its existing captured root without changing its immutable identity.
    from booley.goals.paths import record_paths

    paths = record_paths(layout.control, context.binding.record_id)
    paths.root.mkdir()
    paths.lock_file.touch()
    paths.record_file.write_text(
        json.dumps(replace(layout.record, revision=layout.record.revision).to_json())
    )
    monkeypatch.setattr(job_records, "write_record", lambda *a, **kw: False)
    for synchronous in (False, True):
        jobs = JobLifecycle()
        monkeypatch.setattr(jobs, "_run_process", executor)
        with pytest.raises(GoalStoreError, match="persist"):
            if synchronous:
                await jobs.run_synchronous("lint", command("pass"), 30, context=context)
            else:
                jobs.submit("lint", command("pass"), 30, context=context)
        assert not jobs._tasks and not starts


def test_late_bound_job_writers_cannot_resurrect_pruned_record(layout, monkeypatch):
    import shutil
    from functools import partial

    from booley.goals.paths import record_paths
    from booley.mcp.job_results import LocatedJob
    from tests.goals.test_record_mutation import bound_context, fence, tree_bytes

    context = bound_context(layout)
    paths = record_paths(layout.control, context.binding.record_id)
    original = tree_bytes(paths.root)
    owners = ("pid", "started", "terminal", "cancel", "abort", "forget", "restart")
    for stage in ("intent", "quarantine", "removed"):
        for owner in owners:
            if paths.root.exists():
                shutil.rmtree(paths.root)
            quarantine = paths.root.parent / f".pruning-{context.binding.record_id}"
            if quarantine.exists():
                shutil.rmtree(quarantine)
            (paths.root.parent / f".pruning-{context.binding.record_id}.json").unlink(
                missing_ok=True
            )
            for name, data in original.items():
                p = paths.root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            rec = job_records.JobRecord(
                context.binding.invocation_id,
                "lint",
                "2000-01-01T00:00:00Z",
                1,
                binding=context.binding.to_json(),
            )
            job_records.write_record(rec, paths.jobs_dir)
            jobs = JobLifecycle()
            jobs._job_roots[rec.run_id] = paths.jobs_dir
            monkeypatch.setattr(jobs, "_report", lambda _: (None, False))
            fence(context, stage)
            before = tree_bytes(layout.control / "goals")
            actions = {
                "pid": partial(jobs._stamp_pid, rec, os.getpid()),
                "started": partial(jobs._stamp_run_started, rec),
                "terminal": partial(jobs._record_terminal, rec, 0, timed_out=False),
                "cancel": partial(jobs._record_user_cancellation, rec),
                "abort": partial(jobs._abort_synchronous, rec),
                "forget": partial(jobs._forget_synchronous, rec),
                "restart": partial(jobs._reconcile_one, LocatedJob(rec, paths.jobs_dir)),
            }
            actions[owner]()
            assert tree_bytes(layout.control / "goals") == before, (
                "late bound Job writer recreated or mutated pruned record"
            )

    _waiting_late_pid_writer(layout, monkeypatch, paths, original, rec)


def _waiting_late_pid_writer(layout, monkeypatch, paths, original, rec):
    # A real callback already waiting on the original lock must reject its inode
    # once cleanup retires it, even when the external marker is already gone.
    import shutil
    import threading

    from booley.goals import store as store_module
    from tests.goals.test_record_mutation import tree_bytes

    for name, data in original.items():
        p = paths.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    jobs = JobLifecycle()
    jobs._job_roots[rec.run_id] = paths.jobs_dir
    opened, release = threading.Event(), threading.Event()
    original_wait = store_module.wait_for_file_lock

    def wait(handle, **kwargs):
        if threading.current_thread().name == "late-pid-writer":
            opened.set()
            assert release.wait(5)
        return original_wait(handle, **kwargs)

    monkeypatch.setattr(store_module, "wait_for_file_lock", wait)
    thread = threading.Thread(
        target=lambda: jobs._stamp_pid(rec, os.getpid()), name="late-pid-writer"
    )
    thread.start()
    try:
        assert opened.wait(5)
        shutil.rmtree(paths.root)
        before = tree_bytes(layout.control / "goals")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert tree_bytes(layout.control / "goals") == before, (
        "late bound Job writer recreated or mutated pruned record"
    )


@pytest.mark.asyncio
async def test_bound_job_admission_and_prune_are_serialized(layout, monkeypatch):
    from booley.goals.paths import record_paths
    from booley.goals.prune import prune_record
    from booley.goals.store import GoalStore, GoalStoreError
    from tests.goals.conftest import git
    from tests.goals.test_record_mutation import bound_context, fence

    context = bound_context(layout)
    jobs = JobLifecycle()
    # Admission wins first; its fresh PID-less lease is visible to prune.
    started = layout.main / "delayed-child-started"
    run_id = jobs.submit(
        "lint",
        command(f"from pathlib import Path; Path({str(started)!r}).touch()"),
        30,
        context=context,
    )
    fence(context, "terminal")
    git(layout.main, "worktree", "remove", "--force", str(layout.worktree))
    git(layout.main, "branch", "-D", context.binding.goal_branch)
    store = GoalStore(layout.control)
    with pytest.raises(GoalStoreError, match="active record-owned Jobs"):
        prune_record(store, layout.main, context.binding.record_id, confirm=True)
    # Delayed scheduling past grace still cannot spawn after cleanup wins.
    path = record_paths(layout.control, context.binding.record_id).jobs_dir / f"{run_id}.json"
    data = json.loads(path.read_text())
    data["started_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(data))
    prune_record(store, layout.main, context.binding.record_id, confirm=True)
    try:
        await asyncio.sleep(0)
        await asyncio.gather(*jobs._tasks.values(), return_exceptions=True)
        assert not record_paths(layout.control, context.binding.record_id).root.exists()
        assert jobs._results.get(run_id, (None,))[0] != 0
        assert not started.exists()
    finally:
        await stop_jobs(jobs)


@pytest.mark.asyncio
async def test_bound_spawn_failure_reaps_process_before_admission_failure(layout, monkeypatch):
    from booley.goals.store import GoalStoreError
    from tests.goals.test_record_mutation import bound_context

    context = bound_context(layout)
    jobs = JobLifecycle()
    pids = []

    def fail_publication(rec, pid, **kwargs):
        pids.append(pid)
        raise GoalStoreError("cannot persist spawned Goal Job PID")

    monkeypatch.setattr(jobs, "_stamp_pid", fail_publication)
    with pytest.raises(GoalStoreError, match="persist spawned"):
        await jobs.run_synchronous(
            "lint", command("import time; time.sleep(30)"), 30, context=context
        )
    assert pids and not is_pid_alive(pids[0])


@pytest.mark.asyncio
async def test_bound_spawn_cancellation_releases_record_lock_and_stops_child(layout, monkeypatch):
    from booley.goals.store import GoalStore
    from tests.goals.test_record_mutation import bound_context

    context = bound_context(layout)
    jobs = FastSupervisor()
    run_id = jobs.submit("lint", gated_command(context), 30, context=context)
    try:
        await wait_until((context.work_dir / "ready").exists)
        rec = job_records.read_record(run_id, context.jobs_root)
        assert rec.pid and is_pid_alive(rec.pid)
        # The producer's lifetime does not retain a record lock.
        with GoalStore(layout.control, lock_timeout_s=0.1).record_lock(context.binding.record_id):
            pass
        assert await jobs.cancel(run_id) == "running"
        assert not is_pid_alive(rec.pid)
    finally:
        await stop_jobs(jobs)


def test_bound_terminal_bookkeeping_and_pinned_synthetic_end(layout, monkeypatch):
    from tests.goals.test_record_mutation import bound_context, fence

    context = bound_context(layout)
    jobs = JobLifecycle()
    rec = jobs._new_record(context.binding.invocation_id, "lint", command("pass"), 30)
    jobs._stamp_context(rec, context)
    jobs._job_roots[rec.run_id] = context.jobs_root
    jobs._persist(rec, active=True)
    paths = fence(context, "terminal")
    jobs._record_terminal(rec, 0, timed_out=False)
    assert job_records.read_record(rec.run_id, context.jobs_root).status == "done"
    foreign = layout.main / "foreign-runtime"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(foreign))
    jobs._close_display(rec, 0, outcome="aborted")
    assert (paths.runtime_dir / "display.jsonl").exists()
    assert not foreign.exists()


@pytest.mark.asyncio
async def test_bound_cancel_during_creation_reaps_unpublished_child(layout, monkeypatch):
    from tests.goals.test_record_mutation import bound_context

    context = bound_context(layout)
    jobs = FastSupervisor()
    created, release = asyncio.Event(), asyncio.Event()
    original = asyncio.create_subprocess_exec
    pids = []

    async def delayed(*args, **kwargs):
        proc = await original(*args, **kwargs)
        pids.append(proc.pid)
        created.set()
        await asyncio.wait_for(release.wait(), timeout=3)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(
        jobs.run_synchronous("lint", command("import time; time.sleep(30)"), 30, context=context)
    )
    try:
        await asyncio.wait_for(created.wait(), timeout=5)
        task.cancel()
        asyncio.get_running_loop().call_later(0.01, release.set)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert pids and not is_pid_alive(pids[0])
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for pid in pids:
            if is_pid_alive(pid):
                await jobs._cancel_process(pid)


def test_bound_job_temporary_link_cannot_write_outside_record(layout, tmp_path):
    from booley.goals.store import GoalStoreError
    from tests.goals.test_record_mutation import bound_context

    context = bound_context(layout)
    context.jobs_root.mkdir(parents=True)
    outside = tmp_path / "outside-temporary"
    outside.write_bytes(b"preserved")
    temporary = context.jobs_root / f"{context.binding.invocation_id}.json.{os.getpid()}.tmp"
    temporary.symlink_to(outside)
    with pytest.raises(GoalStoreError, match="linked"):
        JobLifecycle().submit("lint", command("pass"), 30, context=context)
    assert outside.read_bytes() == b"preserved"
