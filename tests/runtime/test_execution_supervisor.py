"""Sandbox Attachment execution owns cancellation through complete tree exit."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

if sys.platform != "linux":
    pytest.skip(
        "Sandbox Attachment execution supervision requires Linux",
        allow_module_level=True,
    )

import pty

from tests.execution_test_support import (
    runtime_env,
    start_supervisor_with_detached_descendant,
    wait_for,
)

from booley.runtime.execution_records import (
    ExecutionId,
    atomic_write_json,
    execution_paths,
    force_cancellation_requested,
    read_json,
    request_cancellation,
    write_attachment_heartbeat,
)

_EXECUTION_ID = ExecutionId("a" * 32)


def _heartbeat_generations(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [int(line) for line in path.read_text(encoding="ascii").splitlines()]


def _start_supervisor_with_test_heartbeat(
    project_dir: Path,
    execution_id: ExecutionId,
    heartbeat_path: Path,
    command: list[str],
    *,
    grace_seconds: float = 0.25,
) -> subprocess.Popen[str]:
    bootstrap = (
        "from pathlib import Path\n"
        "import booley.runtime.execution_supervisor as supervisor\n"
        f"heartbeat_path = Path({str(heartbeat_path)!r})\n"
        "generation = 0\n"
        "def touch():\n"
        "    global generation\n"
        "    generation += 1\n"
        "    with heartbeat_path.open('a', encoding='ascii') as stream:\n"
        "        stream.write(f'{generation}\\n')\n"
        "supervisor.touch_reaper_heartbeat = touch\n"
        "supervisor._EXECUTION_HEARTBEAT_SECONDS = 0.03\n"
        "raise SystemExit(supervisor.main())\n"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            bootstrap,
            "run",
            "--execution-id",
            execution_id,
            "--grace-seconds",
            str(grace_seconds),
            "--attachment-timeout-seconds",
            "5",
            "--",
            *command,
        ],
        env=runtime_env(project_dir),
        text=True,
    )


def test_force_cancellation_is_monotonic(tmp_path: Path) -> None:
    paths = execution_paths(_EXECUTION_ID, project_dir=tmp_path)
    request_cancellation(paths, force=True)
    request_cancellation(paths, force=False)

    assert force_cancellation_requested(paths) is True
    assert read_json(paths.cancel)["force"] is True


def test_cancellation_before_start_never_launches_the_command(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    execution_id = ExecutionId("d" * 32)
    paths = execution_paths(execution_id, project_dir=project_dir)
    marker = tmp_path / "command-started"
    write_attachment_heartbeat(paths, generation=1)
    request_cancellation(paths, signum=signal.SIGINT)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--",
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').close()",
        ],
        check=False,
        env=runtime_env(project_dir),
    )

    assert result.returncode == 130
    assert not marker.exists()
    payload = read_json(paths.record)
    assert payload["state"] == "terminal"
    assert payload["tree_terminal"] is True
    assert payload["leader"] is None


def test_boolean_protocol_signal_does_not_become_signal_one(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    execution_id = ExecutionId("e" * 32)
    paths = execution_paths(execution_id, project_dir=project_dir)
    write_attachment_heartbeat(paths, generation=1)
    atomic_write_json(
        paths.cancel,
        {"force": False, "reason": "cancelled", "signum": True},
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(99)",
        ],
        check=False,
        env=runtime_env(project_dir),
    )

    assert result.returncode == 130


def _non_zombie_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_cancellation_reaps_descendant_that_created_a_new_session(tmp_path: Path) -> None:
    """Cancellation remains scoped and complete across a descendant's setsid()."""
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    paths = execution_paths(_EXECUTION_ID, project_dir=project_dir)
    write_attachment_heartbeat(paths, generation=1)
    descendant_pid_file = tmp_path / "descendant.pid"
    supervisor = start_supervisor_with_detached_descendant(
        project_dir, _EXECUTION_ID, descendant_pid_file
    )
    descendant_pid: int | None = None
    try:
        wait_for(
            lambda: paths.record.exists() and descendant_pid_file.exists(),
            failure="supervisor and descendant did not become ready",
        )
        descendant_pid = int(descendant_pid_file.read_text(encoding="utf-8"))

        cancel = subprocess.run(
            [
                sys.executable,
                "-m",
                "booley.runtime.execution_supervisor",
                "cancel",
                "--execution-id",
                _EXECUTION_ID,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=runtime_env(project_dir),
        )
        assert cancel.returncode == 0, cancel.stderr

        assert supervisor.wait(timeout=5) == 130
        wait_for(
            lambda: descendant_pid is not None and not _non_zombie_alive(descendant_pid),
            failure="detached descendant did not stop",
        )
        payload = json.loads(paths.record.read_text(encoding="utf-8"))
        assert payload["state"] == "terminal"
        assert payload["exit_code"] == 130
        assert payload["tree_terminal"] is True
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.communicate(timeout=5)
        if descendant_pid is not None and _non_zombie_alive(descendant_pid):
            os.killpg(descendant_pid, signal.SIGKILL)


def test_supervisor_refreshes_reaper_heartbeat_until_normal_exit(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    execution_id = ExecutionId("f" * 32)
    paths = execution_paths(execution_id, project_dir=project_dir)
    heartbeat_path = tmp_path / "reaper-heartbeat"
    write_attachment_heartbeat(paths, generation=1)
    supervisor = _start_supervisor_with_test_heartbeat(
        project_dir,
        execution_id,
        heartbeat_path,
        [sys.executable, "-c", "import time; time.sleep(0.3); raise SystemExit(7)"],
    )
    try:
        wait_for(
            lambda: len(_heartbeat_generations(heartbeat_path)) >= 2,
            failure="supervisor did not refresh its reaper heartbeat",
        )
        assert supervisor.wait(timeout=5) == 7
        payload = read_json(paths.record)
        assert payload["state"] == "terminal"
        assert payload["exit_code"] == 7
        assert payload["tree_terminal"] is True
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.wait(timeout=5)


def test_supervisor_refreshes_reaper_heartbeat_during_cancellation(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    execution_id = ExecutionId("1" * 32)
    paths = execution_paths(execution_id, project_dir=project_dir)
    heartbeat_path = tmp_path / "reaper-heartbeat"
    write_attachment_heartbeat(paths, generation=1)
    command = [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(10)",
    ]
    supervisor = _start_supervisor_with_test_heartbeat(
        project_dir,
        execution_id,
        heartbeat_path,
        command,
    )
    try:
        wait_for(
            lambda: len(_heartbeat_generations(heartbeat_path)) >= 2,
            failure="supervisor did not start its reaper heartbeat",
        )
        before_cancel = len(_heartbeat_generations(heartbeat_path))
        request_cancellation(paths, signum=signal.SIGINT)
        assert supervisor.wait(timeout=5) == 130
        after_cancel = len(_heartbeat_generations(heartbeat_path))
        assert after_cancel > before_cancel
        payload = read_json(paths.record)
        assert payload["state"] == "terminal"
        assert payload["tree_terminal"] is True
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.wait(timeout=5)


def test_attachment_heartbeat_expiry_cancels_execution(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    execution_id = ExecutionId("b" * 32)
    paths = execution_paths(execution_id, project_dir=project_dir)
    write_attachment_heartbeat(paths, generation=1)
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--grace-seconds",
            "0.1",
            "--attachment-timeout-seconds",
            "0.1",
            "--",
            "sleep",
            "120",
        ],
        env=runtime_env(project_dir),
    )
    try:
        assert supervisor.wait(timeout=5) == 130
        payload = json.loads(paths.record.read_text(encoding="utf-8"))
        assert payload["state"] == "terminal"
        assert payload["terminal_cause"] == "attachment_expired"
        assert payload["tree_terminal"] is True
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.wait(timeout=5)


def test_tty_child_owns_the_foreground_process_group(tmp_path: Path) -> None:
    if sys.platform != "linux":
        return
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    execution_id = ExecutionId("c" * 32)
    paths = execution_paths(execution_id, project_dir=project_dir)
    write_attachment_heartbeat(paths, generation=1)
    child_check = (
        "import os,sys; sys.exit(0 if os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp() else 9)"
    )
    argv = [
        sys.executable,
        "-m",
        "booley.runtime.execution_supervisor",
        "run",
        "--execution-id",
        execution_id,
        "--tty",
        "--",
        sys.executable,
        "-c",
        child_check,
    ]
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, argv, runtime_env(project_dir))
    try:
        _waited, status = os.waitpid(pid, 0)
    finally:
        os.close(fd)

    assert os.waitstatus_to_exitcode(status) == 0
    payload = json.loads(paths.record.read_text(encoding="utf-8"))
    assert payload["tree_terminal"] is True
