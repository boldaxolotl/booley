"""Owned subprocess-group lifecycle contracts."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time

import pytest
from tests.time_budget import hang_guard_s, scaled
from tests.windows_job_fake import JOB, FakeLeader, WindowsJobFake

from booley.runtime import process_group


def _install_posix_process_apis(monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """Install POSIX-only seams even when this test suite runs on Windows."""
    sigterm = 15
    sigkill = 9
    monkeypatch.setattr(process_group.signal, "SIGTERM", sigterm, raising=False)
    monkeypatch.setattr(process_group.signal, "SIGKILL", sigkill, raising=False)
    return sigterm, sigkill


def test_group_identity_is_captured_from_spawn_not_rediscovered() -> None:
    process = type("Process", (), {"pid": 417})()

    group = process_group.capture_process_group(process)

    assert group == process_group.ProcessGroup(417)


def test_windows_spawn_uses_new_process_group() -> None:
    assert process_group.new_group_kwargs(is_windows=True) == {"creationflags": 0x200}
    # An owned child also starts suspended (0x4), until it is in its job.
    assert process_group.owned_group_kwargs(is_windows=True) == {"creationflags": 0x204}


@pytest.mark.parametrize("job", ["none", "create", "assign"])
@pytest.mark.parametrize("taskkill_returncode", [0, 1])
def test_windows_termination_targets_tree_after_leader_exit_or_taskkill_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    taskkill_returncode: int,
    job: str,
) -> None:
    """Without a job, whether none was asked for or none could be had, the tree is walked."""
    caplog.set_level(logging.DEBUG, logger=process_group.__name__)
    fake = WindowsJobFake.install(monkeypatch, fail=job, taskkill_returncode=taskkill_returncode)
    process = FakeLeader(417, returncode=0)
    group = process_group.ProcessGroup(417)
    if job != "none":
        group = process_group.own_process_group(process)

    process_group.terminate_process_group(process, group, is_windows=True)

    assert fake.taskkills == [
        (["taskkill", "/T", "/PID", "417"], 10),
        (["taskkill", "/F", "/T", "/PID", "417"], 10),
    ]
    if job != "none":
        assert fake.resumed == [417]
        assert f"could not {job} " in caplog.text


@pytest.mark.parametrize(
    ("options", "requested", "terminated"),
    [
        pytest.param({}, [], False, id="members-exited"),
        pytest.param({"members": [501, 502]}, ["501", "502"], True, id="members-running"),
        pytest.param(
            {"members": [501, 502], "outsiders": [502]}, ["501"], True, id="outsider-listed"
        ),
        pytest.param(
            {"members": [501], "on_request": lambda fake: fake.members.clear()},
            ["501"],
            False,
            id="members-exit-on-request",
        ),
        pytest.param({"members": [501], "fail": "truncate"}, ["501"], True, id="list-truncated"),
        pytest.param({"members": [501], "fail": "query"}, [], True, id="job-unreadable"),
        pytest.param({"members": [501], "fail": "terminate"}, ["501"], True, id="kill-refused"),
    ],
)
def test_windows_job_stops_its_members_and_reports_an_emptied_tree(
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object],
    requested: list[str],
    terminated: bool,
) -> None:
    fake = WindowsJobFake.install(monkeypatch, **options)
    process = FakeLeader(501, returncode=0)
    group = process_group.own_process_group(process)
    assert fake.events[:2] == ["assign", "resume"], "the child ran before it was in the job"
    assert process_group.capture_process_group(process) is group

    process_group.terminate_process_group(process, group, grace_seconds=0.1, is_windows=True)

    arguments = [part for pid in requested for part in ("/PID", pid)]
    assert [command for command, _ in fake.taskkills] == (
        [["taskkill", *arguments]] if requested else []
    )
    assert ("terminate" in fake.events) is terminated
    process_group.release_process_group(process)
    assert fake.events[-1] == f"close {JOB}"
    assert process_group.capture_process_group(process) == process_group.ProcessGroup(501)


def test_windows_job_released_during_a_stop_closes_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeLeader(501, returncode=0)
    fake = WindowsJobFake.install(
        monkeypatch,
        members=[501],
        on_request=lambda _: process_group.release_process_group(process),
    )
    group = process_group.own_process_group(process)

    process_group.terminate_process_group(process, group, grace_seconds=0, is_windows=True)

    assert fake.events.index("terminate") < fake.events.index(f"close {JOB}")
    closed = list(fake.events)
    # A late stop on the closed job touches no handle and sends nothing more.
    process_group.terminate_process_group(process, group, grace_seconds=0, is_windows=True)
    process_group.force_group_termination(process, group, is_windows=True)
    assert fake.events == closed
    assert len(fake.taskkills) == 1


@pytest.mark.parametrize("fail", ["open", "resume"])
def test_a_windows_child_that_cannot_be_resumed_is_killed(
    monkeypatch: pytest.MonkeyPatch, fail: str, tmp_path
) -> None:
    fake = WindowsJobFake.install(monkeypatch, fail=fail)
    process = FakeLeader(501)
    with (tmp_path / "out").open("wb") as pipe:
        process.stdout = pipe

        with pytest.raises(OSError, match=f"could not {fail} process 501"):
            process_group.own_process_group(process)

        assert process.killed and pipe.closed
    assert (f"close {JOB}" in fake.events) is (fail == "resume"), "the unused job leaked"


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


def test_uncertain_posix_group_liveness_is_treated_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sigterm, sigkill = _install_posix_process_apis(monkeypatch)
    process = type("Process", (), {"poll": lambda self: 0})()
    calls: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))
        if sig == 0:
            raise PermissionError

    monkeypatch.setattr(process_group.os, "killpg", killpg, raising=False)

    process_group.terminate_process_group(
        process,
        process_group.ProcessGroup(417),
        is_windows=False,
    )

    assert calls == [
        (417, sigterm),
        (417, 0),
        (417, sigkill),
    ]


def test_sync_timeout_forces_group_and_reaps_direct_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sigterm, sigkill = _install_posix_process_apis(monkeypatch)

    class Process:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise subprocess.TimeoutExpired("leader", timeout)
            return 0

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_group.os,
        "killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
        raising=False,
    )
    process = Process()

    process_group.terminate_process_group(
        process,
        process_group.ProcessGroup(417),
        grace_seconds=0.25,
        is_windows=False,
    )

    assert process.waits == [0.25, 5]
    assert calls == [
        (417, sigterm),
        (417, 0),
        (417, sigkill),
    ]


@pytest.mark.asyncio
async def test_async_graceful_termination_uses_captured_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sigterm, sigkill = _install_posix_process_apis(monkeypatch)

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
        (417, sigterm),
        (417, 0),
        (417, sigkill),
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
        f"time.sleep({scaled(1)}); "
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
        deadline = time.monotonic() + hang_guard_s()
        while not ready.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert ready.exists(), "grandchild never started"

        await process_group.terminate_async_process_group(leader, group, grace_seconds=scaled(0.1))
        await asyncio.sleep(scaled(1.1))

        assert not survived.exists()
    finally:
        await process_group.force_async_process_group(leader, group)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
@pytest.mark.asyncio
async def test_adopted_group_termination_kills_descendant_after_leader_exit(tmp_path) -> None:
    ready = tmp_path / "adopted-ready"
    survived = tmp_path / "adopted-survived"
    grandchild = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        f"time.sleep({scaled(1)}); "
        f"pathlib.Path({str(survived)!r}).write_text('survived')"
    )
    leader = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        f"import subprocess; subprocess.Popen({[sys.executable, '-c', grandchild]!r})",
        **process_group.new_group_kwargs(),
    )
    group = process_group.capture_process_group(leader)
    await leader.wait()
    try:
        deadline = time.monotonic() + hang_guard_s()
        while not ready.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert ready.exists(), "adopted grandchild never started"

        await process_group.terminate_adopted_process_group(group, grace_seconds=scaled(0.1))
        await asyncio.sleep(scaled(1.1))

        assert not survived.exists()
    finally:
        await process_group.terminate_adopted_process_group(group, grace_seconds=0)


@pytest.mark.asyncio
async def test_adopted_group_termination_escalates_by_group_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sigterm, sigkill = _install_posix_process_apis(monkeypatch)
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
        (417, sigterm),
        (417, 0),
        (417, 0),
        (417, sigkill),
    ]


@pytest.mark.asyncio
async def test_windows_taskkill_failure_still_escalates_to_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    class TaskkillProcess:
        async def wait(self) -> int:
            return 255

    async def create_taskkill(*command, **_kwargs):
        commands.append(command)
        return TaskkillProcess()

    leader = type("Process", (), {"returncode": 0})()
    monkeypatch.setattr(process_group.sys, "platform", "win32")
    monkeypatch.setattr(process_group.asyncio, "create_subprocess_exec", create_taskkill)

    await process_group.terminate_async_process_group(
        leader,
        process_group.ProcessGroup(417),
    )

    assert commands == [
        ("taskkill", "/T", "/PID", "417"),
        ("taskkill", "/F", "/T", "/PID", "417"),
    ]


@pytest.mark.asyncio
async def test_windows_hung_taskkill_is_stopped_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TaskkillProcess:
        def __init__(self) -> None:
            self.killed = False
            self.waits = 0

        async def wait(self) -> int:
            self.waits += 1
            return 0

        def kill(self) -> None:
            self.killed = True

    killers: list[TaskkillProcess] = []

    async def create_taskkill(*_args, **_kwargs):
        killer = TaskkillProcess()
        killers.append(killer)
        return killer

    async def bounded_wait(awaitable, *, timeout):
        if timeout == 10:
            awaitable.close()
            raise TimeoutError
        return await awaitable

    leader = type("Process", (), {"pid": 999, "returncode": 0})()
    monkeypatch.setattr(process_group.sys, "platform", "win32")
    monkeypatch.setattr(process_group.asyncio, "create_subprocess_exec", create_taskkill)
    monkeypatch.setattr(process_group.asyncio, "wait_for", bounded_wait)

    await process_group.terminate_async_process_group(
        leader,
        process_group.ProcessGroup(417),
    )

    assert len(killers) == 2
    assert all(killer.killed for killer in killers)
    assert all(killer.waits == 1 for killer in killers)
