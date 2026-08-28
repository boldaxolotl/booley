"""Boundary tests for conservative process-liveness checks."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

import pytest

from booley.runtime import pid as runtime_pid


def _write_proc_entry(
    root: Path, pid: int, *, state: str, start_ticks: int, namespace: str
) -> None:
    proc = root / str(pid)
    (proc / "ns").mkdir(parents=True, exist_ok=True)
    fields_4_through_21 = ["1", *(["0"] * 17)]
    stat = f"{pid} (python worker) {state} {' '.join(fields_4_through_21)} {start_ticks}\n"
    (proc / "stat").write_text(stat, encoding="utf-8")
    (proc / "ns" / "pid").unlink(missing_ok=True)
    (proc / "ns" / "pid").symlink_to(namespace)


class _FakeCall:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


class _FakeKernel32:
    def __init__(self, *, handle: int, exit_code: int = 259, exit_query_ok: bool = True):
        self.OpenProcess = _FakeCall(lambda *_args: handle)

        def get_exit_code(_handle, output) -> bool:
            output._obj.value = exit_code
            return exit_query_ok

        self.GetExitCodeProcess = _FakeCall(get_exit_code)
        self.CloseHandle = _FakeCall(lambda *_args: True)


@pytest.mark.parametrize(("exit_code", "expected"), [(259, True), (0, False)])
def test_windows_live_and_recently_exited_processes(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, expected: bool
) -> None:
    kernel32 = _FakeKernel32(handle=12, exit_code=exit_code)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)

    assert runtime_pid._windows_pid_alive(1234) is expected


def test_windows_access_denied_is_conservatively_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = _FakeKernel32(handle=0)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)

    assert runtime_pid._windows_pid_alive(1234) is True


def test_posix_permission_denied_is_conservatively_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(runtime_pid.sys, "platform", "linux")
    monkeypatch.setattr(runtime_pid.os, "kill", deny, raising=False)

    assert runtime_pid.is_pid_alive(1234) is True


def test_posix_zombie_is_not_alive() -> None:
    if sys.platform != "linux":
        pytest.skip("Linux /proc process states are required")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="utf-8")
            if stat.rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("child did not become a zombie")

        assert runtime_pid.is_pid_alive(child.pid) is False
    finally:
        child.wait(timeout=5)


def test_observation_distinguishes_zombie_and_reused_pid(tmp_path: Path) -> None:
    _write_proc_entry(tmp_path, 123, state="S", start_ticks=100, namespace="pid:[10]")
    identity = runtime_pid.capture_process_identity(123, proc_root=tmp_path)
    assert identity == runtime_pid.ProcessIdentity(
        pid=123,
        pid_namespace="pid:[10]",
        start_ticks=100,
    )
    assert runtime_pid.observe_process(identity, proc_root=tmp_path).state is runtime_pid.RUNNING

    (tmp_path / "123" / "stat").unlink()
    _write_proc_entry(tmp_path, 123, state="Z", start_ticks=100, namespace="pid:[10]")
    assert runtime_pid.observe_process(identity, proc_root=tmp_path).state is runtime_pid.ZOMBIE

    (tmp_path / "123" / "stat").unlink()
    (tmp_path / "123" / "ns" / "pid").unlink()
    _write_proc_entry(tmp_path, 123, state="S", start_ticks=101, namespace="pid:[10]")
    assert runtime_pid.observe_process(identity, proc_root=tmp_path).state is runtime_pid.REUSED


def test_process_identity_owns_protocol_serialization() -> None:
    identity = runtime_pid.ProcessIdentity(
        pid=123,
        pid_namespace="pid:[10]",
        start_ticks=100,
    )

    assert identity.to_payload() == {
        "pid": 123,
        "pid_namespace": "pid:[10]",
        "start_ticks": 100,
    }
    assert runtime_pid.ProcessIdentity.from_payload(identity.to_payload()) == identity
    assert (
        runtime_pid.ProcessIdentity.from_payload(
            {"pid": True, "pid_namespace": "pid:[10]", "start_ticks": 100}
        )
        is None
    )
