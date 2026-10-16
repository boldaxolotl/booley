"""Native receipts survive an uncertain descendant cleanup result."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from booley.core import temporary_workspace as tw
from booley.core.models import AgentCallParams
from booley.runtime import _codex_backend as cb

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux native receipts")


@pytest.mark.asyncio
async def test_native_success_survives_guardian_uncertainty(tmp_path, monkeypatch):
    launcher = tmp_path / "uncertain_guardian.py"
    launcher.write_text(
        "import sys\n"
        "from booley.core import lease_exec\n"
        "wait = lease_exec._wait_tree\n"
        "def uncertain(*args):\n"
        "    assert wait(*args)\n"
        "    return False\n"
        "lease_exec._wait_tree = uncertain\n"
        "raise SystemExit(lease_exec.supervise(sys.argv[1], sys.argv[2:]))\n"
    )
    prepare = cb.prepare_launch

    def uncertain_launcher(*args):
        launch = prepare(*args)
        return replace(launch, command=[sys.executable, str(launcher), *launch.command[2:]])

    monkeypatch.setattr(cb, "prepare_launch", uncertain_launcher)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("PYTHONPATH", str(Path(cb.__file__).resolve().parents[2]))
    stdout, stderr, code = await cb._codex_run_subprocess(
        [sys.executable, "-c", "import sys; sys.stdin.read(); print('native success')"],
        AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path),
        full_prompt="synthetic",
        on_event=None,
        auth_mode="api_key",
    )
    assert code == 0
    assert "native success" in stdout
    assert "descendants remain unproven" in stderr
    assert list(tw.registry_root().glob("booley-owned-*")), (
        "uncertain payload must remain retained"
    )


@pytest.mark.asyncio
async def test_cleanup_deadline_preserves_receipt_without_suppressing_operation_timeout(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    cleanup = AsyncMock()
    monkeypatch.setattr(cb, "_terminate_process", cleanup)

    async def expired(work, *_):
        work.close()
        raise cb._CleanupDeadlineError

    monkeypatch.setattr(cb, "_native_work_deadline", expired)
    await cb._run_codex_work(asyncio.sleep(0), object(), timeout_seconds=1)
    cleanup.assert_awaited_once()

    async def operation_timeout(work, *_):
        work.close()
        raise TimeoutError

    monkeypatch.setattr(cb, "_native_work_deadline", operation_timeout)
    with pytest.raises(cb.AgentTimeoutError):
        await cb._run_codex_work(asyncio.sleep(0), object(), timeout_seconds=1)


@pytest.mark.asyncio
async def test_cancellation_during_cleanup_after_native_completion_propagates(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def expired(work, *_):
        work.close()
        raise cb._CleanupDeadlineError

    async def cleanup(_):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)

    monkeypatch.setattr(cb, "_native_work_deadline", expired)
    monkeypatch.setattr(cb, "_terminate_process", cleanup)
    task = asyncio.create_task(cb._run_codex_work(asyncio.sleep(0), object(), timeout_seconds=1))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
