"""Cancellation must roll back prepared launches before any native spawn."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest
from tests.runtime.scratch_support import scratch_process_ownership_supported

from booley.core import temporary_workspace as tw
from booley.core.models import AgentCallParams
from booley.runtime import _claude_backend as claude
from booley.runtime import _codex_backend as codex

pytestmark = pytest.mark.skipif(
    not scratch_process_ownership_supported(), reason="Linux lease handoffs"
)


async def _provider_attempt(provider, tmp_path):
    from claude_agent_sdk import ClaudeAgentOptions

    from booley.core.workspace_async import async_workspace

    async with async_workspace(prefix="enclosing-specialist-"):
        if provider == "claude":
            async with claude._guarded_sdk_options(ClaudeAgentOptions(), ()):
                raise AssertionError("cancelled preparation reached native spawn")
        else:
            async with codex._async_invocation_home():
                params = AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path)
                await codex._codex_spawn(
                    [sys.executable, "-c", "import time; time.sleep(30)"], params, "api_key"
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_cancelled_preparation_rolls_back_all_pending_roots(provider, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    module, name = (
        (codex, "prepare_launch")
        if provider == "codex"
        else (claude, "_prepare_guarded_sdk_options")
    )
    prepare = getattr(module, name)

    def blocked(*args):
        result = prepare(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(module, name, blocked)
    monkeypatch.setattr(claude, "_native_sdk_cli", lambda _: sys.executable)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    task = asyncio.create_task(_provider_attempt(provider, tmp_path))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not list(tw.registry_root().glob("booley-owned-*"))
    assert tw.active_guard_fds() == ()


def test_claude_shim_publication_failure_rolls_back_both_launches(tmp_path, monkeypatch):
    from claude_agent_sdk import ClaudeAgentOptions

    write = Path.write_text

    def fail_shim(path, *args, **kwargs):
        if path.name == "claude-supervised":
            raise OSError("synthetic shim publication failure")
        return write(path, *args, **kwargs)

    with tw.temporary_workspace() as raw:
        monkeypatch.setattr(Path, "write_text", fail_shim)
        with pytest.raises(OSError, match="shim publication"):
            claude._prepare_guarded_sdk_options(
                ClaudeAgentOptions(), sys.executable, (), Path(raw)
            )
        marker, _ = tw._validated_marker(Path(raw))
        assert marker["launches"] == {}
    assert not Path(raw).exists()


@pytest.mark.asyncio
async def test_cancelled_codex_spawn_joins_and_reaps_obtained_process(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    spawn = asyncio.create_subprocess_exec
    processes = []

    async def delayed_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    task = asyncio.create_task(_provider_attempt("codex", tmp_path))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    assert processes[0].returncode is not None
    assert not list(tw.registry_root().glob("booley-owned-*"))


@pytest.mark.asyncio
async def test_caller_cancellation_survives_a_failing_joined_worker():
    from booley.core.workspace_async import filesystem_call

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def worker():
        entered.set()
        assert release.wait(5)
        finished.set()
        raise OSError("synthetic failed worker")

    task = asyncio.create_task(filesystem_call(worker))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as error:
        await asyncio.wait_for(task, 5)
    assert isinstance(error.value.__cause__, OSError)
    assert finished.is_set()
