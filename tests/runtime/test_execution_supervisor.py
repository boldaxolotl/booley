"""Sandbox Attachment execution owns cancellation through complete tree exit."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
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
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import hang_guard_s, scaled

from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    ExecutionId,
    ExecutionPaths,
    atomic_write_json,
    execution_paths,
    force_cancellation_requested,
    read_json,
    request_cancellation,
    write_attachment_heartbeat,
)
from booley.runtime.process_group import new_group_kwargs
from booley.runtime.supervised_execution import SupervisedProcessSet


def _unique_execution_id() -> ExecutionId:
    """A per-test execution ID.

    Supervised commands carry their ID in the environment, and runtime recovery
    signals every host process carrying it, so a shared literal would let one
    xdist worker's recovery kill another worker's command.
    """
    return ExecutionId(uuid.uuid4().hex)


def _heartbeat_generations(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [int(line) for line in path.read_text(encoding="ascii").splitlines()]


def _start_supervisor_with_test_heartbeat(
    booley_dir: Path,
    execution_id: ExecutionId,
    heartbeat_path: Path,
    command: list[str],
    *,
    grace_seconds: float = scaled(0.25),
) -> subprocess.Popen[str]:
    bootstrap = (
        "from pathlib import Path\n"
        "import booley.runtime.execution_supervisor as supervisor\n"
        "import booley.runtime.heartbeat as reaper\n"
        f"heartbeat_path = Path({str(heartbeat_path)!r})\n"
        "generation = 0\n"
        "def touch():\n"
        "    global generation\n"
        "    generation += 1\n"
        "    with heartbeat_path.open('a', encoding='ascii') as stream:\n"
        "        stream.write(f'{generation}\\n')\n"
        # boundary: the Sandbox activity heartbeat file the idle reaper reads
        "reaper.touch_reaper_heartbeat = touch\n"
        "supervisor._EXECUTION_HEARTBEAT_SECONDS = 0.03\n"
        "raise SystemExit(supervisor.main())\n"
    )
    # Kept on Popen: the test waits on the supervisor itself.
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
            str(2 * hang_guard_s()),
            "--",
            *command,
        ],
        env=runtime_env(booley_dir),
        text=True,
    )


def _run_command(tmp_path: Path, command: list[str], *, path: str | None = None):
    data = tmp_path / ".booley"
    data.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=data)
    write_attachment_heartbeat(paths, generation=1)
    env = runtime_env(data)
    if path is not None:
        env["PATH"] = path
    result = run_bounded_child(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--",
            *command,
        ],
        env=env,
        timeout=2 * hang_guard_s(),
    )
    return result, read_json(paths.record)


@pytest.mark.parametrize(
    ("kind", "exit_code", "diagnostic"),
    [
        pytest.param("name", 127, "command not found", id="missing-name"),
        pytest.param("path", 127, "command not found", id="missing-path"),
        pytest.param("permission", 126, "permission denied", id="permission-denied"),
    ],
)
def test_spawn_failure_is_terminal(tmp_path: Path, kind, exit_code, diagnostic) -> None:
    executable = "booley-test-missing-command-1572"
    if kind != "name":
        executable = str(tmp_path / "command")
    if kind == "permission":
        Path(executable).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        Path(executable).chmod(0o644)

    result, payload = _run_command(tmp_path, [executable, "private-argument"], path=str(tmp_path))

    assert result.returncode == exit_code
    assert result.stderr == f"{diagnostic}: {executable}\n"
    assert result.stdout == ""
    assert payload["schema_version"] == PROTOCOL_VERSION
    assert payload["state"] == "terminal"
    assert payload["exit_code"] == exit_code
    assert payload["tree_terminal"] is True
    assert payload["leader"] is None
    assert payload["supervisor"]
    assert payload["terminal_cause"] == "spawn_error"


def test_signal_exit_status_is_preserved(tmp_path: Path) -> None:
    script = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"

    result, payload = _run_command(tmp_path, [sys.executable, "-c", script])

    assert result.returncode == 143
    assert result.stderr == ""
    assert result.stdout == ""
    assert payload["exit_code"] == 143
    assert payload["state"] == "terminal"
    assert payload["tree_terminal"] is True


def test_force_cancellation_is_monotonic(tmp_path: Path) -> None:
    paths = execution_paths(_unique_execution_id(), booley_dir=tmp_path)
    request_cancellation(paths, force=True)
    request_cancellation(paths, force=False)

    assert force_cancellation_requested(paths) is True
    assert read_json(paths.cancel)["force"] is True


def test_cancellation_before_start_never_launches_the_command(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
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
        env=runtime_env(booley_dir),
    )

    assert result.returncode == 130
    assert not marker.exists()
    payload = read_json(paths.record)
    assert payload["state"] == "terminal"
    assert payload["tree_terminal"] is True
    assert payload["leader"] is None


def test_boolean_protocol_signal_does_not_become_signal_one(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
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
        env=runtime_env(booley_dir),
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
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
    write_attachment_heartbeat(paths, generation=1)
    descendant_pid_file = tmp_path / "descendant.pid"
    # Kept on Popen: the test cancels and waits on the supervisor itself.
    supervisor = start_supervisor_with_detached_descendant(
        booley_dir, execution_id, descendant_pid_file
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
                execution_id,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=runtime_env(booley_dir),
        )
        assert cancel.returncode == 0, cancel.stderr

        assert supervisor.wait(timeout=hang_guard_s()) == 130
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
        # Kept on Popen: reaps the supervisor the test drives itself.
        supervisor.communicate(timeout=hang_guard_s())
        if descendant_pid is not None and _non_zombie_alive(descendant_pid):
            os.killpg(descendant_pid, signal.SIGKILL)


def test_supervisor_refreshes_reaper_heartbeat_until_normal_exit(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
    heartbeat_path = tmp_path / "reaper-heartbeat"
    write_attachment_heartbeat(paths, generation=1)
    supervisor = _start_supervisor_with_test_heartbeat(
        booley_dir,
        execution_id,
        heartbeat_path,
        [sys.executable, "-c", "import time; time.sleep(0.3); raise SystemExit(7)"],
    )
    try:
        wait_for(
            lambda: len(_heartbeat_generations(heartbeat_path)) >= 2,
            failure="supervisor did not refresh its reaper heartbeat",
        )
        assert supervisor.wait(timeout=hang_guard_s()) == 7
        payload = read_json(paths.record)
        assert payload["state"] == "terminal"
        assert payload["exit_code"] == 7
        assert payload["tree_terminal"] is True
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.wait(timeout=hang_guard_s())


def test_supervisor_refreshes_reaper_heartbeat_during_cancellation(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
    heartbeat_path = tmp_path / "reaper-heartbeat"
    child_starting = tmp_path / "child-starting"
    child_ready = tmp_path / "child-ready"
    write_attachment_heartbeat(paths, generation=1)
    command = [
        sys.executable,
        "-c",
        "import signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(1)); "
        f"Path({str(child_starting)!r}).touch(); time.sleep(0.3); "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        f"Path({str(child_ready)!r}).touch(); time.sleep(10)",
    ]
    supervisor = _start_supervisor_with_test_heartbeat(
        booley_dir,
        execution_id,
        heartbeat_path,
        command,
    )
    try:
        wait_for(
            lambda: len(_heartbeat_generations(heartbeat_path)) >= 2,
            failure="supervisor did not start its reaper heartbeat",
        )
        wait_for(child_starting.exists, failure="child did not begin startup")
        wait_for(child_ready.exists, failure="child did not finish startup")
        before_cancel = len(_heartbeat_generations(heartbeat_path))
        request_cancellation(paths, signum=signal.SIGINT)
        assert supervisor.wait(timeout=hang_guard_s()) == 130
        after_cancel = len(_heartbeat_generations(heartbeat_path))
        assert after_cancel > before_cancel
        payload = read_json(paths.record)
        assert payload["state"] == "terminal"
        assert payload["tree_terminal"] is True
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.wait(timeout=hang_guard_s())


def test_attachment_heartbeat_expiry_cancels_execution(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
    write_attachment_heartbeat(paths, generation=1)
    # Kept on Popen: the test waits on the supervisor itself.
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
            str(scaled(0.1)),
            "--",
            "sleep",
            "120",
        ],
        env=runtime_env(booley_dir),
    )
    try:
        assert supervisor.wait(timeout=hang_guard_s()) == 130
        payload = json.loads(paths.record.read_text(encoding="utf-8"))
        assert payload["state"] == "terminal"
        assert payload["terminal_cause"] == "attachment_expired"
        assert payload["tree_terminal"] is True
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.wait(timeout=hang_guard_s())


def test_tty_child_owns_the_foreground_process_group(tmp_path: Path) -> None:
    if sys.platform != "linux":
        return
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
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
        os.execve(sys.executable, argv, runtime_env(booley_dir))
    try:
        _waited, status = os.waitpid(pid, 0)
    finally:
        os.close(fd)

    assert os.waitstatus_to_exitcode(status) == 0
    payload = json.loads(paths.record.read_text(encoding="utf-8"))
    assert payload["tree_terminal"] is True


def test_process_set_lists_registered_pids_not_yet_reaped(tmp_path: Path) -> None:
    class FakePopen:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

    processes = SupervisedProcessSet(None, tmp_path, lambda: False)
    first, second = FakePopen(4101), FakePopen(4102)
    assert processes.pids() == ()

    processes.register(first)  # type: ignore[arg-type]
    processes.register(second)  # type: ignore[arg-type]
    assert sorted(processes.pids()) == [4101, 4102]

    processes.unregister(first)  # type: ignore[arg-type]
    assert processes.pids() == (4102,)

    second.returncode = 0  # reaped, not yet unregistered: its PID may be reused
    assert processes.pids() == ()


# Forks a middle process that forks a short-lived grandchild and exits, so the
# grandchild is adopted by the supervisor; then waits for its /proc entry to go.
_ORPHAN_THEN_WAIT_PREFIX = """
import os, sys, time
read_fd, write_fd = os.pipe()
middle = os.fork()
if middle == 0:
    grandchild = os.fork()
    if grandchild == 0:
        time.sleep({orphan_lifetime})
        os._exit(0)
    os.write(write_fd, str(grandchild).encode())
    os._exit(0)
grandchild = int(os.read(read_fd, 32))
os.waitpid(middle, 0)
deadline = time.monotonic() + {deadline_s}
while os.path.exists(f"/proc/{{grandchild}}"):
    if time.monotonic() > deadline:
        sys.exit(9)
    time.sleep(0.01)
"""


def test_running_command_sees_orphaned_grandchild_reaped(tmp_path: Path) -> None:
    """An adopted orphan is reaped while the command runs, not only after it ends."""
    script = _ORPHAN_THEN_WAIT_PREFIX.format(orphan_lifetime=0.2, deadline_s=hang_guard_s())

    result, payload = _run_command(tmp_path, [sys.executable, "-c", script])

    assert result.returncode == 0
    assert payload["exit_code"] == 0
    assert payload["tree_terminal"] is True


def test_signal_exit_status_survives_orphan_reaping(tmp_path: Path) -> None:
    """In-loop reaping of orphans never collects the direct child behind Popen."""
    script = _ORPHAN_THEN_WAIT_PREFIX.format(orphan_lifetime=0, deadline_s=hang_guard_s()) + (
        "import signal\nos.kill(os.getpid(), signal.SIGTERM)\n"
    )

    result, payload = _run_command(tmp_path, [sys.executable, "-c", script])

    assert result.returncode == 143
    assert payload["exit_code"] == 143
    assert payload["tree_terminal"] is True


def test_orphan_reaping_leaves_uncollected_direct_child(tmp_path: Path) -> None:
    """The peek stops at an exited-but-uncollected direct child instead of reaping it."""
    bootstrap = (
        "import os, subprocess, sys, time\n"
        "from booley.runtime import execution_supervisor as supervisor\n"
        "proc = subprocess.Popen([sys.executable, '-c', 'import os; os._exit(3)'])\n"
        "def state():\n"
        "    stat = open(f'/proc/{proc.pid}/stat').read()\n"
        "    return stat.rsplit(')', 1)[1].split()[0]\n"
        f"deadline = time.monotonic() + {hang_guard_s()}\n"
        "while state() != 'Z' and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "class Racing:\n"
        "    pid = proc.pid\n"
        "    returncode = None\n"
        "    def poll(self):\n"
        "        return None\n"
        "supervisor._reap_adopted(Racing())\n"
        "print(state())\n"
        "print(os.waitpid(proc.pid, 0)[1] >> 8)\n"
    )

    result = run_bounded_child(
        [sys.executable, "-c", bootstrap],
        env=runtime_env(tmp_path),
        timeout=2 * hang_guard_s(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["Z", "3"]


def test_orphan_exiting_after_direct_child_is_reaped_by_end(tmp_path: Path) -> None:
    """An orphan that is still a zombie when the direct child is collected is reaped."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
    write_attachment_heartbeat(paths, generation=1)
    grandchild_file = tmp_path / "grandchild.pid"
    marker = tmp_path / "ordered"
    # A middle process forks the grandchild and exits, so the supervisor adopts it.
    command_script = (
        "import os, time\n"
        "middle = os.fork()\n"
        "if middle == 0:\n"
        "    if os.fork() == 0:\n"
        f"        open({str(grandchild_file)!r}, 'w').write(str(os.getpid()))\n"
        "        os._exit(0)\n"
        "    os._exit(0)\n"
        "os.waitpid(middle, 0)\n"
    )
    bootstrap = (
        "import os, time\n"
        "from pathlib import Path\n"
        "import booley.runtime.execution_supervisor as supervisor\n"
        f"grandchild_file = Path({str(grandchild_file)!r})\n"
        f"marker = Path({str(marker)!r})\n"
        "original = supervisor._wait_until_stop\n"
        "def zombie(pid):\n"
        "    try:\n"
        "        stat = Path(f'/proc/{pid}/stat').read_text()\n"
        "    except OSError:\n"
        "        return False\n"
        "    return stat.rsplit(')', 1)[1].split()[0] == 'Z'\n"
        "def wait_until_stop(child, *args):\n"
        f"    deadline = time.monotonic() + {hang_guard_s()}\n"
        "    while time.monotonic() < deadline:\n"
        "        if grandchild_file.exists() and grandchild_file.read_text():\n"
        "            grandchild = int(grandchild_file.read_text())\n"
        "            adopted = Path(f'/proc/{grandchild}/stat').read_text().rsplit(')', 1)[1].split()[1]\n"
        "            if zombie(child.pid) and zombie(grandchild) and int(adopted) == os.getpid():\n"
        "                marker.write_text('ordered')\n"
        "                break\n"
        "        time.sleep(0.005)\n"
        "    return original(child, *args)\n"
        "supervisor._wait_until_stop = wait_until_stop\n"
        "raise SystemExit(supervisor.main())\n"
    )
    result = run_bounded_child(
        [
            sys.executable,
            "-c",
            bootstrap,
            "run",
            "--execution-id",
            execution_id,
            "--grace-seconds",
            "0.25",
            "--",
            sys.executable,
            "-c",
            command_script,
        ],
        env=runtime_env(booley_dir),
        timeout=2 * hang_guard_s(),
    )

    payload = read_json(paths.record)
    assert marker.exists(), "never reached direct child and orphan both zombie"
    assert result.returncode == 0, result.stderr
    assert payload["exit_code"] == 0
    assert payload["state"] == "terminal"
    assert payload["tree_terminal"] is True
    assert not Path(f"/proc/{int(grandchild_file.read_text())}").exists()


def test_register_on_a_terminal_record_keeps_it_and_kills_the_process(tmp_path: Path) -> None:
    execution_id = ExecutionId(uuid.uuid4().hex)
    paths = execution_paths(execution_id, booley_dir=tmp_path)
    terminal = {
        "schema_version": 1,
        "state": "terminal",
        "runtime_identity": None,
        "supervisor": None,
        "leader": None,
        "exit_code": 125,
        "tree_terminal": True,
        "terminal_cause": "orphan_recovered",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    atomic_write_json(paths.record, terminal)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], **new_group_kwargs()
    )
    try:
        SupervisedProcessSet(execution_id, tmp_path, lambda: False).register(child)

        assert read_json(paths.record) == terminal
        assert child.wait(timeout=hang_guard_s()) != 0
    finally:
        child.kill()
        child.wait()


_FORGED_TOKEN = "f" * 32
_RELEASED_PROCESSES = ("worker", "grandchild", "worker_forged", "plain", "forged")
_SLEEPER = """\
import os, sys, time
from pathlib import Path
pid_file = Path(sys.argv[1])
pid_file.with_suffix(".tmp").write_text(str(os.getpid()), encoding="utf-8")
pid_file.with_suffix(".tmp").replace(pid_file)
time.sleep(float(sys.argv[2]))
"""
# The intermediate exits at once, so the supervisor adopts the grandchild.
_WORKER = """\
import os, subprocess, sys
from pathlib import Path
sleeper, seconds, root = sys.argv[1], sys.argv[2], Path(sys.argv[3])
subprocess.run(
    [sys.executable, "-c", "import subprocess, sys; subprocess.Popen(sys.argv[1:])",
     sys.executable, sleeper, str(root / "grandchild.pid"), seconds],
    check=True,
)
forged = dict(os.environ, BOOLEY_RELEASE_TOKEN=sys.argv[4])
subprocess.Popen([sys.executable, sleeper, str(root / "worker_forged.pid"), seconds], env=forged)
os.execv(sys.executable, [sys.executable, sleeper, str(root / "worker.pid"), seconds])
"""
_COMMAND = """\
import os, subprocess, sys, time
from pathlib import Path
from booley.runtime import detached_job
root, worker, sleeper, seconds, forged_token, mode = sys.argv[1:7]
root = Path(root)
try:
    detached_job.spawn(
        [sys.executable, worker, sleeper, seconds, str(root), forged_token],
        root / "job.log",
        jobs_root=root / "jobs",
        run_id="released-1",
    )
except OSError:
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:  # the refused spawn left no child behind
        (root / "refused").write_text("1", encoding="utf-8")
    sys.exit(0)
subprocess.Popen(
    [sys.executable, sleeper, str(root / "plain.pid"), seconds], start_new_session=True
)
forged = dict(os.environ, BOOLEY_RELEASE_TOKEN=forged_token)
subprocess.Popen(
    [sys.executable, sleeper, str(root / "forged.pid"), seconds],
    start_new_session=True,
    env=forged,
)
names = ("worker", "grandchild", "worker_forged", "plain", "forged")
while not all((root / f"{name}.pid").exists() for name in names):
    time.sleep(0.02)
if mode == "exit":
    sys.exit(0)
time.sleep(float(seconds))
"""


def _write_scenario(tmp_path: Path, *, exit_after_ready: bool) -> list[str]:
    """The supervised command that spawns a released worker and unreleased descendants.

    The worker starts a grandchild whose parent has exited and a child with a
    forged release token. The command itself starts a plain ``setsid``
    descendant and a forged-token one.
    """
    (tmp_path / "jobs").mkdir(exist_ok=True)
    scripts = {"command": _COMMAND, "worker": _WORKER, "sleeper": _SLEEPER}
    for name, source in scripts.items():
        (tmp_path / f"{name}.py").write_bytes(source.encode())
    return [
        sys.executable,
        str(tmp_path / "command.py"),
        str(tmp_path),
        str(tmp_path / "worker.py"),
        str(tmp_path / "sleeper.py"),
        str(2 * hang_guard_s()),
        _FORGED_TOKEN,
        "exit" if exit_after_ready else "sleep",
    ]


def _recorded_pids(tmp_path: Path) -> dict[str, int]:
    return {
        name: int((tmp_path / f"{name}.pid").read_text(encoding="utf-8"))
        for name in _RELEASED_PROCESSES
        if (tmp_path / f"{name}.pid").exists()
    }


def _beat(paths: ExecutionPaths, ready: threading.Event) -> None:
    """Change the attachment heartbeat until *ready*; it then expires."""
    generation = 1
    while not ready.wait(0.1):
        generation += 1
        write_attachment_heartbeat(paths, generation=generation)


@pytest.mark.parametrize(
    "ending", ["exited", "cancelled", "force_cancelled", "attachment_expired"]
)
def test_released_worker_outlives_the_attachment(tmp_path: Path, ending: str) -> None:
    """A detached worker and its adopted grandchild survive; every other descendant ends."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
    write_attachment_heartbeat(paths, generation=1)
    expired = ending == "attachment_expired"
    command = _write_scenario(tmp_path, exit_after_ready=ending == "exited")
    # Kept on Popen: the test cancels and waits on the supervisor itself.
    supervisor = subprocess.Popen(
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
            str(scaled(1.0) if expired else 2 * hang_guard_s()),
            "--",
            *command,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=runtime_env(booley_dir, execution_id),
    )
    ready = threading.Event()
    heartbeat = threading.Thread(target=_beat, args=(paths, ready))
    try:
        if expired:
            heartbeat.start()
        wait_for(
            lambda: len(_recorded_pids(tmp_path)) == len(_RELEASED_PROCESSES),
            failure="the scenario did not start every process",
        )
        ready.set()
        if ending == "cancelled":
            request_cancellation(paths)
        elif ending == "force_cancelled":
            request_cancellation(paths, force=True)
        expected = 0 if ending == "exited" else 130
        assert supervisor.wait(timeout=hang_guard_s()) == expected

        pids = _recorded_pids(tmp_path)
        for name in ("plain", "forged", "worker_forged"):
            wait_for(
                lambda name=name: not _non_zombie_alive(pids[name]),
                failure=f"the {name} descendant did not stop",
            )
        assert _non_zombie_alive(pids["worker"])
        assert _non_zombie_alive(pids["grandchild"])
        record = json.loads(paths.record.read_text(encoding="utf-8"))
        assert set(record) == {
            "schema_version",
            "state",
            "runtime_identity",
            "supervisor",
            "leader",
            "exit_code",
            "tree_terminal",
            "terminal_cause",
            "updated_at",
        }
        assert record["state"] == "terminal"
        assert record["tree_terminal"] is True
        assert record["exit_code"] == expected
    finally:
        ready.set()
        if supervisor.poll() is None:
            supervisor.kill()
        # Kept on Popen: reaps the supervisor the test drives itself.
        supervisor.communicate(timeout=hang_guard_s())
        for pid in _recorded_pids(tmp_path).values():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_unrecordable_release_token_starts_no_worker(tmp_path: Path) -> None:
    """A release token that cannot be recorded makes the spawn raise before any worker starts."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    execution_id = _unique_execution_id()
    paths = execution_paths(execution_id, booley_dir=booley_dir)
    write_attachment_heartbeat(paths, generation=1)
    (paths.root / "release-tokens").mkdir()
    command = _write_scenario(tmp_path, exit_after_ready=True)

    result = run_bounded_child(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--",
            *command,
        ],
        env=runtime_env(booley_dir, execution_id),
        timeout=2 * hang_guard_s(),
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "refused").exists()
    assert _recorded_pids(tmp_path) == {}
