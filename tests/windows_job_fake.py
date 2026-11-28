"""A fake of the Windows Job Object API (kernel32, ntdll) and ``taskkill``."""

from __future__ import annotations

import ctypes
import subprocess
from collections.abc import Callable, Iterable
from typing import Any

import pytest

from booley.runtime import process_group
from tests.host_platform_fake import use_sys_platform

JOB = 7
_PROCESS_HANDLE_BASE = 1000
_ERROR_MORE_DATA = 234
_ERROR_ACCESS_DENIED = 5
_ACCOUNTING, _PROCESS_ID_LIST = 1, 3


class _Call:
    def __init__(self, implementation: Callable[..., Any]) -> None:
        self.implementation = implementation

    def __call__(self, *args: Any) -> Any:
        return self.implementation(*args)


class FakeLeader:
    """The ``Popen`` of an owned child: running until ``kill``, unless it has a *returncode*."""

    stdin = stdout = stderr = None

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("leader", timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1


class WindowsJobFake:
    """One job holding *members*; *outsiders* are listed PIDs that are not in it.

    *fail* names the call that fails: ``open``, ``create``, ``assign``, ``resume``,
    ``query``, ``terminate``, or ``truncate`` (a PID list reported as incomplete).
    *on_request* runs during each graceful ``taskkill``.
    """

    def __init__(
        self,
        *,
        members: Iterable[int] = (),
        outsiders: Iterable[int] = (),
        fail: str | None = None,
        taskkill_returncode: int = 0,
        on_request: Callable[[WindowsJobFake], None] | None = None,
    ) -> None:
        self.members = list(members)
        self.outsiders = set(outsiders)
        self.fail = fail
        self.taskkill_returncode = taskkill_returncode
        self.on_request = on_request
        self.taskkills: list[tuple[list[str], float]] = []
        self.resumed: list[int] = []
        # "assign", "resume", "terminate" and "close <handle>", in call order.
        self.events: list[str] = []

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch, **options: Any) -> WindowsJobFake:
        """Make this process a Windows host whose job and ``taskkill`` calls reach the fake."""
        fake = cls(**options)
        kernel32 = type("Kernel32", (), {})()
        for name in (
            "OpenProcess",
            "CloseHandle",
            "CreateJobObjectW",
            "AssignProcessToJobObject",
            "IsProcessInJob",
            "QueryInformationJobObject",
            "TerminateJobObject",
        ):
            setattr(kernel32, name, _Call(getattr(fake, f"_{name}")))
        ntdll = type("Ntdll", (), {})()
        ntdll.NtResumeProcess = _Call(fake._resume)
        libraries = {"kernel32": kernel32, "ntdll": ntdll}
        error = _ERROR_MORE_DATA if fake.fail == "truncate" else _ERROR_ACCESS_DENIED
        monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_: libraries[name], raising=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
        # boundary: ``taskkill`` is a Windows program.
        monkeypatch.setattr(process_group.subprocess, "run", fake._taskkill)
        use_sys_platform(monkeypatch, windows=True)
        return fake

    def _taskkill(self, command: list[str], *, timeout: float, **_: Any) -> Any:
        self.taskkills.append((command, timeout))
        if "/F" not in command and self.on_request is not None:
            self.on_request(self)
        return subprocess.CompletedProcess(command, self.taskkill_returncode)

    def _OpenProcess(self, _access: int, _inherit: bool, pid: int) -> int:  # noqa: N802
        return 0 if self.fail == "open" else _PROCESS_HANDLE_BASE + pid

    def _CloseHandle(self, handle: int) -> bool:  # noqa: N802
        self.events.append(f"close {handle}")
        return True

    def _CreateJobObjectW(self, _security: None, _name: None) -> int:  # noqa: N802
        return 0 if self.fail == "create" else JOB

    def _AssignProcessToJobObject(self, _job: int, _process: int) -> bool:  # noqa: N802
        self.events.append("assign")
        return self.fail != "assign"

    def _resume(self, handle: int) -> int:
        self.events.append("resume")
        self.resumed.append(handle - _PROCESS_HANDLE_BASE)
        return -1 if self.fail == "resume" else 0

    def _IsProcessInJob(self, handle: int, _job: int, inside: Any) -> bool:  # noqa: N802
        inside._obj.value = handle - _PROCESS_HANDLE_BASE not in self.outsiders
        return True

    def _QueryInformationJobObject(  # noqa: N802
        self, _job: int, information_class: int, into: Any, _size: int, _returned: None
    ) -> bool:
        if self.fail == "query":
            return False
        if information_class == _ACCOUNTING:
            into._obj.ActiveProcesses = len(self.members)
            return True
        assert information_class == _PROCESS_ID_LIST
        into._obj.NumberOfProcessIdsInList = len(self.members)
        into._obj.ProcessIdList[: len(self.members)] = self.members
        return self.fail != "truncate"

    def _TerminateJobObject(self, _job: int, _code: int) -> bool:  # noqa: N802
        self.events.append("terminate")
        if self.fail == "terminate":
            return False
        self.members.clear()
        return True
