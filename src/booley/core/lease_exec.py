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

from booley.core.lease_shutdown import ENV, SETTLE_SECONDS, ShutdownGroup
from booley.core.temporary_workspace import (
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


def _publish_result(roots: tuple[Path, ...], token: str, returncode: int) -> None:
    from booley.core.temporary_workspace import _publish_json

    for root in roots:
        _publish_json(
            root / (".booley-result-" + token + ".json"),
            {
                "supervisor": process_identity(os.getpid()),
                "returncode": returncode,
            },
        )


def _wait_tree(
    leader: subprocess.Popen,
    pending: _Signals,
    group: ShutdownGroup,
    roots: tuple[Path, ...],
    token: str,
) -> bool:
    published = False
    while leader.returncode is None or _direct_children():
        terminal = _reap(leader)
        if leader.returncode is not None and not published:
            _publish_result(roots, token, leader.returncode)
            published = True
            if not terminal:
                group.stop(signal.SIGTERM)
        if terminal:
            if not published:
                _publish_result(roots, token, leader.returncode or 0)
            return True
        if pending.requested is not None:
            group.stop(pending.requested)
        deadline = group.tick()
        if deadline is not None and time.monotonic() >= deadline + SETTLE_SECONDS:
            return _reap(leader)
        time.sleep(_POLL_S)
    _publish_result(roots, token, leader.returncode or 0)
    return _reap(leader)


def _prepare_supervisor(pending: _Signals, parent_pid: int) -> None:
    if os.getsid(0) != os.getpid():
        os.setsid()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, pending.receive)
    _subreaper()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "scratch parent-death registration failed")
    if os.getppid() != parent_pid:
        pending.requested = signal.SIGTERM


def supervise(payload: str, command: list[str]) -> int:
    """Forward stdio unchanged, adopting/reaping detached native descendants."""
    data = json.loads(payload)
    if set(data) not in (
        {"roots", "token"},
        {"roots", "token", "fds"},
        {"roots", "token", "fds", "parent"},
    ) or not isinstance(data["roots"], list):
        raise ValueError("invalid scratch launch payload")
    if not 0 < len(data["roots"]) <= 64 or not isinstance(data["token"], str):
        raise ValueError("invalid scratch launch bounds")
    roots = tuple(Path(root) for root in data["roots"])
    pending = _Signals()
    _prepare_supervisor(pending, data.get("parent", os.getppid()))
    inherited = data.get("fds", [])
    if (
        not isinstance(inherited, list)
        or len(inherited) > 64
        or any(type(fd) is not int or fd < 0 for fd in inherited)
    ):
        raise ValueError("invalid inherited scratch descriptors")
    inherited_group = ShutdownGroup.inherited()
    group = inherited_group or ShutdownGroup.create(roots[0])
    group.register(roots, data["token"])
    descriptors = register_launch(roots, data["token"], tuple(inherited), group=group)
    try:
        if pending.requested is not None:
            group.complete(owner=inherited_group is None)
            complete_launch(roots, data["token"])
            return -pending.requested
        try:
            leader = subprocess.Popen(
                command, start_new_session=True, env={**os.environ, ENV: str(group.path)}
            )
        except OSError:
            complete_launch(roots, data["token"])
            raise
        terminal = _wait_tree(leader, pending, group, roots, data["token"])
        if not terminal:
            print(
                "Scratch descendants remain unproven; preserving their workspace", file=sys.stderr
            )
            return 125
        group.complete(owner=inherited_group is None)
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
