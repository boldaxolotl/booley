"""Supervised command execution through a Sandbox attachment."""

from __future__ import annotations

import contextlib
import logging
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from booley.core.boundary import BoundaryError, as_str, require_int
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    RUNTIME_EXECUTION_ENV,
    ExecutionId,
    ExecutionPaths,
    execution_paths,
    gc_terminal_executions,
    read_json,
    request_cancellation,
    write_attachment_heartbeat,
)

_HEARTBEAT_INTERVAL_SECONDS = 0.25
_ATTACHMENT_TIMEOUT_SECONDS = 2.0
_RECOVERY_FORCE_SECONDS = 4.0
_RECOVERY_LIMIT_SECONDS = 12.0
_STARTUP_LIMIT_SECONDS = 2.0
_POLL_SECONDS = 0.05

# Causes that need no extra line: the command's own exit, the user's host signal, and a
# spawn failure the supervisor has already printed.
_QUIET_CAUSES = frozenset({"exited", "cancelled", "spawn_error"})
_LOSS_PHRASES = {
    "attachment_missing": "the Sandbox found no host attachment when the command started",
    "attachment_expired": "the host attachment stopped renewing, so the Sandbox stopped the command",
    "context_mismatch": "the command's execution context did not match this run",
    "transport_lost": "the `docker exec` connection to the Sandbox ended",
    "orphan_recovered": "its supervisor died and Booley stopped its remaining processes",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    """Authoritative terminal result from the in-Sandbox supervisor."""

    exit_code: int
    state: str
    tree_terminal: bool
    terminal_cause: str


class SupervisedExecutionDriver:
    """Drive one preallocated execution ID through bounded terminal proof."""

    def __init__(
        self,
        project_data: Path,
        *,
        project_root: Path,
        container: str,
        sandbox_name: str,
        cwd: str | None = None,
    ) -> None:
        self._project_data = project_data
        self._project_root = project_root
        self._container = container
        self._sandbox_name = sandbox_name
        self._cwd = cwd

    def run(self, execution_id: ExecutionId, transport_argv: list[str]) -> ExecutionResult:
        paths = execution_paths(execution_id, booley_dir=self._project_data)
        write_attachment_heartbeat(paths, generation=1)
        pending = _PendingSignals()
        with _capture_host_signals(pending):
            process = subprocess.Popen(transport_argv)
            try:
                result = _drive_execution(process, paths, pending)
            finally:
                _stop_transport(process)
        # Outside the signal capture: inspecting a hung Docker must stay interruptible.
        self._report(result)
        return result

    def _report(self, result: ExecutionResult) -> None:
        if self._cwd is not None and result.terminal_cause in {
            "protocol_unavailable",
            "protocol_mismatch",
        }:
            # A vanished directory and a broken image look the same here.
            logger.error(
                "Sandbox startup failed with selected directory %r; "
                "directory and protocol startup could not be distinguished",
                self._cwd,
            )
        else:
            _report_execution_failure(
                result, self._project_root, self._container, self._sandbox_name
            )


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
        "-P",
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
    request_cancellation(paths, force=True, signum=signal.SIGTERM, reason="protocol_mismatch")


def _force_recovery_if_due(
    paths: ExecutionPaths, pending: _PendingSignals, recovery_started: float | None, now: float
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
    process: subprocess.Popen, paths: ExecutionPaths, pending: _PendingSignals
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
            request_cancellation(paths, force=pending.count > 1, signum=pending.signum)
            recovery_started = recovery_started or now
        record_readable = True
        try:
            payload = read_json(paths.record)
        except OSError as exc:
            # A sharing error outlasting the read retries is "indeterminate this
            # pass", not "no record": skip the terminal/startup checks, poll again.
            logger.warning("Cannot read execution record %s this pass: %s", paths.record, exc)
            payload = None
            record_readable = False
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
            and record_readable
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


def _report_execution_failure(
    result: ExecutionResult, project_root: Path, container: str, sandbox_name: str
) -> None:
    """Log why the execution was lost; the exit code stays the caller's."""
    cause = result.terminal_cause
    if cause in _QUIET_CAUSES:
        return
    if cause in {"protocol_mismatch", "protocol_unavailable"}:
        from booley.runtime import session_runtime

        remedy = session_runtime.sandbox_remedy(project_root)
        logger.error("Sandbox execution protocol is unavailable or incompatible; %s", remedy)
    elif cause == "recovery_timeout":
        logger.error("%s", _lost_sandbox_message(project_root, container, sandbox_name))
    else:
        phrase = _LOSS_PHRASES.get(cause, f"cause {cause}")
        logger.error("Sandbox %r lost the command: %s", sandbox_name, phrase)


def _lost_sandbox_message(project_root: Path, container: str, sandbox_name: str) -> str:
    """Describe a recovery timeout from the Project's running Sandbox as it is now."""
    from booley.runtime import session_runtime

    try:
        running = session_runtime.resolve_running_sandbox(project_root)
    except session_runtime.SessionError as exc:
        return (
            f"lost the command in Sandbox {sandbox_name!r} (recovery timeout); "
            f"cannot inspect the Sandbox: {exc}"
        )
    if running is None:
        return (
            f"Sandbox {sandbox_name!r} stopped while the command ran, so the command was lost; "
            "start it again with `booley session up` on the host, then rerun the command"
        )
    if running.container_id != container:
        return (
            f"Sandbox {sandbox_name!r} was replaced while the command ran, "
            "so the command was lost; rerun it in the new Sandbox"
        )
    return (
        f"Sandbox {sandbox_name!r} is still running, but the command's execution supervisor "
        f"stopped reporting for {_RECOVERY_LIMIT_SECONDS:g} s (recovery timeout), "
        "so the command was lost"
    )


def run_command(
    project_root: Path,
    container_id: str,
    command: list[str],
    *,
    tty: bool,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    sandbox_name: str | None = None,
) -> ExecutionResult:
    """Run one explicit command and own it through complete scoped cleanup.

    *container_id* is the Sandbox container's immutable ID; a lost command is reported
    as "replaced" when the Project's running Sandbox has another one. *sandbox_name*
    names the Sandbox in failure messages and defaults to the ID.
    """
    from booley.runtime.session_runtime import _probe_command_cwd, exec_argv

    if cwd is not None:
        _probe_command_cwd(container_id, cwd)

    execution_id = ExecutionId(uuid.uuid4().hex)
    from booley.core.booley_dir import resolve_authoritative_booley_dir

    project_data = (
        resolve_authoritative_booley_dir(project_root)
        if cwd is not None
        else resolve_checkout_booley_dir(project_root)
    )
    gc_terminal_executions(project_data)
    supervisor = _supervisor_command(execution_id, command, tty=tty)
    docker_argv = exec_argv(
        container_id,
        supervisor,
        tty=tty,
        env={**(env or {}), RUNTIME_EXECUTION_ENV: execution_id},
        cwd=cwd,
    )
    driver = SupervisedExecutionDriver(
        project_data,
        project_root=project_root,
        container=container_id,
        sandbox_name=sandbox_name or container_id,
        cwd=cwd,
    )
    return driver.run(execution_id, docker_argv)
