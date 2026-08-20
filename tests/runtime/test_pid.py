"""Boundary tests for conservative process-liveness checks."""

from __future__ import annotations

import ctypes

import pytest

from booley.runtime import pid as runtime_pid


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
