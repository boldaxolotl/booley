"""Boundary tests for cross-platform runtime file locking."""

from __future__ import annotations

import errno
import io
import sys
from types import SimpleNamespace

import pytest

from booley.runtime import file_lock


class _MemoryFile(io.StringIO):
    def fileno(self) -> int:
        return 7


def test_windows_lock_seeds_empty_file_and_uses_byte_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = _MemoryFile()
    calls: list[tuple[int, int, int, int]] = []

    def locking(fd: int, mode: int, count: int) -> None:
        calls.append((fd, mode, count, handle.tell()))

    fake_msvcrt = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
    monkeypatch.setattr(file_lock.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    with file_lock.nonblocking_file_lock(handle):
        assert handle.getvalue() == "\0"
        assert handle.tell() == 0

    assert calls == [(7, 1, 1, 0), (7, 2, 1, 0)]


def test_lock_contention_has_specific_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def flock(_fd: int, _flags: int) -> None:
        raise OSError(errno.EAGAIN, "busy")

    fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=flock)
    monkeypatch.setattr(file_lock.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)

    with pytest.raises(file_lock.LockContentionError):
        file_lock.acquire_file_lock(_MemoryFile("x"))


def test_unrelated_lock_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    error = OSError(errno.EIO, "broken filesystem")

    def flock(_fd: int, _flags: int) -> None:
        raise error

    fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=flock)
    monkeypatch.setattr(file_lock.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)

    with pytest.raises(OSError) as raised:
        file_lock.acquire_file_lock(_MemoryFile("x"))

    assert raised.value is error


def test_try_lock_does_not_swallow_errors_from_the_protected_body(tmp_path) -> None:
    path = tmp_path / "lock"
    with (
        path.open("a+", encoding="utf-8") as handle,
        pytest.raises(file_lock.LockContentionError, match="body failure"),
        file_lock.try_file_lock(handle) as acquired,
    ):
        assert acquired
        raise file_lock.LockContentionError("body failure")
