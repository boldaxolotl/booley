"""Shared process harness for Runtime execution integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from booley.core import atomic_files, file_lock
from booley.runtime import execution_records, execution_recovery
from booley.runtime.execution_records import (
    RUNTIME_EXECUTION_ENV,
    ExecutionId,
    execution_paths,
    read_json,
)
from tests.time_budget import hang_guard_s, scaled

_SRC_ROOT = Path(__file__).parents[1] / "src"
_WAIT_SECONDS = hang_guard_s()
ResultT = TypeVar("ResultT")


def runtime_env(booley_dir: Path, execution_id: ExecutionId | None = None) -> dict[str, str]:
    """Return a source-checkout subprocess environment for one Project."""
    python_path = os.environ.get("PYTHONPATH", "")
    source_path = str(_SRC_ROOT)
    if python_path:
        source_path = f"{source_path}{os.pathsep}{python_path}"
    env = {**os.environ, "BOOLEY_DIR": str(booley_dir), "PYTHONPATH": source_path}
    if execution_id is not None:
        env[RUNTIME_EXECUTION_ENV] = execution_id
    return env


def wait_for_value(
    probe: Callable[[], ResultT | None], *, failure: str, timeout_s: float = _WAIT_SECONDS
) -> ResultT:
    """Return the first non-None probe result within a bounded interval."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = probe()
        if result is not None:
            return result
        time.sleep(0.02)
    raise AssertionError(failure)


def wait_for(probe: Callable[[], bool], *, failure: str, timeout_s: float = _WAIT_SECONDS) -> None:
    """Wait for a boolean probe within a bounded interval."""
    wait_for_value(lambda: True if probe() else None, failure=failure, timeout_s=timeout_s)


def start_supervisor_with_detached_descendant(
    booley_dir: Path, execution_id: ExecutionId, descendant_pid_file: Path
) -> subprocess.Popen[str]:
    """Start a marked supervisor whose SIGINT-resistant descendant calls setsid."""
    descendant_script = (
        "import os,signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        f"pid_file = Path({str(descendant_pid_file)!r})\n"
        "pending = pid_file.with_suffix('.tmp')\n"
        "pending.write_text(str(os.getpid()),encoding='utf-8')\n"
        "pending.replace(pid_file)\n"
        "time.sleep(120)\n"
    )
    leader_script = (
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{descendant_script!r}],start_new_session=True)\n"
        "time.sleep(120)\n"
    )
    # Kept on Popen: callers kill and wait on the supervisor themselves.
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--grace-seconds",
            str(scaled(0.1)),
            "--attachment-timeout-seconds",
            str(2 * hang_guard_s()),
            "--",
            sys.executable,
            "-c",
            leader_script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=runtime_env(booley_dir, execution_id),
    )


class _RecoveryInterleaving:
    """Hooks that start recovery between an execution-record read and write."""

    def __init__(self, execution_id: ExecutionId, booley_dir: Path) -> None:
        self.paths = execution_paths(execution_id, booley_dir=booley_dir)
        self.main_thread = threading.get_ident()
        self.settled = threading.Event()
        self.reads = 0
        self.replaced: list[object] = []
        self.failures: list[Exception] = []
        self.thread = threading.Thread(
            target=self._recover, args=(execution_id, booley_dir), daemon=True
        )
        self.real_read = execution_records.read_file_bytes
        self.real_replace = atomic_files.replace_file
        self.real_acquire = file_lock.acquire_file_lock

    def _recover(self, execution_id: ExecutionId, booley_dir: Path) -> None:
        try:
            # private: the race window sits inside this step; public recover_execution cannot hold it open.
            execution_recovery._publish_recovered_terminal(execution_id, booley_dir=booley_dir)
        except Exception as exc:  # noqa: BLE001 - reported to the test thread
            self.failures.append(exc)
        finally:
            self.settled.set()

    def read(self, path: Path, *args: Any, **kwargs: Any) -> bytes:
        raw = self.real_read(path, *args, **kwargs)
        if path == self.paths.record and threading.get_ident() == self.main_thread:
            self.reads += 1
            if self.reads == 1:
                self.thread.start()
                assert self.settled.wait(hang_guard_s()), (
                    "recovery neither finished nor met the record lock"
                )
        return raw

    def replace(self, source: Path, destination: Path) -> None:
        if destination == self.paths.record:
            current = read_json(self.paths.record)
            self.replaced.append(current.get("state") if current is not None else None)
        self.real_replace(source, destination)

    def acquire(self, handle: Any, **kwargs: Any) -> None:
        try:
            self.real_acquire(handle, **kwargs)
        except file_lock.LockContentionError:
            if threading.get_ident() != self.main_thread:
                self.settled.set()
            raise


def run_with_recovery_between_read_and_write(
    monkeypatch: pytest.MonkeyPatch,
    execution_id: ExecutionId,
    booley_dir: Path,
    interrupted: Callable[[], object],
) -> list[object]:
    """Run *interrupted* while recovery publishes its terminal record mid-update.

    Recovery starts on another thread right after *interrupted* first reads the
    execution record, and the call waits until that thread finished or met the
    record lock. Returns the state of every record that a write replaced, so a
    caller can assert that no ``terminal`` record was ever replaced.
    """
    hooks = _RecoveryInterleaving(execution_id, booley_dir)
    monkeypatch.setattr(execution_records, "read_file_bytes", hooks.read)
    monkeypatch.setattr(atomic_files, "replace_file", hooks.replace)
    monkeypatch.setattr(file_lock, "acquire_file_lock", hooks.acquire)
    interrupted()
    hooks.thread.join(hang_guard_s())
    assert not hooks.thread.is_alive()
    assert hooks.reads >= 1
    assert hooks.failures == []
    return hooks.replaced
