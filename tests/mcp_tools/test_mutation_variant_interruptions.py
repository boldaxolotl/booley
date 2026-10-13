"""Abrupt sweep interruption preserves dirty RTL and its Git index."""

from __future__ import annotations

import asyncio
import contextlib
import os
import select
import signal
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from booley.dev_support.mutation_variants import MutationVariantPlan
from booley.mcp import job_results
from booley.mcp.call_context import CallContext
from booley.mcp.job_lifecycle import JobLifecycle
from booley.runtime import job_records
from booley.runtime.pid import is_pid_alive
from booley.specialists.mutation_tester import (
    MutationRunPlan,
    MutationSpec,
    MutationTesterSpecialist,
    MutationTestRun,
)

pytestmark = [
    pytest.mark.timeout(60),
    pytest.mark.skipif(os.name != "posix", reason="requires POSIX FIFO and process groups"),
]
SCOPE = "rtl/dut.sv"
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


@pytest.fixture
def dirty_worktree(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    source = repository / SCOPE
    source.parent.mkdir()
    source.write_bytes(b"assign x = a + b;\n")
    _git(repository, "add", SCOPE)
    _git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "fixture",
    )
    worktree = tmp_path / "worktree"
    _git(repository, "worktree", "add", "-b", "interruption-fixture", str(worktree))
    source = worktree / SCOPE
    source.write_bytes(b"assign x = a + b;\n// staged edit\n")
    _git(worktree, "add", SCOPE)
    source.write_bytes(source.read_bytes() + b"// unstaged edit\r\n")
    return worktree


def _snapshot(worktree: Path) -> tuple[bytes, bytes, bytes]:
    return (
        (worktree / SCOPE).read_bytes(),
        _git(worktree, "show", f":{SCOPE}"),
        _git(worktree, "status", "--porcelain=v1", "--", SCOPE),
    )


def _child_sweep(worktree: str, fifo: str, stage: str) -> None:
    """Run the real sweep, replacing only external compiler/simulator work."""
    root = Path(worktree)
    source = root / SCOPE
    specs = [MutationSpec(1, "operator", SCOPE, 1, "a + b", "a - b")]
    variants = MutationVariantPlan.resolve(specs, root, [SCOPE])
    endpoint = MutationTesterSpecialist()
    endpoint._args = types.SimpleNamespace(work_dir=root, tb_top="tb")
    endpoint.emit_progress = lambda _line: None

    def barrier() -> None:
        # The pipe proves mutation materialization has completed before interruption.
        with Path(fifo).open("wb", buffering=0) as ready:
            ready.write(b"ready")
        if not threading.Event().wait(30):
            raise TimeoutError("parent did not interrupt the ready sweep")

    def elaborate(_target, _work_dir, build_dir, mutation):
        staged = build_dir / "dut.sv"
        staged.write_bytes(source.read_bytes())
        mutation[0].materialize(mutation[1], [staged], build_dir)
        assert b"a - b" in staged.read_bytes()
        if stage == "build":
            barrier()
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def run_suite(*_args, **_kwargs):
        if stage == "test":
            barrier()
        return [MutationTestRun("suite", process=subprocess.CompletedProcess([], 0))]

    endpoint._run_elab = elaborate
    endpoint._run_target_test_suite = run_suite
    plan = MutationRunPlan(
        scope_files=[SCOPE],
        scope_hashes={},
        work_dir=root,
        target="sim",
        tb_top="tb",
        report_dir=None,
        min_detected=1,
        count=1,
        auto_mode=False,
        formula_count=1,
        source_size_budget=None,
    )
    endpoint._run_variant_sweep(plan, specs, variants)


def _command(worktree: Path, fifo: Path, stage: str) -> list[str]:
    script = (
        f"import runpy; module = runpy.run_path({str(Path(__file__).resolve())!r}); "
        f"module['_child_sweep']({str(worktree)!r}, {str(fifo)!r}, {stage!r})"
    )
    return [sys.executable, "-c", script]


@contextlib.contextmanager
def _readiness_pipe(tmp_path: Path):
    fifo = tmp_path / "ready.fifo"
    os.mkfifo(fifo)
    descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        yield fifo, descriptor
    finally:
        os.close(descriptor)


def _await_ready(descriptor: int) -> None:
    assert select.select([descriptor], [], [], 20)[0], "sweep never reached its barrier"
    assert os.read(descriptor, 5) == b"ready"


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PYTHONPATH": str(SOURCE_ROOT),
        "BOOLEY_RUNTIME_DIR": str(tmp_path / "runtime"),
        "BOOLEY_LOGS_DIR": str(tmp_path / "logs"),
        "BOOLEY_SLOTS_DIR": str(tmp_path / "runtime/jobs/slots"),
    }


@pytest.mark.parametrize("stage", ["build", "test"])
def test_sigkill_sweep_preserves_dirty_scope(dirty_worktree, tmp_path, stage):
    before = _snapshot(dirty_worktree)
    assert before[2].startswith(b"MM ")
    with _readiness_pipe(tmp_path) as (fifo, descriptor):
        child = subprocess.Popen(
            _command(dirty_worktree, fifo, stage),
            env={**os.environ, **_environment(tmp_path)},
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _await_ready(descriptor)
            assert _snapshot(dirty_worktree) == before
            child.send_signal(signal.SIGKILL)
            child.communicate(timeout=10)
            assert child.returncode == -signal.SIGKILL
            assert _snapshot(dirty_worktree) == before
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["build", "test"])
async def test_public_job_cancel_preserves_dirty_scope(
    dirty_worktree,
    tmp_path,
    monkeypatch,
    stage,
):
    overrides = _environment(tmp_path)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(job_results, "project_goal_store", lambda: None)
    runtime = tmp_path / "runtime"
    context = CallContext(
        dirty_worktree,
        runtime / "jobs",
        None,
        tmp_path / "logs",
        runtime,
        overrides,
    )
    before = _snapshot(dirty_worktree)
    jobs = JobLifecycle()
    with _readiness_pipe(tmp_path) as (fifo, descriptor):
        run_id = jobs.submit(
            "mutation_tester", _command(dirty_worktree, fifo, stage), 30, context=context
        )
        try:
            await asyncio.to_thread(_await_ready, descriptor)
            assert _snapshot(dirty_worktree) == before
            assert await jobs.cancel(run_id) == "running"
            poll = await jobs.poll(run_id)
            record = job_records.read_record(run_id, context.jobs_root)
            assert poll.status == "finished"
            assert poll.result.exit_code == 130
            assert "CANCELLED" in poll.result.stderr
            assert record.status == job_records.STATUS_CANCELLED
            assert record.exit_code == 130
            assert not is_pid_alive(record.pid)
            assert _snapshot(dirty_worktree) == before
        finally:
            tasks = list(jobs._tasks.values())
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
