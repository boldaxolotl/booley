"""Runtime Attachment execution owns cancellation through complete tree exit."""

from __future__ import annotations

import json
import os
import pty
import signal
import subprocess
import sys
import time
from pathlib import Path

from booley.runtime.execution_records import (
    execution_paths,
    force_cancellation_requested,
    read_json,
    request_cancellation,
    write_attachment_heartbeat,
)

_EXECUTION_ID = "a" * 32


def test_force_cancellation_is_monotonic(tmp_path: Path) -> None:
    paths = execution_paths(_EXECUTION_ID, project_dir=tmp_path)
    request_cancellation(paths, force=True)
    request_cancellation(paths, force=False)

    assert force_cancellation_requested(paths) is True
    assert read_json(paths.cancel)["force"] is True


def test_cancellation_before_start_never_launches_the_command(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    execution_id = "d" * 32
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
        env={**os.environ, "BOOLEY_PROJECT_DIR": str(project_dir)},
    )

    assert result.returncode == 130
    assert not marker.exists()
    payload = read_json(paths.record)
    assert payload["state"] == "terminal"
    assert payload["tree_terminal"] is True
    assert payload["leader"] is None


def _wait_for(predicate, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


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
    descendant_script = (
        "import signal,time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(120)\n"
    )
    command_script = (
        "import subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.executable,'-c',{descendant_script!r}],start_new_session=True)\n"
        f"open({str(descendant_pid_file)!r},'w').write(str(p.pid))\n"
        "time.sleep(120)\n"
    )
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            _EXECUTION_ID,
            "--grace-seconds",
            "0.1",
            "--attachment-timeout-seconds",
            "5",
            "--",
            sys.executable,
            "-c",
            command_script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "BOOLEY_PROJECT_DIR": str(project_dir)},
    )
    descendant_pid: int | None = None
    try:
        _wait_for(lambda: paths.record.exists() and descendant_pid_file.exists())
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
            env={**os.environ, "BOOLEY_PROJECT_DIR": str(project_dir)},
        )
        assert cancel.returncode == 0, cancel.stderr

        assert supervisor.wait(timeout=5) == 130
        _wait_for(lambda: descendant_pid is not None and not _non_zombie_alive(descendant_pid))
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


def test_attachment_heartbeat_expiry_cancels_execution(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    paths = execution_paths("b" * 32, project_dir=project_dir)
    write_attachment_heartbeat(paths, generation=1)
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            "b" * 32,
            "--grace-seconds",
            "0.1",
            "--attachment-timeout-seconds",
            "0.1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
        ],
        env={**os.environ, "BOOLEY_PROJECT_DIR": str(project_dir)},
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
    execution_id = "c" * 32
    paths = execution_paths(execution_id, project_dir=project_dir)
    write_attachment_heartbeat(paths, generation=1)
    child_check = (
        "import os,sys; "
        "sys.exit(0 if os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp() else 9)"
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
        os.execve(sys.executable, argv, {**os.environ, "BOOLEY_PROJECT_DIR": str(project_dir)})
    try:
        _waited, status = os.waitpid(pid, 0)
    finally:
        os.close(fd)

    assert os.waitstatus_to_exitcode(status) == 0
    payload = json.loads(paths.record.read_text(encoding="utf-8"))
    assert payload["tree_terminal"] is True
