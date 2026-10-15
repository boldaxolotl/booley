"""Retain scratch guards until a Linux native command's complete descendant exit."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

# A script invocation must work in source checkouts as well as installed wheels.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from booley.runtime.temporary_workspace import (
    complete_launch,
    process_identity,
    register_launch,
)

_POLL_S = 0.02
_STOP_S = 3.0
_MAX_PROCESSES = 65536


class _Signals:
    def __init__(self) -> None:
        self.requested: int | None = None

    def receive(self, signum: int, frame: FrameType | None) -> None:
        self.requested = self.requested or signum


def _subreaper() -> None:
    if sys.platform != "linux":
        raise RuntimeError("scratch descendant supervision requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _direct_children() -> list[int]:
    # The supervisor is single-threaded. Kernel children includes adopted setsid
    # descendants once their intermediate parents have exited.
    values = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    if len(values) > _MAX_PROCESSES:
        raise RuntimeError("scratch supervisor child bound exceeded")
    return [int(value) for value in values]


def _reap(leader: subprocess.Popen) -> bool:
    leader.poll()
    for _ in range(_MAX_PROCESSES):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if not pid:
            return False
        if pid == leader.pid:
            leader.returncode = os.waitstatus_to_exitcode(status)
    raise RuntimeError("scratch supervisor reap bound exceeded")


def _signal_children(signum: int) -> None:
    for pid in _direct_children():
        identity = process_identity(pid)
        if identity is None:
            raise RuntimeError("cannot prove scratch child creation identity")
        # Recheck immediately before signalling; never act on a reused PID.
        if process_identity(pid) == identity:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signum)


def _wait_tree(leader: subprocess.Popen, pending: _Signals) -> bool:
    stopping: float | None = None
    # The native command's lifetime is intentional. Shutdown, once requested,
    # has a strict deadline; an unprovable tree stays marked for preservation.
    while leader.returncode is None or _direct_children():
        if _reap(leader):
            return True
        if pending.requested is not None:
            stopping = time.monotonic() if stopping is None else stopping
            elapsed = time.monotonic() - stopping
            if elapsed >= _STOP_S:
                return _reap(leader)
            _signal_children(pending.requested if elapsed < 0.5 else signal.SIGKILL)
        time.sleep(_POLL_S)
    return _reap(leader)


def supervise(payload: str, command: list[str]) -> int:
    """Forward stdio unchanged, adopting/reaping detached native descendants."""
    data = json.loads(payload)
    if set(data) not in ({"roots", "token"}, {"roots", "token", "fds"}) or not isinstance(
        data["roots"], list
    ):
        raise ValueError("invalid scratch launch payload")
    if not 0 < len(data["roots"]) <= 64 or not isinstance(data["token"], str):
        raise ValueError("invalid scratch launch bounds")
    roots = tuple(Path(root) for root in data["roots"])
    _subreaper()
    inherited = data.get("fds", [])
    if (
        not isinstance(inherited, list)
        or len(inherited) > 64
        or any(type(fd) is not int or fd < 0 for fd in inherited)
    ):
        raise ValueError("invalid inherited scratch descriptors")
    descriptors = register_launch(roots, data["token"], tuple(inherited))
    pending = _Signals()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, pending.receive)
    try:
        try:
            leader = subprocess.Popen(command, start_new_session=True)
        except OSError:
            complete_launch(roots, data["token"])
            raise
        terminal = _wait_tree(leader, pending)
        if not terminal:
            print(
                "Scratch descendants remain unproven; preserving their workspace", file=sys.stderr
            )
            return 125
        complete_launch(roots, data["token"])
        return leader.returncode or 0
    finally:
        for fd in descriptors:
            os.close(fd)


def main() -> None:
    status = supervise(sys.argv[1], sys.argv[2:])
    if status < 0:
        signum = -status
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    raise SystemExit(status)


if __name__ == "__main__":
    main()
