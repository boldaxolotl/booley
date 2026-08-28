"""Host Runtime Attachment drives the in-runtime execution lifecycle."""

from __future__ import annotations

import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path

from booley.harness import runtime_attachment
from booley.runtime import execution_records, project_dir


def _fake_docker(tmp_path: Path, monkeypatch, project: Path) -> None:
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/usr/bin/python3\n"
        "import os,sys\n"
        "args=sys.argv[1:]\n"
        "start=args.index('python3')\n"
        "os.execvp(args[start],args[start:])\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    project_dir.reset_cache()


def _exiting_docker(tmp_path: Path, monkeypatch, project: Path) -> None:
    binary = tmp_path / "docker"
    binary.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    project_dir.reset_cache()


def test_normal_command_uses_structured_terminal_result(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley_project"
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


def test_sigint_requests_cancellation_and_returns_130(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley_project"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)

    interrupter = threading.Thread(
        target=lambda: (time.sleep(0.2), os.kill(os.getpid(), signal.SIGINT)),
        daemon=True,
    )
    interrupter.start()
    result = runtime_attachment.run_command(
        root,
        "session-name",
        [sys.executable, "-c", "import time; time.sleep(120)"],
        tty=False,
    )
    interrupter.join(timeout=2)

    assert result.exit_code == 130
    assert result.state == "terminal"
    assert result.terminal_cause == "cancelled"


def test_sigterm_preserves_signal_exit_semantics(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    data = root / ".booley_project"
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
    data = root / ".booley_project"
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
    data = root / ".booley_project"
    data.mkdir(parents=True)
    _fake_docker(tmp_path, monkeypatch, data)
    script = (
        "import signal,time\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "time.sleep(120)\n"
    )

    def interrupt_twice() -> None:
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    interrupter = threading.Thread(target=interrupt_twice, daemon=True)
    interrupter.start()
    started = time.monotonic()
    result = runtime_attachment.run_command(
        root, "session-name", [sys.executable, "-c", script], tty=False
    )

    assert result.exit_code == 130
    assert result.tree_terminal is True
    assert time.monotonic() - started < 2


def test_missing_container_protocol_fails_fast_with_actionable_message(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    root = tmp_path / "project"
    data = root / ".booley_project"
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
    assert "booley session refresh" in caplog.text
    assert time.monotonic() - started < 1


def test_protocol_version_mismatch_fails_closed(tmp_path: Path, monkeypatch, caplog) -> None:
    root = tmp_path / "project"
    data = root / ".booley_project"
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
    assert "booley session refresh" in caplog.text
    assert time.monotonic() - started < 2
