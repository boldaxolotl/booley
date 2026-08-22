"""Owned subprocess-group lifecycle contracts."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys

import pytest

from booley.runtime import process_group


def test_group_identity_is_captured_from_spawn_not_rediscovered() -> None:
    process = type("Process", (), {"pid": 417})()

    group = process_group.capture_process_group(process)

    assert group == process_group.ProcessGroup(417)


def test_windows_spawn_uses_new_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)

    assert process_group.new_group_kwargs(is_windows=True) == {"creationflags": 0x200}


def test_force_signal_uses_captured_group_after_direct_child_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = type("Process", (), {"pid": 999, "returncode": 0, "kill": lambda self: None})()
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_group.sys, "platform", "linux")
    monkeypatch.setattr(process_group.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        process_group.os,
        "killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
        raising=False,
    )

    process_group.force_async_process_group_now(process, process_group.ProcessGroup(417))

    assert calls == [(417, 9)]


@pytest.mark.asyncio
async def test_async_graceful_termination_uses_captured_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 999
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_group.sys, "platform", "linux")
    monkeypatch.setattr(
        process_group.os,
        "killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
        raising=False,
    )

    await process_group.terminate_async_process_group(Process(), process_group.ProcessGroup(417))

    assert calls == [
        (417, signal.SIGTERM),
        (417, 0),
        (417, signal.SIGKILL),
    ]


@pytest.mark.asyncio
async def test_windows_async_termination_forces_tree_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = type("Process", (), {"pid": 999, "returncode": 0})()
    calls: list[tuple[int, bool]] = []

    async def taskkill(group: process_group.ProcessGroup, *, force: bool) -> None:
        calls.append((group.id, force))

    monkeypatch.setattr(process_group.sys, "platform", "win32")
    monkeypatch.setattr(process_group, "_async_taskkill", taskkill)

    await process_group.terminate_async_process_group(
        process,
        process_group.ProcessGroup(417),
    )

    assert calls == [(417, False), (417, True)]


@pytest.mark.asyncio
async def test_adopted_group_termination_escalates_by_group_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_group.sys, "platform", "linux")
    monkeypatch.setattr(
        process_group.os,
        "killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
        raising=False,
    )

    await process_group.terminate_adopted_process_group(
        process_group.ProcessGroup(417),
        grace_seconds=0,
    )

    assert calls == [
        (417, signal.SIGTERM),
        (417, 0),
        (417, 0),
        (417, signal.SIGKILL),
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
@pytest.mark.asyncio
async def test_async_termination_kills_descendant_after_leader_exits(tmp_path) -> None:
    ready = tmp_path / "ready"
    survived = tmp_path / "survived"
    grandchild = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(1); "
        f"pathlib.Path({str(survived)!r}).write_text('survived')"
    )
    leader = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        f"import subprocess, time; subprocess.Popen({[sys.executable, '-c', grandchild]!r}); time.sleep(30)",
        **process_group.new_group_kwargs(),
    )
    group = process_group.capture_process_group(leader)
    try:
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(0.02)
        assert ready.exists(), "grandchild never started"

        await process_group.terminate_async_process_group(leader, group, grace_seconds=0.1)
        await asyncio.sleep(1.1)

        assert not survived.exists()
    finally:
        await process_group.force_async_process_group(leader, group)
