"""Tests for host-side recovery of abandoned runtime executions."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

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
    _scan_execution,
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

_SRC_ROOT = Path(__file__).parents[2] / "src"
_PROCESS_TIMEOUT_SECONDS = 5.0


def _runtime_env(project_dir: Path, execution_id: str) -> dict[str, str]:
    python_path = os.environ.get("PYTHONPATH", "")
    source_path = str(_SRC_ROOT)
    if python_path:
        source_path = f"{source_path}{os.pathsep}{python_path}"
    return {
        **os.environ,
        "BOOLEY_PROJECT_DIR": str(project_dir),
        "PYTHONPATH": source_path,
        RUNTIME_EXECUTION_ENV: execution_id,
    }


def _start_supervisor(
    project_dir: Path, execution_id: str, descendant_pid_file: Path
) -> subprocess.Popen[str]:
    descendant_script = (
        "import os,signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        f"Path({str(descendant_pid_file)!r}).write_text(str(os.getpid()),encoding='utf-8')\n"
        "time.sleep(120)\n"
    )
    leader_script = (
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{descendant_script!r}],start_new_session=True)\n"
        "time.sleep(120)\n"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--attachment-timeout-seconds",
            "60",
            "--",
            sys.executable,
            "-c",
            leader_script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=_runtime_env(project_dir, execution_id),
    )


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


def _wait_for_ready_execution(
    paths: ExecutionPaths, descendant_pid_file: Path
) -> tuple[ProcessIdentity, ProcessIdentity]:
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        members = _ready_execution_members(paths, descendant_pid_file)
        if members is not None:
            return members
        time.sleep(0.02)
    raise AssertionError("execution did not publish live leader and descendant identities")


def _wait_for_stopped(identities: set[ProcessIdentity]) -> None:
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if all(
            observe_process(identity).state not in {RUNNING, UNKNOWN} for identity in identities
        ):
            return
        time.sleep(0.02)
    raise AssertionError("execution members did not stop before cleanup timeout")


def _cleanup_identities(
    execution_id: str,
    paths: ExecutionPaths,
    known: tuple[ProcessIdentity, ...],
) -> None:
    identities = set(known)
    record = read_json(paths.record)
    if record is not None:
        for field in ("supervisor", "leader"):
            identity = ProcessIdentity.from_payload(record.get(field))
            if identity is not None:
                identities.add(identity)
    identities.update(_scan_execution(ExecutionId(execution_id)).identities)
    for identity in identities:
        if observe_process(identity).state is RUNNING:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(identity.pid, signal.SIGKILL)
    _wait_for_stopped(identities)


def _cleanup_execution(
    execution_id: str,
    paths: ExecutionPaths,
    known: tuple[ProcessIdentity, ...],
) -> None:
    reset_cache()
    try:
        recover_execution(execution_id)
    finally:
        try:
            _cleanup_identities(execution_id, paths, known)
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
    execution_id = uuid.uuid4().hex
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    paths = execution_paths(execution_id, project_dir=project_dir)
    descendant_pid_file = tmp_path / "descendant.pid"
    monkeypatch.delenv(RUNTIME_EXECUTION_ENV, raising=False)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
    reset_cache()
    write_attachment_heartbeat(paths, generation=1)
    supervisor = _start_supervisor(project_dir, execution_id, descendant_pid_file)
    known: tuple[ProcessIdentity, ...] = ()
    try:
        leader, descendant = _wait_for_ready_execution(paths, descendant_pid_file)
        known = (leader, descendant)
        supervisor.kill()
        assert supervisor.wait(timeout=_PROCESS_TIMEOUT_SECONDS) == -signal.SIGKILL

        reset_cache()
        assert recover_execution(execution_id) is True

        processes = _scan_execution(ExecutionId(execution_id))
        assert processes.complete is True
        assert processes.identities == ()
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
            _cleanup_execution(execution_id, paths, known)


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
