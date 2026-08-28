"""Recover an execution after its original in-runtime supervisor disappears."""

from __future__ import annotations

import contextlib
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import as_int
from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    RUNTIME_EXECUTION_ENV,
    ExecutionId,
    atomic_write_json,
    execution_paths,
    read_json,
    request_cancellation,
)
from booley.runtime.pid import (
    DEAD,
    REUSED,
    RUNNING,
    UNKNOWN,
    ZOMBIE,
    ProcessIdentity,
    capture_process_identity,
    observe_process,
)
from booley.runtime.process_tree import descendant_pids
from booley.runtime.timefmt import utc_now_rfc3339

_POLL_SECONDS = 0.05
_RECOVERY_STAGES = tuple(
    (signum, timeout_s)
    for signal_name, timeout_s in (
        ("SIGINT", 2.0),
        ("SIGTERM", 2.0),
        ("SIGKILL", 5.0),
    )
    if (signum := getattr(signal, signal_name, None)) is not None
)


@dataclass(frozen=True)
class _ExecutionProcesses:
    identities: tuple[ProcessIdentity, ...]
    complete: bool


def _execution_marker(execution_id: ExecutionId) -> bytes:
    return f"{RUNTIME_EXECUTION_ENV}={execution_id}".encode()


def _matches_execution(proc: Path, marker: bytes) -> bool | None:
    try:
        environ = (proc / "environ").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        try:
            state = (proc / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
            if state == "Z":
                return False
        except (OSError, IndexError):
            pass
        try:
            return False if proc.stat().st_uid != os.geteuid() else None
        except OSError:
            return False
    return marker in environ.split(b"\0")


def _scan_execution(execution_id: ExecutionId) -> _ExecutionProcesses:
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return _ExecutionProcesses((), False)
    marker = _execution_marker(execution_id)
    identities: list[ProcessIdentity] = []
    complete = True
    for proc in entries:
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        matches = _matches_execution(proc, marker)
        if matches is None:
            complete = False
        elif matches:
            identity = capture_process_identity(int(proc.name), proc_root=proc_root)
            if identity is None:
                complete = False
            else:
                state = observe_process(identity).state
                if state is RUNNING:
                    identities.append(identity)
                elif state is UNKNOWN:
                    complete = False
    return _ExecutionProcesses(tuple(identities), complete)


def _signal_execution(processes: _ExecutionProcesses, signum: int) -> None:
    for identity in processes.identities:
        if observe_process(identity).state is not RUNNING:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(identity.pid, signum)


def _wait_for_empty(execution_id: ExecutionId, signum: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        processes = _scan_execution(execution_id)
        if not processes.identities and processes.complete:
            return True
        _signal_execution(processes, signum)
        time.sleep(_POLL_SECONDS)
    processes = _scan_execution(execution_id)
    return not processes.identities and processes.complete


def _publish_recovered_terminal(execution_id: ExecutionId) -> None:
    paths = execution_paths(execution_id)
    current = read_json(paths.record) or {}
    exit_code = as_int(current.get("exit_code"), 125) or 125
    atomic_write_json(
        paths.record,
        {
            **current,
            "schema_version": PROTOCOL_VERSION,
            "state": "terminal",
            "exit_code": exit_code,
            "tree_terminal": True,
            "terminal_cause": "orphan_recovered",
            "updated_at": utc_now_rfc3339(),
        },
    )


def recover_execution(raw_execution_id: str | ExecutionId) -> bool:
    """Request cancellation and prove a failed supervisor's execution tree empty."""
    execution_id = ExecutionId(raw_execution_id)
    paths = execution_paths(execution_id)
    record = read_json(paths.record)
    if record is not None and record.get("state") == "terminal":
        return record.get("tree_terminal") is True
    request_cancellation(paths, signum=signal.SIGINT, reason="lease_recovery")
    supervisor = ProcessIdentity.from_payload(
        record.get("supervisor") if record is not None else None
    )
    if supervisor is not None and observe_process(supervisor).state in {RUNNING, UNKNOWN}:
        return False
    if os.environ.get(RUNTIME_EXECUTION_ENV) == execution_id:
        return False
    for signum, timeout_s in _RECOVERY_STAGES:
        if _wait_for_empty(execution_id, signum, timeout_s):
            _publish_recovered_terminal(execution_id)
            return True
    return False


def _discover_owned_processes(owner: ProcessIdentity, known: dict[int, ProcessIdentity]) -> None:
    if observe_process(owner).state is not RUNNING:
        return
    known[owner.pid] = owner
    for pid in descendant_pids(owner.pid):
        identity = capture_process_identity(pid)
        if identity is not None:
            known[pid] = identity


def _running_owned_processes(
    known: dict[int, ProcessIdentity], owner_pid: int
) -> tuple[list[ProcessIdentity], bool]:
    running: list[ProcessIdentity] = []
    complete = True
    for identity in known.values():
        state = observe_process(identity).state
        if state is RUNNING:
            running.append(identity)
        elif state is UNKNOWN:
            complete = False
    running.sort(key=lambda identity: identity.pid == owner_pid)
    return running, complete


def _wait_for_owned_tree(
    owner: ProcessIdentity,
    known: dict[int, ProcessIdentity],
    signum: int,
    timeout_s: float,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _discover_owned_processes(owner, known)
        running, complete = _running_owned_processes(known, owner.pid)
        if not running and complete:
            return True
        _signal_execution(_ExecutionProcesses(tuple(running), complete), signum)
        time.sleep(_POLL_SECONDS)
    running, complete = _running_owned_processes(known, owner.pid)
    return not running and complete


def recover_process_owner(owner: ProcessIdentity) -> bool:
    """Cancel one expired process-owned tree without signaling a reused PID."""
    state = observe_process(owner).state
    if state in {DEAD, REUSED, ZOMBIE}:
        return True
    if state is UNKNOWN:
        return False
    known = {owner.pid: owner}
    for signum, timeout_s in _RECOVERY_STAGES:
        if _wait_for_owned_tree(owner, known, signum, timeout_s):
            return True
    return False
