"""Owned subprocess-group lifecycle contracts."""

from __future__ import annotations

import signal
import subprocess

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
    process = type("Process", (), {"pid": 999, "returncode": None, "kill": lambda self: None})()
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

    assert calls == [(417, signal.SIGTERM)]
