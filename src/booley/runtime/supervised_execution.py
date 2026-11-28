"""Thread-local binding for one preallocated supervised child execution."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    ExecutionId,
    ExecutionPaths,
    execution_paths,
    update_execution_record,
)
from booley.runtime.pid import ProcessIdentity, capture_process_identity
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
            self._record_running(self._execution_id, process)

    def _record_running(self, execution_id: ExecutionId, process: subprocess.Popen) -> None:
        identity = capture_process_identity(process.pid)
        already_terminal = False

        def change(current: dict[str, Any] | None) -> dict[str, Any] | None:
            nonlocal already_terminal
            if current is not None and current.get("state") == "terminal":
                already_terminal = True
                return None
            return self._record("running", identity)

        update_execution_record(self._paths(execution_id), change)
        if already_terminal:
            # The execution was proven empty and recorded terminal: nothing may
            # run under it, and the terminal record is never replaced.
            kill_process_tree(process)

    def unregister(self, process: subprocess.Popen) -> None:
        with self._gate:
            self._processes.discard(process)
            active = bool(self._processes)
        if self._execution_id is not None and not active:

            def change(current: dict[str, Any] | None) -> dict[str, Any] | None:
                if current is not None and current.get("state") == "running":
                    return self._record("waiting", None)
                return None

            update_execution_record(self._paths(self._execution_id), change)

    def pids(self) -> tuple[int, ...]:
        """The PIDs of the registered processes not yet reaped.

        A reaped process stays registered until its owner unregisters it, and
        the kernel may reuse its PID before then: never hand out that PID.
        """
        with self._gate:
            return tuple(process.pid for process in self._processes if process.returncode is None)

    def terminate_all(self) -> None:
        with self._gate:
            processes = tuple(self._processes)
        for process in processes:
            kill_process_tree(process)

    def _paths(self, execution_id: ExecutionId) -> ExecutionPaths:
        return execution_paths(execution_id, booley_dir=self._project_data)

    @staticmethod
    def _record(state: str, leader: ProcessIdentity | None) -> dict[str, Any]:
        return {
            "schema_version": PROTOCOL_VERSION,
            "state": state,
            "runtime_identity": None,
            "supervisor": None,
            "leader": leader.to_payload() if leader is not None else None,
            "exit_code": None,
            "tree_terminal": False,
            "terminal_cause": None,
            "updated_at": utc_now_rfc3339(),
        }


def no_cancellation_cause() -> str | None:
    return None


@dataclass(frozen=True, slots=True)
class SupervisedExecutionScope:
    execution_id: ExecutionId | None
    project_data: Path
    cancelled: Callable[[], bool]
    cancellation_cause: Callable[[], str | None] = no_cancellation_cause
    processes: SupervisedProcessSet = field(init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "processes",
            SupervisedProcessSet(self.execution_id, self.project_data, self.cancelled),
        )

    def describe_cancellation(self) -> str:
        """Name the lane and the recorded cause of this scope's cancellation."""
        lane = (
            "outer lane" if self.execution_id is None else f"child execution {self.execution_id}"
        )
        return f"{lane}: {self.cancellation_cause() or 'cancellation requested'}"


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
        name="booley-record-process-cancellation",
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


__all__ = [
    "SupervisedExecutionScope",
    "current_supervised_execution",
    "supervised_execution_scope",
]
