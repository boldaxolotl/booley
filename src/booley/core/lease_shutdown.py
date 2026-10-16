"""One bounded shutdown window shared by nested scratch process supervisors.

The file is a rendezvous between related live processes, never deletion
authority by itself. An owner publishes terminal only after reaping its tree.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import math
import os
import signal
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV = "BOOLEY_SCRATCH_SHUTDOWN"
GRACE_SECONDS = 10.0
SETTLE_SECONDS = 5.0
MAX_MEMBERS = 64


def _identity(pid: int) -> dict[str, Any] | None:
    from booley.core.temporary_workspace import process_identity

    return process_identity(pid)


def ancestors(pid: int) -> list[dict[str, Any]]:
    """Capture creation-bound ancestry with an explicit depth limit."""
    result = []
    for _ in range(64):
        identity = _identity(pid)
        if identity is None:
            break
        result.append(identity)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 0:
            break
    else:
        raise RuntimeError("scratch supervisor ancestry bound exceeded")
    return result


def descendants(pid: int) -> set[int]:
    """Include children forked from any thread, including detached sessions."""
    found: set[int] = set()
    pending = [pid]
    for _ in range(65536):
        if not pending:
            return found
        parent = pending.pop()
        try:
            tasks = list(Path(f"/proc/{parent}/task").iterdir())
            if len(tasks) > 4096:
                raise RuntimeError("scratch thread bound exceeded")
            for task in tasks:
                for value in (task / "children").read_text().split():
                    child = int(value)
                    if child not in found:
                        found.add(child)
                        pending.append(child)
        except FileNotFoundError:
            continue
    raise RuntimeError("scratch descendant bound exceeded")


def signal_identity(identity: dict[str, Any], signum: int) -> None:
    """Bind signal delivery to the observed process rather than a reusable PID."""
    pid = identity["pid"]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    libc.pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    fd = libc.pidfd_open(pid, 0)
    if fd < 0:
        error = ctypes.get_errno()
        if error == 3:
            return
        raise OSError(error, os.strerror(error))
    try:
        if _identity(pid) == identity and libc.pidfd_send_signal(fd, signum, None, 0) != 0:
            error = ctypes.get_errno()
            if error != 3:
                raise OSError(error, os.strerror(error))
    finally:
        os.close(fd)


def _valid_member(value: Any) -> bool:
    from booley.core.temporary_workspace import _valid_process_identity

    return (
        isinstance(value, dict)
        and set(value) == {"identity", "ancestors", "terminal", "roots", "token"}
        and _valid_process_identity(value["identity"])
        and type(value["terminal"]) is bool
        and isinstance(value["ancestors"], list)
        and 0 < len(value["ancestors"]) <= 64
        and all(_valid_process_identity(item) for item in value["ancestors"])
        and value["identity"] in value["ancestors"]
        and isinstance(value["token"], str)
        and len(value["token"]) == 32
        and isinstance(value["roots"], list)
        and len(value["roots"]) <= 64
        and all(_valid_root_reference(item) for item in value["roots"])
    )


def _valid_root_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"path", "lease", "root"}
        and isinstance(value["path"], str)
        and Path(value["path"]).is_absolute()
        and isinstance(value["lease"], str)
        and len(value["lease"]) == 32
        and isinstance(value["root"], list)
        and len(value["root"]) == 2
        and all(type(number) is int and number >= 0 for number in value["root"])
    )


def _valid_stop(value: Any) -> bool:
    from booley.core.temporary_workspace import _valid_process_identity

    return (
        isinstance(value, dict)
        and set(value) == {"owner", "signal", "deadline", "signaled"}
        and _valid_process_identity(value["owner"])
        and type(value["signal"]) is int
        and value["signal"] in {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
        and type(value["deadline"]) in {int, float}
        and math.isfinite(value["deadline"])
        and value["deadline"] > 0
        and isinstance(value["signaled"], dict)
        and len(value["signaled"]) <= 65536
        and all(
            isinstance(pid, str) and pid.isdecimal() and type(start) is int and start >= 0
            for pid, start in value["signaled"].items()
        )
    )


class ShutdownGroup:
    """Shared deadlines and creation identities for one actual process tree."""

    def __init__(self, path: Path) -> None:
        self.path: Path = path

    @classmethod
    def create(cls, parent: Path) -> ShutdownGroup:
        path = parent / (".booley-shutdown-" + uuid.uuid4().hex)
        path.mkdir(mode=0o700)
        group = cls(path)
        with group.locked(initial=True) as data:
            data.update(owner=_identity(os.getpid()), members={}, stops={}, terminal=False)
        return group

    @classmethod
    def inherited(cls) -> ShutdownGroup | None:
        raw = os.environ.get(ENV)
        if not raw:
            return None
        group = cls(Path(raw))
        with group.locked() as data:
            if data["terminal"] or data["owner"] not in ancestors(os.getpid()):
                raise ValueError("scratch shutdown owner is not a live ancestor")
        return group

    @contextlib.contextmanager
    def locked(self, *, initial: bool = False) -> Iterator[dict[str, Any]]:
        import fcntl

        from booley.core.temporary_workspace import _open_regular, _publish_json

        info = self.path.lstat()
        if self.path != self.path.resolve() or info.st_uid != os.getuid():
            raise ValueError("scratch shutdown crosses an untrusted directory")
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("scratch shutdown directory is not private")
        fd = _open_regular(self.path / "lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = {} if initial else self._read()
            before = json.dumps(data, sort_keys=True)
            yield data
            if initial or json.dumps(data, sort_keys=True) != before:
                _publish_json(self.path / "state.json", data)
        finally:
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        from booley.core.temporary_workspace import _open_regular, _valid_process_identity

        fd = _open_regular(self.path / "state.json", os.O_RDONLY)
        try:
            raw = os.read(fd, 2097153)
        finally:
            os.close(fd)
        data = json.loads(raw)
        if (
            len(raw) > 2097152
            or set(data) != {"owner", "members", "stops", "terminal"}
            or not _valid_process_identity(data["owner"])
            or type(data["terminal"]) is not bool
            or not isinstance(data["members"], dict)
            or len(data["members"]) > MAX_MEMBERS
            or not isinstance(data["stops"], dict)
            or len(data["stops"]) > MAX_MEMBERS + 1
            or any(not _valid_member(value) for value in data["members"].values())
            or any(not _valid_stop(value) for value in data["stops"].values())
        ):
            raise ValueError("invalid scratch shutdown state")
        return data

    def register(self, roots: tuple[Path, ...], token: str) -> None:
        from booley.core.temporary_workspace import _validated_marker

        references = []
        for root in roots:
            marker, _ = _validated_marker(root)
            if marker["launches"].get(token) != {"state": "pending"}:
                raise ValueError("scratch shutdown requires a pending launch")
            references.append(
                {"path": str(root), "lease": marker["lease"], "root": marker["root"]}
            )
        identity = _identity(os.getpid())
        with self.locked() as data:
            data["members"] = {
                key: value
                for key, value in data["members"].items()
                if not value["terminal"]
                or _identity(value["identity"]["pid"]) == value["identity"]
            }
            data["stops"] = {
                key: value
                for key, value in data["stops"].items()
                if _identity(value["owner"]["pid"]) == value["owner"]
                or any(
                    not member["terminal"] and value["owner"] in member["ancestors"]
                    for member in data["members"].values()
                )
            }
            if len(data["members"]) >= MAX_MEMBERS:
                raise RuntimeError("scratch supervisor concurrency limit exceeded")
            data["members"][str(os.getpid())] = {
                "identity": identity,
                "roots": references,
                "token": token,
                "ancestors": ancestors(os.getpid()),
                "terminal": False,
            }

    def stop(self, signum: int) -> None:
        with self.locked() as data:
            key = str(os.getpid())
            if key not in data["stops"]:
                if len(data["stops"]) >= MAX_MEMBERS + 1:
                    raise RuntimeError("scratch shutdown request bound exceeded")
                data["stops"][key] = {
                    "owner": _identity(os.getpid()),
                    "signal": signum,
                    "deadline": time.monotonic() + GRACE_SECONDS,
                    "signaled": {},
                }

    def tick(self) -> float | None:
        chain = ancestors(os.getpid())
        children = descendants(os.getpid())
        with self.locked() as data:
            own = data["members"].get(str(os.getpid()))
            chain = own["ancestors"] if own is not None else chain
            stops = [value for value in data["stops"].values() if value["owner"] in chain]
            if not stops:
                return None
            stop = min(stops, key=lambda value: value["deadline"])
            guards = {
                value["identity"]["pid"]: value["identity"] for value in data["members"].values()
            }
            expired = time.monotonic() >= stop["deadline"]
            for pid in children:
                identity = _identity(pid)
                if identity is None or guards.get(pid) == identity:
                    continue
                key = str(pid)
                delivered = any(
                    request["signaled"].get(key) == identity["start"]
                    for request in data["stops"].values()
                )
                if expired or not delivered:
                    signal_identity(identity, signal.SIGKILL if expired else stop["signal"])
                    stop["signaled"][key] = identity["start"]
            return stop["deadline"]

    def complete(self, *, owner: bool = False) -> None:
        members = []
        with self.locked() as data:
            if owner:
                if data["owner"] != _identity(os.getpid()):
                    raise ValueError("scratch terminal publication requires its owner")
                data["terminal"] = True
                members = list(data["members"].values())
            member = data["members"].get(str(os.getpid()))
            if member is not None:
                member["terminal"] = True
        for member in members:
            self._complete_roots(member)

    def _complete_roots(self, member: dict[str, Any]) -> None:
        from booley.core.temporary_workspace import _exclusion, _validated_marker, _write_marker

        for reference in member["roots"]:
            root = Path(reference["path"])
            try:
                with _exclusion(root.parent):
                    marker, _ = _validated_marker(root)
                    if (
                        marker["lease"] != reference["lease"]
                        or marker["root"] != reference["root"]
                    ):
                        continue
                    state = marker["launches"].get(member["token"])
                    if state == {"state": "pending"} or (
                        isinstance(state, dict) and state.get("supervisor") == member["identity"]
                    ):
                        marker["launches"][member["token"]] = {"state": "complete"}
                        _write_marker(root, marker)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Terminal scratch proof could not be refreshed for %s: %s", root, exc
                )

    def active(self) -> bool:
        with self.locked() as data:
            return any(not member["terminal"] for member in data["members"].values())
