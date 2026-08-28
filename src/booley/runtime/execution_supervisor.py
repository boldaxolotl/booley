"""Supervise one Runtime Attachment command through complete process-tree exit.

The host-side Runtime Attachment owns a durable record and requests cancellation
through a sibling control file.  This in-runtime supervisor is a Linux
subreaper: descendants that outlive their immediate parent are adopted here,
so a child that calls ``setsid()`` cannot escape scoped cleanup.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from types import FrameType
from typing import Any

from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    ExecutionPaths,
    atomic_write_json,
    execution_paths,
    force_cancellation_requested,
    read_attachment_heartbeat,
    read_json,
    request_cancellation,
)
from booley.runtime.pid import ProcessIdentity, capture_process_identity
from booley.runtime.process_tree import descendant_pids
from booley.runtime.timefmt import utc_now_rfc3339

_PR_SET_CHILD_SUBREAPER = 36
_POLL_SECONDS = 0.02
_FORCE_REAP_SECONDS = 5.0


@dataclass
class _RuntimeSignals:
    signum: int | None = None
    count: int = 0

    def receive(self, signum: int, _frame: FrameType | None) -> None:
        self.signum = self.signum or signum
        self.count += 1


@contextlib.contextmanager
def _capture_runtime_signals(pending: _RuntimeSignals):
    signals = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
    previous = {signum: signal.getsignal(signum) for signum in signals}
    try:
        for signum in signals:
            signal.signal(signum, pending.receive)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _become_subreaper() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Runtime Attachment execution supervision requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def _identity_payload(identity: ProcessIdentity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "pid": identity.pid,
        "pid_namespace": identity.pid_namespace,
        "start_ticks": identity.start_ticks,
    }


def _execution_record(
    *,
    state: str,
    supervisor: ProcessIdentity | None,
    leader: ProcessIdentity | None,
    exit_code: int | None = None,
    tree_terminal: bool = False,
    terminal_cause: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": PROTOCOL_VERSION,
        "state": state,
        "runtime_identity": supervisor.pid_namespace if supervisor is not None else None,
        "supervisor": _identity_payload(supervisor),
        "leader": _identity_payload(leader),
        "exit_code": exit_code,
        "tree_terminal": tree_terminal,
        "terminal_cause": terminal_cause,
        "updated_at": utc_now_rfc3339(),
    }


def _signal_processes(pids: set[int], signum: int) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signum)


def _signal_owned_tree(signum: int) -> set[int]:
    pids = set(descendant_pids(os.getpid()))
    _signal_processes(pids, signum)
    return pids


def _reap_adopted(child: subprocess.Popen[Any]) -> None:
    child.poll()
    if child.returncode is None:
        return
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _wait_for_tree_exit(
    child: subprocess.Popen[Any],
    timeout_s: float,
    paths: ExecutionPaths,
    *,
    watch_force: bool,
    repeated_signal: int,
    signaled: set[int],
) -> tuple[bool, bool]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = set(descendant_pids(os.getpid()))
        targets = current if repeated_signal == signal.SIGKILL else current - signaled
        _signal_processes(targets, repeated_signal)
        signaled.update(current)
        _reap_adopted(child)
        if not descendant_pids(os.getpid()):
            return True, False
        cancel = read_json(paths.cancel)
        if watch_force and (
            force_cancellation_requested(paths)
            or (cancel is not None and cancel.get("force") is True)
        ):
            return False, True
        time.sleep(_POLL_SECONDS)
    _reap_adopted(child)
    return not descendant_pids(os.getpid()), False


def _terminate_tree(
    child: subprocess.Popen[Any],
    paths: ExecutionPaths,
    *,
    grace_seconds: float,
    initial_signal: int,
    force: bool = False,
) -> bool:
    stages = [(signal.SIGKILL, _FORCE_REAP_SECONDS)] if force else [
        (initial_signal, grace_seconds),
        (signal.SIGTERM, grace_seconds),
        (signal.SIGKILL, _FORCE_REAP_SECONDS),
    ]
    for signum, timeout_s in stages:
        signaled = _signal_owned_tree(signum)
        terminal, force_requested = _wait_for_tree_exit(
            child,
            timeout_s,
            paths,
            watch_force=signum != signal.SIGKILL,
            repeated_signal=signum,
            signaled=signaled,
        )
        if terminal:
            return True
        if force_requested:
            signaled = _signal_owned_tree(signal.SIGKILL)
            terminal, _unused = _wait_for_tree_exit(
                child,
                _FORCE_REAP_SECONDS,
                paths,
                watch_force=False,
                repeated_signal=signal.SIGKILL,
                signaled=signaled,
            )
            return terminal
    return False


def _normal_exit_code(returncode: int) -> int:
    return 128 + (-returncode) if returncode < 0 else returncode


def _cancellation_request(paths: ExecutionPaths) -> tuple[str, bool, int] | None:
    cancel = read_json(paths.cancel)
    force = force_cancellation_requested(paths)
    if cancel is None:
        return ("cancelled", True, signal.SIGINT) if force else None
    reason = cancel.get("reason")
    signum = cancel.get("signum")
    return (
        reason if isinstance(reason, str) else "cancelled",
        cancel.get("force") is True or force,
        signum if isinstance(signum, int) and signum > 0 else signal.SIGINT,
    )


def _wait_until_stop(
    child: subprocess.Popen[Any],
    paths: ExecutionPaths,
    attachment_timeout_s: float,
    pending: _RuntimeSignals,
) -> tuple[str, bool, int]:
    generation = read_attachment_heartbeat(paths)
    if generation is None:
        return "attachment_missing", False, signal.SIGTERM
    last_change = time.monotonic()
    while child.poll() is None:
        if pending.signum is not None:
            return "runtime_signal", pending.count > 1, pending.signum
        cancellation = _cancellation_request(paths)
        if cancellation is not None:
            return cancellation
        current = read_attachment_heartbeat(paths)
        if current != generation:
            generation = current
            last_change = time.monotonic()
        if time.monotonic() - last_change >= attachment_timeout_s:
            return "attachment_expired", False, signal.SIGINT
        time.sleep(_POLL_SECONDS)
    return "exited", False, signal.SIGTERM


def _finish_prestart_cancellation(
    paths: ExecutionPaths,
    supervisor: ProcessIdentity | None,
    cancellation: tuple[str, bool, int],
) -> int:
    reason, _force, signum = cancellation
    exit_code = 128 + signum
    atomic_write_json(
        paths.record,
        _execution_record(
            state="terminal",
            supervisor=supervisor,
            leader=None,
            exit_code=exit_code,
            tree_terminal=True,
            terminal_cause=reason,
        ),
    )
    return exit_code


def _finish_owned_tree(
    child: subprocess.Popen[Any],
    paths: ExecutionPaths,
    supervisor: ProcessIdentity | None,
    leader: ProcessIdentity | None,
    stop: tuple[str, bool, int],
    grace_seconds: float,
) -> int:
    terminal_cause, force, requested_signal = stop
    cancelled = terminal_cause != "exited"
    if cancelled:
        cancelling = _execution_record(state="cancelling", supervisor=supervisor, leader=leader)
        atomic_write_json(paths.record, cancelling)
    root_returncode = child.returncode
    tree_terminal = _terminate_tree(
        child,
        paths,
        grace_seconds=grace_seconds,
        initial_signal=requested_signal,
        force=force,
    )
    child.poll()
    root_returncode = child.returncode if root_returncode is None else root_returncode
    if cancelled and root_returncode is not None and root_returncode >= 0:
        exit_code = root_returncode
    else:
        exit_code = 128 + requested_signal if cancelled else _normal_exit_code(root_returncode or 0)
    atomic_write_json(
        paths.record,
        _execution_record(
            state="terminal" if tree_terminal else "unrecoverable",
            supervisor=supervisor,
            leader=leader,
            exit_code=exit_code,
            tree_terminal=tree_terminal,
            terminal_cause=terminal_cause,
        ),
    )
    return exit_code if tree_terminal else 125


def supervise(
    paths: ExecutionPaths,
    command: list[str],
    *,
    grace_seconds: float,
    attachment_timeout_s: float,
    tty: bool = False,
) -> int:
    """Run *command* and return only after its complete owned tree is terminal."""
    _become_subreaper()
    supervisor = capture_process_identity(os.getpid())
    starting = _execution_record(state="starting", supervisor=supervisor, leader=None)
    atomic_write_json(paths.record, starting)
    if read_attachment_heartbeat(paths) is None:
        failed = _execution_record(
            state="unrecoverable",
            supervisor=supervisor,
            leader=None,
            exit_code=125,
            terminal_cause="attachment_missing",
        )
        atomic_write_json(paths.record, failed)
        return 125
    cancellation = _cancellation_request(paths)
    if cancellation is not None:
        return _finish_prestart_cancellation(paths, supervisor, cancellation)
    grouped_tty = tty and os.isatty(0) and hasattr(os, "tcsetpgrp")
    child = subprocess.Popen(command, process_group=0 if grouped_tty else None)
    pending = _RuntimeSignals()
    with _capture_runtime_signals(pending), _foreground_child(child, enabled=grouped_tty):
        leader = capture_process_identity(child.pid)
        running = _execution_record(state="running", supervisor=supervisor, leader=leader)
        atomic_write_json(paths.record, running)
        stop = _wait_until_stop(
            child, paths, attachment_timeout_s, pending
        )
    return _finish_owned_tree(
        child,
        paths,
        supervisor,
        leader,
        stop,
        grace_seconds,
    )


@contextlib.contextmanager
def _foreground_child(child: subprocess.Popen[Any], *, enabled: bool):
    if not enabled:
        yield
        return
    previous = signal.getsignal(signal.SIGTTOU)
    try:
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(0, child.pid)
        yield
    finally:
        with contextlib.suppress(OSError):
            os.tcsetpgrp(0, os.getpgrp())
        signal.signal(signal.SIGTTOU, previous)


def _positive_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booley-execution-supervisor")
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run")
    run.add_argument("--execution-id", required=True)
    run.add_argument("--grace-seconds", type=_positive_seconds, default=2.0)
    run.add_argument("--attachment-timeout-seconds", type=_positive_seconds, default=5.0)
    run.add_argument("--tty", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--execution-id", required=True)
    cancel.add_argument("--force", action="store_true")
    cancel.add_argument("--signal", type=int, default=signal.SIGINT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = execution_paths(args.execution_id)
    if args.action == "cancel":
        request_cancellation(paths, force=args.force, signum=args.signal)
        return 0
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("run requires a command after --")
    return supervise(
        paths,
        command,
        grace_seconds=args.grace_seconds,
        attachment_timeout_s=args.attachment_timeout_seconds,
        tty=args.tty,
    )


if __name__ == "__main__":
    raise SystemExit(main())
