"""Host Sandbox Attachment drives the in-runtime execution lifecycle."""

from __future__ import annotations

import os
import signal
import stat
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from booley.runtime import (
    booley_dir,
    execution_records,
    job_slots,
    lifecycle_lock,
    runtime_attachment,
    session_runtime,
)
from booley.runtime.pid import RUNNING, UNKNOWN, ProcessIdentity, observe_process

_SRC_ROOT = Path(__file__).parents[2] / "src"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Sandbox Attachment execution supervision requires Linux",
)


def _fake_docker(tmp_path: Path, monkeypatch, project: Path) -> None:
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/usr/bin/python3\n"
        "import os,sys\n"
        "args=sys.argv[1:]\n"
        "for index,arg in enumerate(args):\n"
        " if arg=='-e':\n"
        "  key,value=args[index+1].split('=',1)\n"
        "  os.environ[key]=value\n"
        "start=args.index('python3')\n"
        "os.execvp(args[start],args[start:])\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(
        "PYTHONPATH",
        f"{_SRC_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    )
    monkeypatch.setenv("BOOLEY_DIR", str(project))
    booley_dir.reset_cache()


def _exiting_docker(tmp_path: Path, monkeypatch, project: Path) -> None:
    binary = tmp_path / "docker"
    binary.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOOLEY_DIR", str(project))
    booley_dir.reset_cache()


def _wait_for_execution_leader_exit(project: Path, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    executions = project / ".runtime" / "executions"
    while time.monotonic() < deadline:
        for record in executions.glob("*/record.json"):
            payload = execution_records.read_json(record)
            leader = ProcessIdentity.from_payload(
                payload.get("leader") if payload is not None else None
            )
            if leader is not None and observe_process(leader).state not in {RUNNING, UNKNOWN}:
                return True
        time.sleep(0.01)
    return False


def _interrupt_when(predicate, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            os.kill(os.getpid(), signal.SIGINT)
            return
        time.sleep(0.01)


@pytest.mark.parametrize(
    ("denied", "exit_code", "diagnostic"),
    [
        pytest.param(False, 127, "command not found", id="missing-command"),
        pytest.param(True, 126, "permission denied", id="permission-denied"),
    ],
)
def test_spawn_failure_preserves_command_status(
    tmp_path: Path, monkeypatch, capfd, caplog, denied, exit_code, diagnostic
) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    monkeypatch.setattr(lifecycle_lock, "host_lifecycle_lock", lambda *_: nullcontext())
    monkeypatch.setattr(session_runtime, "_recover_before_lifecycle", lambda *_: None)
    monkeypatch.setattr(
        session_runtime,
        "_select_or_start_project_runtime",
        lambda _: ("session-name", {"BOOLEY_DIR": str(data)}),
    )
    executable = str(tmp_path / "missing-command")
    if denied:
        command = tmp_path / "command"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o644)
        executable = str(command)

    result = session_runtime.enter(root, [executable, "private-argument"], tty=False)

    assert result == exit_code
    captured = capfd.readouterr()
    assert captured.err == f"{diagnostic}: {executable}\n"
    assert captured.out == ""
    assert caplog.records == []


def test_normal_command_uses_structured_terminal_result(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)

    result = runtime_attachment.run_command(
        root,
        "session-name",
        [sys.executable, "-c", "raise SystemExit(7)"],
        tty=False,
    )

    assert result.exit_code == 7
    assert result.state == "terminal"
    assert result.tree_terminal is True


def test_explicit_environment_reaches_attached_command(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    observed = tmp_path / "proxy.txt"
    script = f"import os; open({str(observed)!r}, 'w').write(os.environ['HTTPS_PROXY'])"

    result = runtime_attachment.run_command(
        root,
        "session-name",
        [sys.executable, "-c", script],
        tty=False,
        env={"HTTPS_PROXY": "http://booley-proxy:8080"},
    )

    assert result.exit_code == 0
    assert observed.read_text(encoding="utf-8") == "http://booley-proxy:8080"


def test_sigint_requests_cancellation_and_returns_130(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)

    ready = tmp_path / "command-ready"
    interrupter = threading.Thread(
        target=lambda: _interrupt_when(ready.exists),
        daemon=True,
    )
    interrupter.start()
    result = runtime_attachment.run_command(
        root,
        "session-name",
        [sys.executable, "-c", f"import time; open({str(ready)!r}, 'w').close(); time.sleep(120)"],
        tty=False,
    )
    interrupter.join(timeout=2)

    assert ready.exists()
    assert result.exit_code == 130
    assert result.state == "terminal"
    assert result.terminal_cause == "cancelled"


def test_sigterm_preserves_signal_exit_semantics(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    interrupter = threading.Thread(
        target=lambda: (time.sleep(0.2), os.kill(os.getpid(), signal.SIGTERM)),
        daemon=True,
    )
    interrupter.start()

    result = runtime_attachment.run_command(
        root,
        "session-name",
        [sys.executable, "-c", "import time; time.sleep(120)"],
        tty=False,
    )

    assert result.exit_code == 143
    assert result.tree_terminal is True


def test_command_that_handles_sigint_and_exits_zero_preserves_zero(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    ready = tmp_path / "handler-ready"

    def interrupt_when_ready() -> None:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)

    interrupter = threading.Thread(
        target=interrupt_when_ready,
        daemon=True,
    )
    interrupter.start()
    script = (
        "import signal,sys,time\n"
        "signal.signal(signal.SIGINT,lambda *_args: sys.exit(0))\n"
        f"open({str(ready)!r},'w').close()\n"
        "time.sleep(120)\n"
    )

    result = runtime_attachment.run_command(
        root,
        "session-name",
        [sys.executable, "-c", script],
        tty=False,
    )

    assert result.exit_code == 0
    assert result.terminal_cause == "cancelled"


def test_second_sigint_forces_cleanup(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    ready = tmp_path / "handlers-ready"
    script = (
        "import signal,time\n"
        "time.sleep(0.3)\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(120)\n"
    )

    second_interrupt_sent = threading.Event()

    def interrupt_twice() -> None:
        _interrupt_when(ready.exists)
        if not ready.exists():
            return
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
        second_interrupt_sent.set()

    interrupter = threading.Thread(target=interrupt_twice, daemon=True)
    interrupter.start()
    result = runtime_attachment.run_command(
        root, "session-name", [sys.executable, "-c", script], tty=False
    )

    interrupter.join(timeout=2)
    assert ready.exists()
    assert second_interrupt_sent.is_set()
    assert result.exit_code == 130
    assert result.tree_terminal is True
    assert list((data / ".runtime" / "executions").glob("*/force-cancel"))


def test_sigint_after_root_exit_cancels_surviving_descendant(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    descendant_pid_file = tmp_path / "descendant.pid"
    descendant_ready = tmp_path / "descendant.ready"
    descendant = (
        "import signal,time\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"open({str(descendant_ready)!r},'w').close()\n"
        "time.sleep(120)\n"
    )
    command = (
        "import pathlib,subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.executable,'-c',{descendant!r}],start_new_session=True)\n"
        f"open({str(descendant_pid_file)!r},'w').write(str(p.pid))\n"
        f"ready=pathlib.Path({str(descendant_ready)!r})\n"
        "while not ready.exists(): time.sleep(0.01)\n"
    )

    interrupter = threading.Thread(
        target=lambda: (
            _wait_for_execution_leader_exit(data) and os.kill(os.getpid(), signal.SIGINT)
        ),
        daemon=True,
    )
    interrupter.start()
    result = runtime_attachment.run_command(
        root,
        "session-name",
        [sys.executable, "-c", command],
        tty=False,
    )
    interrupter.join(timeout=2)

    assert result.exit_code == 130
    assert result.terminal_cause == "cancelled"
    assert result.tree_terminal is True


def test_sigint_during_slot_queue_wait_withdraws_waiter(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    slots = data / "runtime" / "jobs" / "slots"
    holder_store = job_slots.SlotStore(slots)
    holder = holder_store.acquire(job_slots.CLASS_HEAVY, pid=os.getpid())
    command = (
        "import os\n"
        "from booley.runtime import job_slots\n"
        "from booley.runtime.booley_dir import resolve_booley_dir\n"
        "root=resolve_booley_dir()/'runtime'/'jobs'/'slots'\n"
        "store=job_slots.SlotStore(root)\n"
        "store.acquire(job_slots.CLASS_HEAVY,pid=os.getpid(),poll_interval=0.02)\n"
    )
    interrupter = threading.Thread(
        target=lambda: _interrupt_when(
            lambda: bool(holder_store.snapshot(job_slots.CLASS_HEAVY)[1])
        ),
        daemon=True,
    )
    interrupter.start()
    try:
        result = runtime_attachment.run_command(
            root, "session-name", [sys.executable, "-c", command], tty=False
        )
        assert result.exit_code == 130
        assert holder_store.snapshot(job_slots.CLASS_HEAVY)[1] == []
    finally:
        holder_store.release(holder)


def test_sigint_after_promotion_allows_repeated_slot_cleanup(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    promoted = tmp_path / "promoted"
    command = (
        "import os,time\n"
        "from booley.runtime import job_slots\n"
        "from booley.runtime.booley_dir import resolve_booley_dir\n"
        "root=resolve_booley_dir()/'runtime'/'jobs'/'slots'\n"
        "store=job_slots.SlotStore(root)\n"
        "store.acquire(job_slots.CLASS_HEAVY,pid=os.getpid(),poll_interval=0.02)\n"
        f"open({str(promoted)!r},'w').close()\n"
        "time.sleep(120)\n"
    )
    interrupter = threading.Thread(
        target=lambda: _interrupt_when(promoted.exists),
        daemon=True,
    )
    interrupter.start()
    result = runtime_attachment.run_command(
        root, "session-name", [sys.executable, "-c", command], tty=False
    )
    store = job_slots.SlotStore(data / "runtime" / "jobs" / "slots")

    assert result.exit_code == 130
    assert len(store.reap(job_slots.CLASS_HEAVY)) == 1
    assert store.reap(job_slots.CLASS_HEAVY) == []


def test_missing_container_protocol_fails_fast_with_actionable_message(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _exiting_docker(tmp_path, monkeypatch, data)
    monkeypatch.setattr(runtime_attachment, "_STARTUP_LIMIT_SECONDS", 0.1)

    started = time.monotonic()
    result = runtime_attachment.run_command(
        root,
        "stale-session",
        ["true"],
        tty=False,
    )

    assert result.exit_code == 125
    assert result.terminal_cause == "protocol_unavailable"
    assert "execution protocol" in caplog.text
    assert time.monotonic() - started < 1


def test_protocol_version_mismatch_fails_closed(tmp_path: Path, monkeypatch, caplog) -> None:
    root = tmp_path / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    real_read = runtime_attachment.read_json

    def mismatched_record(path: Path):
        payload = real_read(path)
        if payload is not None:
            return {**payload, "schema_version": execution_records.PROTOCOL_VERSION + 1}
        return None

    monkeypatch.setattr(runtime_attachment, "read_json", mismatched_record)
    started = time.monotonic()
    result = runtime_attachment.run_command(
        root,
        "stale-session",
        [sys.executable, "-c", "import time; time.sleep(120)"],
        tty=False,
    )

    assert result.exit_code == 125
    assert result.tree_terminal is False
    assert result.terminal_cause == "protocol_mismatch"
    assert "execution protocol" in caplog.text
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("has_root", [True, False], ids=["root", "no-root"])
def test_protocol_failure_logs_fixed_message_and_one_remedy(
    tmp_path, monkeypatch, caplog, has_root
):
    from unittest.mock import Mock

    from booley.runtime import interactive_docker, session_issuance, session_runtime

    def forbidden(*_args, **_kwargs):
        raise AssertionError("protocol failure must not load, validate or compare identity")

    for owner, name in (
        (session_issuance, "load_issued_snapshot"),
        (session_issuance, "validate"),
        (interactive_docker, "compare_issued_build"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    remedy = Mock(return_value="run the selected remedy")
    monkeypatch.setattr(session_runtime, "sandbox_remedy", remedy)
    cause = "protocol_mismatch" if has_root else "protocol_unavailable"
    runtime_attachment._report_protocol_failure(
        runtime_attachment.ExecutionResult(125, "unrecoverable", False, cause),
        tmp_path if has_root else None,
    )
    assert "Sandbox execution protocol is unavailable or incompatible; " in caplog.text
    if has_root:
        remedy.assert_called_once_with(tmp_path, runtime_only=True)
        assert "run the selected remedy" in caplog.text
    else:
        remedy.assert_not_called()
        assert session_runtime.SANDBOX_HOST_INSPECTION_REMEDY in caplog.text


def test_cwd_disappearing_at_startup_is_not_reported_as_image_defect(
    tmp_path, monkeypatch, caplog
):
    from booley.runtime import session_runtime

    data = tmp_path / ".booley"
    data.mkdir()
    _exiting_docker(tmp_path, monkeypatch, data)
    monkeypatch.setattr(session_runtime, "_probe_command_cwd", lambda *_: None)
    runtime_attachment.run_command(
        tmp_path, "immutable-id", ["pwd"], tty=False, cwd="/work/linked"
    )
    assert "selected directory '/work/linked'" in caplog.text
    assert "published image" not in caplog.text
