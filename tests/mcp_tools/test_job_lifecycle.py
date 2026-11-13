"""Behavioral Job lifecycle tests without an MCP SDK or transport."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.progress_lifecycle import progress_document
from booley.mcp import endpoint_process, job_results
from booley.mcp.call_context import CallContext
from booley.mcp.job_lifecycle import JobLifecycle
from booley.runtime import job_records, job_slots
from booley.runtime.handles import HandleStore
from booley.runtime.pid import is_pid_alive
from booley.runtime.timefmt import utc_now_rfc3339
from tests.conftest import symlink_or_skip
from tests.subprocess_budget import run_bounded_child

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
    monkeypatch.setattr("booley.runtime.booley_dir.resolve_common_booley_dir", lambda: project)
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
        "booley.runtime.booley_dir.resolve_common_booley_dir",
        lambda: pytest.fail("Goal handle must use its admitted binding"),
    )
    binding = SimpleNamespace(booley_dir=project, to_json=lambda: {})
    bound = replace(context, binding=binding)
    rec = job_records.JobRecord("goal-run", "sim", utc_now_rfc3339(), JOB_TIMEOUT)
    JobLifecycle._stamp_context(rec, bound)
    assert HandleStore(project, jobs=True).load().entries == {"goal-run": "J-SIM1"}


def test_job_progress_follows_symlinked_booley_dir_entry(context, tmp_path, monkeypatch):
    """Progress behind a Job report is read and repaired through the linked entry."""
    real = tmp_path / "real-booley-dir"
    entry = tmp_path / "project" / ".booley"
    entry.parent.mkdir()
    real.mkdir()  # the pin needs an existing target
    symlink_or_skip(entry, real)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(entry / "runtime"))
    monkeypatch.setattr(
        job_results,
        "project_goal_store",
        lambda: SimpleNamespace(
            booley_dir=entry, list_records=lambda: SimpleNamespace(records=())
        ),
    )
    rec = job_records.JobRecord("run-1", "sim", utc_now_rfc3339(), JOB_TIMEOUT)
    job_records.write_record(rec, root=entry / "runtime" / "jobs")
    checkpoint = real / "runtime" / "flow-reports" / "sim" / "1" / "progress.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            progress_document(
                flow="sim",
                run_id="run-1",
                phase="running",
                targets=("sim_a",),
                completed_targets=(),
                detail={},
            )
        ),
        encoding="utf-8",
    )

    partial = job_results.JobRepository().progress_for_run("sim", "run-1")
    assert partial is not None
    assert partial["partial"] is True

    JobLifecycle()._record_terminal(rec, 2, timed_out=False)
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["phase"] == "aborted"


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


def _finish_record(context, run_id: str) -> None:
    """Write the owner's terminal record, as a baseline Job's supervisor does."""
    done = job_records.read_record(run_id, context.jobs_root)
    assert done is not None
    done.status, done.exit_code, done.ended_at = "done", 0, utc_now_rfc3339()
    job_records.write_record(done, context.jobs_root)


def _assert_owner_outcome_kept(context, run_id: str) -> None:
    final = job_records.read_record(run_id, context.jobs_root)
    assert final is not None
    assert (final.status, final.exit_code) == ("done", 0)


def test_reconcile_keeps_a_terminal_record_written_after_listing(context, monkeypatch):
    run_id = "baseline-like"
    rec = job_records.JobRecord(run_id, "sim_baseline", utc_now_rfc3339(), 60, pid=os.getpid())
    job_records.write_record(rec, context.jobs_root)
    jobs = JobLifecycle()

    def owner_finishes_then_reads_dead(_pid):
        _finish_record(context, run_id)
        return False

    monkeypatch.setattr(jobs, "_is_alive", owner_finishes_then_reads_dead)
    jobs.reconcile_orphaned()
    _assert_owner_outcome_kept(context, run_id)


def test_reconcile_skips_a_record_deleted_after_listing(context, monkeypatch):
    run_id = "vanishing"
    rec = job_records.JobRecord(run_id, "sim_baseline", utc_now_rfc3339(), 60, pid=os.getpid())
    job_records.write_record(rec, context.jobs_root)
    jobs = JobLifecycle()

    def record_removed_then_dead(_pid):
        job_records.delete_record(run_id, root=context.jobs_root)
        return False

    monkeypatch.setattr(jobs, "_is_alive", record_removed_then_dead)
    jobs.reconcile_orphaned()
    assert job_records.read_record(run_id, context.jobs_root) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_state", ["dead", "live"])
async def test_adopted_cancel_keeps_a_terminal_record_written_by_its_owner(
    context, monkeypatch, owner_state
):
    run_id = "baseline-like"
    rec = job_records.JobRecord(run_id, "sim_baseline", utc_now_rfc3339(), 60, pid=os.getpid())
    job_records.write_record(rec, context.jobs_root)
    jobs = JobLifecycle()
    liveness = iter([True])  # ``cancel``'s own status check comes first

    def is_alive(_pid):
        if next(liveness, None) is not None:
            return True
        _finish_record(context, run_id)
        return False

    async def owner_finishes_during_termination(_pid):
        _finish_record(context, run_id)

    if owner_state == "dead":
        monkeypatch.setattr(jobs, "_is_alive", is_alive)
    else:
        monkeypatch.setattr(jobs, "_is_alive", lambda _pid: True)
        monkeypatch.setattr(jobs, "_cancel_process", owner_finishes_during_termination)

    assert await jobs.cancel(run_id) == "finished"
    _assert_owner_outcome_kept(context, run_id)


@pytest.mark.asyncio
async def test_adopted_cancel_still_cancels_an_owner_that_fails_on_sigterm(context, monkeypatch):
    run_id = "baseline-like"
    rec = job_records.JobRecord(run_id, "sim_baseline", utc_now_rfc3339(), 60, pid=os.getpid())
    job_records.write_record(rec, context.jobs_root)
    jobs = JobLifecycle()

    async def owner_records_failure_on_sigterm(_pid):
        failed = job_records.read_record(run_id, context.jobs_root)
        assert failed is not None
        failed.status, failed.exit_code, failed.ended_at = "failed", 2, utc_now_rfc3339()
        job_records.write_record(failed, context.jobs_root)

    monkeypatch.setattr(jobs, "_cancel_process", owner_records_failure_on_sigterm)
    assert await jobs.cancel(run_id) == "running"
    assert job_records.read_record(run_id, context.jobs_root).status == "cancelled"


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
    result = run_bounded_child([sys.executable, "-c", script], cwd=context.work_dir, timeout=40)
    assert result.returncode == 0, result.stderr


#: A Job that runs alone: any other claim of the memory class waits behind it.
_RUN_ALONE = job_slots.MemoryClaim(budget_mb=1024.0, run_alone=True)


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
        from booley.runtime.job_slots import MemoryClaim, SlotStore, slots_dir
        store = SlotStore(slots_dir())
        deadline = time.monotonic() + 60
        token = store.acquire("memory", pid=os.getpid(), timeout_s=30, poll_interval=0.01,
            memory=MemoryClaim(budget_mb=1024.0, run_alone=True),
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
    holder = store.acquire("memory", pid=os.getpid(), timeout_s=60, memory=_RUN_ALONE)
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


def requeued_command(context):
    """A child that claims memory again after an OOM kill: run alone, with the reason."""
    return command(f"""
        from pathlib import Path
        import os, time
        from booley.runtime import job_slots
        store = job_slots.SlotStore(job_slots.slots_dir())
        deadline = time.monotonic() + 60
        claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=None, run_alone=True)
        token = store.acquire(job_slots.CLASS_MEMORY, pid=os.getpid(), timeout_s=30,
            poll_interval=0.01, memory=claim, queue_reason='requeued after out-of-memory',
            on_submitted=lambda _: Path({str(context.work_dir / "queued")!r}).touch(),
            should_abort=lambda: time.monotonic() > deadline)
        store.release(token)
    """)


@pytest.mark.asyncio
async def test_cancelled_requeued_job_names_the_requeue(context):
    store = job_slots.SlotStore(job_slots.slots_dir())
    holder_claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=100.0)
    holder = store.acquire(
        job_slots.CLASS_MEMORY, pid=os.getpid(), timeout_s=60, memory=holder_claim
    )
    jobs = FastSupervisor()
    try:
        run_id = jobs.submit("lint", requeued_command(context), JOB_TIMEOUT, context=context)
        await wait_until((context.work_dir / "queued").exists)
        queued = await jobs.poll(run_id)
        assert queued.phase.startswith("QUEUED (position 1): waits to run alone")
        assert queued.phase.endswith("; requeued after out-of-memory")
        rec = job_records.read_record(run_id, context.jobs_root)
        assert jobs._child_requeued(rec) is True
        assert await jobs.cancel(run_id) == "queued"
        result = (await jobs.poll(run_id)).result
        assert result.exit_code == 130
        assert "while queued again (requeued after out-of-memory)" in result.stderr
    finally:
        store.release(holder)
        await stop_jobs(jobs)


@pytest.mark.asyncio
async def test_unrecorded_cancellation_keeps_no_requeue_reason(context, monkeypatch):
    """A reason saved for a cancellation that never got recorded must not linger."""
    store = job_slots.SlotStore(job_slots.slots_dir())
    holder_claim = job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=100.0)
    holder = store.acquire(
        job_slots.CLASS_MEMORY, pid=os.getpid(), timeout_s=60, memory=holder_claim
    )
    jobs = FastSupervisor()
    try:
        run_id = jobs.submit("lint", requeued_command(context), JOB_TIMEOUT, context=context)
        await wait_until((context.work_dir / "queued").exists)

        async def task_already_finished(*_args):
            return None

        monkeypatch.setattr(jobs, "_cancel_tracked_task", task_already_finished)
        assert jobs._cancel_waiter(job_records.read_record(run_id, context.jobs_root))
        assert jobs._requeue_reasons == {run_id: "requeued after out-of-memory"}

        await jobs.cancel(run_id)

        assert jobs._requeue_reasons == {}
    finally:
        store.release(holder)
        await stop_jobs(jobs)


@pytest.mark.asyncio
async def test_child_requeued_needs_a_claim_with_a_reason(context):
    store = job_slots.SlotStore(job_slots.slots_dir())
    jobs = JobLifecycle()
    no_pid = job_records.JobRecord(
        run_id="lint-1",
        endpoint="lint",
        started_at=utc_now_rfc3339(),
        timeout_s=JOB_TIMEOUT,
        pid=None,
        status=job_records.STATUS_RUNNING,
    )
    assert jobs._child_requeued(no_pid) is False
    mine = job_records.JobRecord(
        run_id="lint-2",
        endpoint="lint",
        started_at=utc_now_rfc3339(),
        timeout_s=JOB_TIMEOUT,
        pid=os.getpid(),
        status=job_records.STATUS_RUNNING,
    )
    assert jobs._child_requeued(mine) is False  # no claim at all
    plain = store.acquire(job_slots.CLASS_LIGHT, pid=os.getpid(), timeout_s=60)
    try:
        assert jobs._child_requeued(mine) is False  # a first claim has no reason
    finally:
        store.release(plain)
    reasoned = store.acquire(
        job_slots.CLASS_LIGHT,
        pid=os.getpid(),
        timeout_s=60,
        queue_reason="requeued after out-of-memory",
    )
    try:
        assert jobs._child_requeued(mine) is True
    finally:
        store.release(reasoned)


def _sharing_violation() -> PermissionError:
    """The error Windows raises when unlinking a file another handle holds open."""
    exc = PermissionError(13, "The process cannot access the file")
    exc.winerror = 32  # type: ignore[attr-defined]  # POSIX lacks the field
    return exc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        pytest.param(_sharing_violation, "queued", id="sharing-once"),
        pytest.param(lambda: FileNotFoundError("promoted"), "running", id="not-found"),
    ],
)
async def test_queued_cancel_withdrawal_unlink_faults(context, monkeypatch, fault, expected):
    """A transient sharing error on the withdrawal unlink is retried (queued, #2089);
    a missing entry means promotion won the race (running)."""
    store = job_slots.SlotStore(job_slots.slots_dir())
    holder = store.acquire("memory", pid=os.getpid(), timeout_s=60, memory=_RUN_ALONE)
    jobs = FastSupervisor()
    real_unlink = Path.unlink
    raised: list[Path] = []

    def faulty_unlink(path, *args, **kwargs):
        is_waiter = path.parent.name == "memory" and path.name.startswith("w-")
        if is_waiter and not raised:
            raised.append(path)
            raise fault()
        return real_unlink(path, *args, **kwargs)

    try:
        run_id = jobs.submit("lint", queued_command(context), JOB_TIMEOUT, context=context)
        await wait_until((context.work_dir / "queued").exists)
        monkeypatch.setattr(Path, "unlink", faulty_unlink)
        outcome = await jobs.cancel(run_id)
        monkeypatch.setattr(Path, "unlink", real_unlink)
        assert raised, "the withdrawal never unlinked a waiter entry"
        assert outcome == expected
        assert (await jobs.poll(run_id)).result.exit_code == 130
        assert not (context.work_dir / "started").exists()
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)
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
    token = store.submit("light", pid=os.getpid())  # Unpromoted, live waiter.
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


def test_poll_view_keeps_running_state_when_progress_is_held():
    from booley.mcp.job_lifecycle import _poll_view

    def held(_run_id):
        raise job_records.JobRecordError("Job progress for run-1 unreadable: held")

    repository = SimpleNamespace(
        record=lambda _id: None, phase=lambda _id: "RUNNING", progress=held
    )
    state = _poll_view("run-1", "running", True, repository)  # type: ignore[arg-type]
    assert state.status == "running" and state.progress is None


def test_finished_poll_names_the_job_when_its_report_is_held(monkeypatch):
    from booley.mcp.job_lifecycle import JobPoll

    def held(_self, _run_id):
        raise job_records.JobRecordError("Job report is unreadable: held")

    monkeypatch.setattr(JobLifecycle, "terminal_result", held)
    with pytest.raises(job_records.JobRecordError, match="Job run-9 finished") as raised:
        JobLifecycle()._complete_poll(JobPoll("run-9", "finished", True))
    assert isinstance(raised.value.__cause__, job_records.JobRecordError)


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

    monkeypatch.setattr(endpoint_process, "asyncio", SimpleNamespace(wait_for=wait_for))
    exited = await endpoint_process._wait_with_queue_credit(
        SimpleNamespace(wait=wait),
        2,
        lambda: len(attempts) < 4,
        lambda: active.append(clock.elapsed),
        tick_seconds=5,
        clock=clock.monotonic,
    )
    assert exited is True
    assert attempts == [5, 5, 5, 5, 2]
    assert active == [20]
    assert clock.elapsed == 22


class _TickedProcess:
    """A child whose ``wait()`` blocks until the watchdog has sampled *exit_after* times."""

    def __init__(self, exit_after: int):
        self.exit_after = exit_after
        self.samples = 0  # one per watchdog loop pass, counted by ``is_queued``

    def is_queued(self) -> bool:
        self.samples += 1
        return False

    async def wait(self) -> int:
        if self.samples >= self.exit_after:
            return 0
        await asyncio.sleep(3600)  # cancelled by the watchdog's per-tick timeout
        return 0


@pytest.mark.asyncio
async def test_requeued_child_gets_its_full_budget_again():
    ticks_before_requeue = 2
    ticks_after_requeue = 2
    # Samples 1..4 are charged ticks; the child exits during the fifth pass.
    exit_after = ticks_before_requeue + ticks_after_requeue + 1
    budget = 2.5  # fits either half of the run, never both

    async def run(*, requeue_signal: bool) -> bool:
        child = _TickedProcess(exit_after)
        clock_value = 0.0

        def clock() -> float:
            # Every sample advances one second, so each timed-out tick charges 1.0.
            nonlocal clock_value
            clock_value += 1.0
            return clock_value

        def is_requeued() -> bool:
            # The claim says "requeued" for the attempt's whole life; the watchdog
            # first sees it once the killed attempt has used up its two ticks, and
            # the new claim is granted at once (is_queued stays False throughout).
            return child.samples >= ticks_before_requeue

        return await endpoint_process._wait_with_queue_credit(
            child,
            budget,
            child.is_queued,
            None,
            tick_seconds=0.001,
            is_requeued=is_requeued if requeue_signal else None,
            clock=clock,
        )

    assert await run(requeue_signal=True) is True
    # Contrast: without the reset the same sequence overruns the budget.
    assert await run(requeue_signal=False) is False


def _orphan_with_report(context):
    """An orphaned RUNNING record whose finished report is on disk."""
    rec = job_records.JobRecord("orphan", "lint", utc_now_rfc3339(), 5)
    job_records.write_record(rec, context.jobs_root)
    write_report(context, rec.run_id, {"exit_code": 0, "passed": True})
    return rec, context.runtime_dir / "flow-reports/lint/1/report.json"


def test_reconcile_adopts_report_after_transient_sharing_error(context, monkeypatch):
    from tests.sharing_error_support import hold_opens, retry_delays_target

    rec, report = _orphan_with_report(context)
    # Hold longer than the report lookup's fallback reads, so only the retry can adopt it.
    attempts = hold_opens(monkeypatch, {report: 3})
    monkeypatch.setattr(retry_delays_target(), (0.0,) * 4)
    JobLifecycle().reconcile_orphaned()
    final = job_records.read_record(rec.run_id, context.jobs_root)
    assert (final.status, final.exit_code) == ("done", 0)
    assert attempts[report] == 4


def test_reconcile_leaves_job_running_on_lasting_report_sharing_error(context, monkeypatch):
    from tests.sharing_error_support import hold_opens

    rec, report = _orphan_with_report(context)
    attempts = hold_opens(monkeypatch, {report: 100})
    JobLifecycle().reconcile_orphaned()
    assert job_records.read_record(rec.run_id, context.jobs_root).status == "running"
    assert attempts[report] > 1


# --- job.log: every supervised MCP Job tees its combined output to disk ---------------


def job_log_files(context):
    """Every ``<jobs root>/<run_id>/job.log`` present now."""
    return sorted(context.jobs_root.glob("*/job.log"))


def log_bytes(context, run_id):
    """Bytes of the Job's ``job.log``, or ``b""`` while it does not exist."""
    path = context.jobs_root / run_id / "job.log"
    return path.read_bytes() if path.is_file() else b""


def log_gate_command(context, first="started", second="finished"):
    """Child that emits *first*, waits for a release file, then emits *second*.

    Lines are written as bytes so a Windows text layer cannot turn ``\\n`` into ``\\r\\n``.
    """
    return command(f"""
        from pathlib import Path
        import sys, time
        release = Path({str(context.work_dir / "release")!r})
        sys.stdout.buffer.write(b'{first}\\n'); sys.stdout.buffer.flush()
        deadline = time.monotonic() + 60
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        sys.stdout.buffer.write(b'{second}\\n'); sys.stdout.buffer.flush()
    """)


@pytest.mark.asyncio
async def test_submitted_job_logs_full_combined_output(context, monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "64")  # tail cap: 4 x 64 = 256 bytes
    script = """
        import sys
        sys.stderr.buffer.write(b'err-marker\\n')
        sys.stderr.buffer.flush()
        for number in range(200):
            sys.stdout.buffer.write(f'out-{number:03d}\\n'.encode())
        sys.stdout.buffer.flush()
    """
    expected_stdout = "".join(f"out-{number:03d}\n" for number in range(200)).encode()
    submission = await JobLifecycle().submit_and_wait(
        "lint", command(script), JOB_TIMEOUT, context=context, wait_seconds=WAIT_TIMEOUT
    )
    log = log_bytes(context, submission.poll.run_id)
    assert log.count(b"err-marker\n") == 1
    assert log.replace(b"err-marker\n", b"") == expected_stdout
    # The agent-facing tail stays bounded: only the end of stdout survives.
    result = submission.poll.result
    assert "out-000" not in result.stdout
    assert result.stdout.endswith("out-199\n")


@pytest.mark.asyncio
async def test_job_log_is_written_while_the_job_runs(context):
    jobs = JobLifecycle()
    run_id = jobs.submit("lint", log_gate_command(context), JOB_TIMEOUT, context=context)
    try:
        await wait_until(lambda: log_bytes(context, run_id) == b"started\n")
    finally:
        (context.work_dir / "release").write_text("go")
    poll = await jobs.poll(run_id, WAIT_TIMEOUT)
    assert poll.status == "finished"
    assert log_bytes(context, run_id) == b"started\nfinished\n"


@pytest.mark.asyncio
async def test_inline_job_logs_while_running_and_forgets_log_with_record(context):
    jobs = JobLifecycle()
    run = asyncio.create_task(
        jobs.run_synchronous("lint", log_gate_command(context), JOB_TIMEOUT, context=context)
    )
    try:
        await wait_until(
            lambda: [path.read_bytes() for path in job_log_files(context)] == [b"started\n"]
        )
        run_directory = job_log_files(context)[0].parent
    finally:
        (context.work_dir / "release").write_text("go")
    exit_code, stdout, _stderr, _timed_out = await asyncio.wait_for(run, WAIT_TIMEOUT)
    assert exit_code == 0 and stdout == "started\nfinished\n"
    assert job_records.list_records(context.jobs_root) == []
    assert not run_directory.exists()


@pytest.mark.asyncio
async def test_unwritable_job_log_does_not_fail_the_job(context):
    jobs = JobLifecycle()
    jobs._next_run_id = lambda endpoint: "lint-blocked"
    # A regular file where the run directory belongs: creating it fails on every OS.
    context.jobs_root.mkdir(parents=True, exist_ok=True)
    (context.jobs_root / "lint-blocked").write_text("not a directory")
    submission = await jobs.submit_and_wait(
        "lint",
        command("print('completed')"),
        JOB_TIMEOUT,
        context=context,
        wait_seconds=WAIT_TIMEOUT,
    )
    result = submission.poll.result
    assert result.exit_code == 0 and result.stdout.strip() == "completed"
    assert submission.poll.record.status == "done"
    assert result.stderr.count("job.log unavailable") == 1


class _FailingSecondWrite:
    """Raw log file whose second write fails the way a full disk does."""

    def __init__(self, raw):
        self._raw = raw
        self._writes = 0

    def write(self, data):
        self._writes += 1
        if self._writes == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return self._raw.write(data)

    def flush(self):
        self._raw.flush()

    def close(self):
        self._raw.close()


@pytest.mark.asyncio
async def test_job_log_write_failure_mid_run_keeps_job_and_reports_once(context, monkeypatch):
    real_open = Path.open

    def open_job_log(path, *args, **kwargs):
        opened = real_open(path, *args, **kwargs)
        # Only the supervisor's write handle fails; readers of job.log are untouched.
        writing = path.name == "job.log" and args[:1] == ("wb",)
        return _FailingSecondWrite(opened) if writing else opened

    monkeypatch.setattr(Path, "open", open_job_log)
    jobs = JobLifecycle()
    run_id = jobs.submit(
        "lint", log_gate_command(context, "first", "second"), JOB_TIMEOUT, context=context
    )
    try:
        # Release only once "first" is logged, so the two lines are separate chunks.
        await wait_until(lambda: log_bytes(context, run_id) == b"first\n")
    finally:
        (context.work_dir / "release").write_text("go")
    result = (await jobs.poll(run_id, WAIT_TIMEOUT)).result
    assert result.exit_code == 0 and result.stdout == "first\nsecond\n"
    assert result.stderr.count("job.log unavailable") == 1
    assert log_bytes(context, run_id) == b"first\n"
