"""Thread-local binding for one preallocated supervised child execution."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.execution_records import (
    RUNTIME_EXECUTION_ENV,
    ExecutionId,
    execution_paths,
    write_attachment_heartbeat,
)


@dataclass(frozen=True, slots=True)
class SupervisedExecutionScope:
    execution_id: ExecutionId
    project_data: Path
    cancelled: Callable[[], bool]


_SCOPE: ContextVar[SupervisedExecutionScope | None] = ContextVar(
    "booley_supervised_execution_scope", default=None
)


@contextmanager
def supervised_execution_scope(
    scope: SupervisedExecutionScope,
) -> Iterator[None]:
    token: Token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


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
