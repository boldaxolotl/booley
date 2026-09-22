"""Thread-local binding for one preallocated supervised child execution."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path

from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    RUNTIME_EXECUTION_ENV,
    ExecutionId,
    atomic_write_json,
    execution_paths,
    read_json,
    write_attachment_heartbeat,
)
from booley.runtime.pid import capture_process_identity
from booley.runtime.platform_paths import kill_process_tree
from booley.runtime.timefmt import utc_now_rfc3339


class SupervisedProcessSet:
    """Track every command in one whole work item and cancel it as a unit."""

    def __init__(
        self,
        execution_id: ExecutionId | None,
        project_data: Path,
        cancelled: Callable[[], bool],
    ) -> None:
        self._execution_id = execution_id
        self._project_data = project_data
        self._processes: set[subprocess.Popen] = set()
        self._gate = threading.Lock()
        self._cancelled = cancelled

    def register(self, process: subprocess.Popen) -> None:
        with self._gate:
            self._processes.add(process)
            cancelled = self._cancelled()
            if cancelled:
                kill_process_tree(process)
        if self._execution_id is not None and not cancelled and not self._cancelled():
            identity = capture_process_identity(process.pid)
            self._write_record("running", identity, tree_terminal=False)

    def unregister(self, process: subprocess.Popen) -> None:
        with self._gate:
            self._processes.discard(process)
            active = bool(self._processes)
        if self._execution_id is not None and not active:
            paths = execution_paths(self._execution_id, project_dir=self._project_data)
            current = read_json(paths.record)
            if current is not None and current.get("state") == "running":
                self._write_record("waiting", None, tree_terminal=False)

    def terminate_all(self) -> None:
        with self._gate:
            processes = tuple(self._processes)
        for process in processes:
            kill_process_tree(process)

    def _write_record(self, state: str, leader, *, tree_terminal: bool) -> None:
        paths = execution_paths(self._execution_id, project_dir=self._project_data)
        atomic_write_json(
            paths.record,
            {
                "schema_version": PROTOCOL_VERSION,
                "state": state,
                "runtime_identity": None,
                "supervisor": None,
                "leader": leader.to_payload() if leader is not None else None,
                "exit_code": None,
                "tree_terminal": tree_terminal,
                "terminal_cause": None,
                "updated_at": utc_now_rfc3339(),
            },
        )


@dataclass(frozen=True, slots=True)
class SupervisedExecutionScope:
    execution_id: ExecutionId | None
    project_data: Path
    cancelled: Callable[[], bool]
    processes: SupervisedProcessSet = field(init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "processes",
            SupervisedProcessSet(self.execution_id, self.project_data, self.cancelled),
        )


_SCOPE: ContextVar[SupervisedExecutionScope | None] = ContextVar(
    "booley_supervised_execution_scope", default=None
)


@contextmanager
def supervised_execution_scope(
    scope: SupervisedExecutionScope,
) -> Iterator[None]:
    token: Token = _SCOPE.set(scope)
    stop = threading.Event()
    watcher = threading.Thread(
        target=_watch_scope_cancellation,
        args=(scope, stop),
        name="booley-campaign-process-cancellation",
        daemon=True,
    )
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(timeout=1)
        if scope.cancelled():
            scope.processes.terminate_all()
        _SCOPE.reset(token)


def _watch_scope_cancellation(scope: SupervisedExecutionScope, stop: threading.Event) -> None:
    while not stop.wait(0.01):
        if scope.cancelled():
            scope.processes.terminate_all()


def current_supervised_execution() -> SupervisedExecutionScope | None:
    return _SCOPE.get()


class SupervisedCommandHeartbeat:
    """Advance the attachment heartbeat while one wrapped command runs."""

    def __init__(self, scope: SupervisedExecutionScope) -> None:
        self._paths = execution_paths(scope.execution_id, project_dir=scope.project_data)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        write_attachment_heartbeat(self._paths, generation=1)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        generation = 1
        while not self._stop.wait(0.25):
            generation += 1
            write_attachment_heartbeat(self._paths, generation=generation)


def wrap_supervised_command(
    command: list[str], scope: SupervisedExecutionScope
) -> tuple[list[str], dict[str, str], SupervisedCommandHeartbeat]:
    """Wrap a local command in the exact-ID process-tree supervisor."""
    wrapped = [
        sys.executable,
        "-m",
        "booley.runtime.execution_supervisor",
        "run",
        "--execution-id",
        str(scope.execution_id),
        "--attachment-timeout-seconds",
        "2.0",
        "--",
        *command,
    ]
    heartbeat = SupervisedCommandHeartbeat(scope)
    return wrapped, {RUNTIME_EXECUTION_ENV: str(scope.execution_id)}, heartbeat


__all__ = [
    "SupervisedExecutionScope",
    "current_supervised_execution",
    "supervised_execution_scope",
    "wrap_supervised_command",
]
