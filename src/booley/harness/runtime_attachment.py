"""Supervised command execution through a Session Runtime attachment."""

from __future__ import annotations

import contextlib
import logging
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from booley.core.boundary import BoundaryError, as_str, require_int
from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    RUNTIME_EXECUTION_ENV,
    ExecutionPaths,
    execution_paths,
    gc_terminal_executions,
    read_json,
    request_cancellation,
    write_attachment_heartbeat,
)
from booley.runtime.project_dir import resolve_checkout_project_dir

_HEARTBEAT_INTERVAL_SECONDS = 0.25
_ATTACHMENT_TIMEOUT_SECONDS = 2.0
_RECOVERY_FORCE_SECONDS = 4.0
_RECOVERY_LIMIT_SECONDS = 12.0
_STARTUP_LIMIT_SECONDS = 2.0
_POLL_SECONDS = 0.05

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    """Authoritative terminal result from the in-runtime supervisor."""

    exit_code: int
    state: str
    tree_terminal: bool
    terminal_cause: str


@dataclass
class _PendingSignals:
    signum: int | None = None
    count: int = 0

    def receive(self, signum: int, _frame: FrameType | None) -> None:
        self.signum = self.signum or signum
        self.count += 1


@contextlib.contextmanager
def _capture_host_signals(pending: _PendingSignals):
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    try:
        for signum in signals:
            signal.signal(signum, pending.receive)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _supervisor_command(execution_id: str, command: list[str], *, tty: bool) -> list[str]:
    supervisor = [
        "python3",
        "-m",
        "booley.runtime.execution_supervisor",
        "run",
        "--execution-id",
        execution_id,
        "--attachment-timeout-seconds",
        str(_ATTACHMENT_TIMEOUT_SECONDS),
    ]
    if tty:
        supervisor.append("--tty")
    return [*supervisor, "--", *command]


def _terminal_result(payload: dict | None) -> ExecutionResult | None:
    if payload is None:
        return None
    if payload.get("schema_version") != PROTOCOL_VERSION:
        return ExecutionResult(125, "unrecoverable", False, "protocol_mismatch")
    state = payload.get("state")
    if state not in {"terminal", "unrecoverable"}:
        return None
    try:
        exit_code = require_int(payload.get("exit_code"), field="execution exit_code")
    except BoundaryError:
        exit_code = 125
    cause = as_str(payload.get("terminal_cause"), "unknown") or "unknown"
    return ExecutionResult(
        exit_code=exit_code,
        state=state,
        tree_terminal=payload.get("tree_terminal") is True,
        terminal_cause=cause,
    )


def _stop_transport(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=2)


def _request_protocol_shutdown(paths: ExecutionPaths) -> None:
    request_cancellation(
        paths,
        force=True,
        signum=signal.SIGTERM,
        reason="protocol_mismatch",
    )


def _force_recovery_if_due(
    paths: ExecutionPaths,
    pending: _PendingSignals,
    recovery_started: float | None,
    now: float,
) -> None:
    if recovery_started is None or now - recovery_started < _RECOVERY_FORCE_SECONDS:
        return
    request_cancellation(
        paths,
        force=True,
        signum=pending.signum or signal.SIGTERM,
        reason="cancelled" if pending.signum is not None else "transport_lost",
    )


def _drive_execution(
    process: subprocess.Popen,
    paths: ExecutionPaths,
    pending: _PendingSignals,
) -> ExecutionResult:
    generation = 1
    started_at = time.monotonic()
    next_heartbeat = started_at
    recovery_started: float | None = None
    transport_failed = False
    protocol_failure: ExecutionResult | None = None
    while (
        recovery_started is None or time.monotonic() - recovery_started < _RECOVERY_LIMIT_SECONDS
    ):
        now = time.monotonic()
        if now >= next_heartbeat:
            generation += 1
            write_attachment_heartbeat(paths, generation=generation)
            next_heartbeat = now + _HEARTBEAT_INTERVAL_SECONDS
        if pending.signum is not None:
            request_cancellation(
                paths,
                force=pending.count > 1,
                signum=pending.signum,
            )
            recovery_started = recovery_started or now
        payload = read_json(paths.record)
        result = _terminal_result(payload)
        if result is not None:
            if result.terminal_cause != "protocol_mismatch":
                return result
            protocol_failure = result
            _request_protocol_shutdown(paths)
            recovery_started = recovery_started or now
            if process.poll() is not None:
                return result
        if (
            process.poll() is not None
            and payload is None
            and now - started_at >= _STARTUP_LIMIT_SECONDS
        ):
            return ExecutionResult(125, "unrecoverable", False, "protocol_unavailable")
        if process.poll() is not None and not transport_failed:
            transport_failed = True
            request_cancellation(paths, signum=signal.SIGTERM, reason="transport_lost")
            recovery_started = recovery_started or now
        _force_recovery_if_due(paths, pending, recovery_started, now)
        time.sleep(_POLL_SECONDS)
    return protocol_failure or ExecutionResult(125, "unrecoverable", False, "recovery_timeout")


def _report_protocol_failure(result: ExecutionResult) -> None:
    if result.terminal_cause not in {"protocol_mismatch", "protocol_unavailable"}:
        return
    logger.error(
        "Session Runtime execution protocol is unavailable or incompatible; "
        "run `booley session refresh` and retry"
    )


def run_command(
    project_root: Path,
    container_name: str,
    command: list[str],
    *,
    tty: bool,
) -> ExecutionResult:
    """Run one explicit command and own it through complete scoped cleanup."""
    from booley.harness.session_runtime import exec_argv

    execution_id = uuid.uuid4().hex
    project_data = resolve_checkout_project_dir(project_root)
    gc_terminal_executions(project_data)
    paths = execution_paths(execution_id, project_dir=project_data)
    write_attachment_heartbeat(paths, generation=1)
    supervisor = _supervisor_command(execution_id, command, tty=tty)
    docker_argv = exec_argv(
        container_name,
        supervisor,
        tty=tty,
        env={RUNTIME_EXECUTION_ENV: execution_id},
    )
    pending = _PendingSignals()
    with _capture_host_signals(pending):
        process = subprocess.Popen(docker_argv)
        try:
            result = _drive_execution(process, paths, pending)
            _report_protocol_failure(result)
            return result
        finally:
            _stop_transport(process)
