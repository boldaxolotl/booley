"""Tests for host-side recovery of abandoned runtime executions."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.execution_test_support import (
    start_supervisor_with_detached_descendant,
    wait_for,
    wait_for_value,
)

from booley.runtime.execution_records import (
    RUNTIME_EXECUTION_ENV,
    ExecutionId,
    ExecutionPaths,
    execution_paths,
    read_json,
    write_attachment_heartbeat,
)
from booley.runtime.execution_recovery import (
    _matches_execution,
    recover_execution,
)
from booley.runtime.pid import (
    RUNNING,
    UNKNOWN,
    ProcessIdentity,
    capture_process_identity,
    observe_process,
)
from booley.runtime.project_dir import reset_cache

_PROCESS_TIMEOUT_SECONDS = 5.0
_RECOVERY_TIMEOUT_SECONDS = 12.0


def _marked_execution_identities(execution_id: ExecutionId) -> set[ProcessIdentity]:
    marker = f"{RUNTIME_EXECUTION_ENV}={execution_id}".encode()
    identities: set[ProcessIdentity] = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        identity = capture_process_identity(int(proc.name))
        if identity is None:
            continue
        try:
            matches = marker in (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if matches and observe_process(identity).state is RUNNING:
            identities.add(identity)
    return identities


def _ready_execution_members(
    paths: ExecutionPaths, descendant_pid_file: Path
) -> tuple[ProcessIdentity, ProcessIdentity] | None:
    record = read_json(paths.record)
    if record is None or record.get("state") != "running":
        return None
    leader = ProcessIdentity.from_payload(record.get("leader"))
    if leader is None or observe_process(leader).state is not RUNNING:
        return None
    try:
        descendant_pid = int(descendant_pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    descendant = capture_process_identity(descendant_pid)
    if descendant is None or observe_process(descendant).state is not RUNNING:
        return None
    return leader, descendant


def _wait_for_stopped(identities: set[ProcessIdentity]) -> None:
    wait_for(
        lambda: all(
            observe_process(identity).state not in {RUNNING, UNKNOWN} for identity in identities
        ),
        failure="execution members did not stop before cleanup timeout",
        timeout_s=_PROCESS_TIMEOUT_SECONDS,
    )


def _recover_while_observing_terminal_order(
    execution_id: ExecutionId,
    paths: ExecutionPaths,
    descendant: ProcessIdentity,
) -> tuple[bool, bool]:
    executor = ThreadPoolExecutor(max_workers=1)
    recovery = executor.submit(recover_execution, execution_id)
    deadline = time.monotonic() + _RECOVERY_TIMEOUT_SECONDS
    terminal_while_running = False
    try:
        while not recovery.done() and time.monotonic() < deadline:
            record = read_json(paths.record)
            terminal_while_running = bool(
                record is not None
                and record.get("state") == "terminal"
                and observe_process(descendant).state in {RUNNING, UNKNOWN}
            )
            if terminal_while_running:
                break
            time.sleep(0.01)
        remaining = max(0.01, deadline - time.monotonic())
        return recovery.result(timeout=remaining), terminal_while_running
    finally:
        executor.shutdown(wait=recovery.done(), cancel_futures=True)


def _cleanup_identities(
    execution_id: ExecutionId,
    paths: ExecutionPaths,
    known_identities: tuple[ProcessIdentity, ...],
) -> None:
    identities = set(known_identities)
    record = read_json(paths.record)
    if record is not None:
        for field in ("supervisor", "leader"):
            identity = ProcessIdentity.from_payload(record.get(field))
            if identity is not None:
                identities.add(identity)
    identities.update(_marked_execution_identities(execution_id))
    for identity in identities:
        if observe_process(identity).state is RUNNING:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(identity.pid, signal.SIGKILL)
    _wait_for_stopped(identities)


def _cleanup_execution(
    execution_id: ExecutionId,
    paths: ExecutionPaths,
    known_identities: tuple[ProcessIdentity, ...],
) -> None:
    reset_cache()
    try:
        recover_execution(execution_id)
    finally:
        try:
            _cleanup_identities(execution_id, paths, known_identities)
        finally:
            reset_cache()


def _kill_and_reap(supervisor: subprocess.Popen[str]) -> None:
    if supervisor.poll() is None:
        supervisor.kill()
    supervisor.wait(timeout=_PROCESS_TIMEOUT_SECONDS)


@pytest.mark.skipif(sys.platform != "linux", reason="execution recovery scans Linux /proc")
def test_recovers_execution_after_supervisor_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_id = ExecutionId(uuid.uuid4().hex)
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    paths = execution_paths(execution_id, project_dir=project_dir)
    descendant_pid_file = tmp_path / "descendant.pid"
    monkeypatch.delenv(RUNTIME_EXECUTION_ENV, raising=False)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
    reset_cache()
    write_attachment_heartbeat(paths, generation=1)
    supervisor = start_supervisor_with_detached_descendant(
        project_dir, execution_id, descendant_pid_file
    )
    known_identities: tuple[ProcessIdentity, ...] = ()
    try:
        leader, descendant = wait_for_value(
            lambda: _ready_execution_members(paths, descendant_pid_file),
            failure="execution did not publish live leader and descendant identities",
            timeout_s=_PROCESS_TIMEOUT_SECONDS,
        )
        known_identities = (leader, descendant)
        supervisor.kill()
        assert supervisor.wait(timeout=_PROCESS_TIMEOUT_SECONDS) == -signal.SIGKILL

        reset_cache()
        recovered, terminal_while_running = _recover_while_observing_terminal_order(
            execution_id, paths, descendant
        )
        assert terminal_while_running is False
        assert recovered is True

        assert _marked_execution_identities(execution_id) == set()
        assert observe_process(leader).state not in {RUNNING, UNKNOWN}
        assert observe_process(descendant).state not in {RUNNING, UNKNOWN}
        terminal = read_json(paths.record)
        assert terminal is not None
        assert terminal["state"] == "terminal"
        assert terminal["tree_terminal"] is True
        assert terminal["terminal_cause"] == "orphan_recovered"
    finally:
        try:
            _kill_and_reap(supervisor)
        finally:
            _cleanup_execution(execution_id, paths, known_identities)


def test_import_does_not_require_sigkill() -> None:
    """Non-POSIX hosts may not expose SIGKILL through Python's signal module."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import signal, sys; "
                "sys.path.insert(0, 'src'); "
                "hasattr(signal, 'SIGKILL') and delattr(signal, 'SIGKILL'); "
                "import booley.runtime.execution_recovery"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_unreadable_kernel_thread_cannot_match_execution(tmp_path: Path) -> None:
    proc = tmp_path / "2"
    proc.mkdir()
    (proc / "environ").mkdir()
    (proc / "stat").write_text(
        "2 (kworker/0:0) S 0 0 0 0 0 2097152\n",
        encoding="utf-8",
    )

    assert _matches_execution(proc, b"BOOLEY_RUNTIME_EXECUTION_ID=") is False


def test_unreadable_proc_entry_does_not_block_marker_scan(tmp_path: Path) -> None:
    proc = tmp_path / "321"
    proc.mkdir()
    (proc / "environ").mkdir()
    (proc / "stat").write_text(
        "321 (host-agent) S 1 1 1 0 0 0\n",
        encoding="utf-8",
    )

    assert _matches_execution(proc, b"BOOLEY_RUNTIME_EXECUTION_ID=") is False


def test_unreadable_proc_entry_with_malformed_stat_does_not_match(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "654"
    proc.mkdir()
    (proc / "environ").mkdir()
    (proc / "stat").write_text("malformed\n", encoding="utf-8")

    assert _matches_execution(proc, b"BOOLEY_RUNTIME_EXECUTION_ID=") is False
