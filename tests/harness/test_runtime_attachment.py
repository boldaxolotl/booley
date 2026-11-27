"""Host Sandbox Attachment drives the in-runtime execution lifecycle."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from booley.runtime import execution_records, job_slots, runtime_attachment
from booley.runtime.pid import RUNNING, UNKNOWN, ProcessIdentity, observe_process
from tests.harness.session_docker_fake import (
    headless_session_entry,
    install_sandbox_exec,
    serve_sandbox_docker,
)
from tests.sharing_error_support import hold_opens
from tests.time_budget import hang_guard_s

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Sandbox Attachment execution supervision requires Linux",
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Project root whose Sandbox `docker exec` runs on this host."""
    root = tmp_path / "project"
    (root / ".booley").mkdir(parents=True)
    install_sandbox_exec(tmp_path, monkeypatch, root / ".booley")
    return root


def _attach(root: Path, script: str, **kwargs) -> runtime_attachment.ExecutionResult:
    """Run a Python *script* in the Sandbox through the attachment, without a terminal."""
    return runtime_attachment.run_command(
        root, "session-name", [sys.executable, "-c", script], tty=False, **kwargs
    )


def _wait_for_execution_leader_exit(project: Path, *, timeout_s: float = hang_guard_s()) -> bool:
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


def _signal_when(
    predicate: Callable[[], object],
    signum: int = signal.SIGINT,
    *,
    timeout_s: float = hang_guard_s(),
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            os.kill(os.getpid(), signum)
            return
        time.sleep(0.01)


def _signal_in_thread(
    predicate: Callable[[], object], signum: int = signal.SIGINT
) -> threading.Thread:
    thread = threading.Thread(target=lambda: _signal_when(predicate, signum), daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize(
    ("denied", "exit_code", "diagnostic"),
    [
        pytest.param(False, 127, "command not found", id="missing-command"),
        pytest.param(True, 126, "permission denied", id="permission-denied"),
    ],
)
def test_spawn_failure_preserves_command_status(
    project: Path, tmp_path: Path, capfd, caplog, denied, exit_code, diagnostic
) -> None:
    executable = str(tmp_path / "missing-command")
    if denied:
        command = tmp_path / "command"
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o644)
        executable = str(command)

    result = runtime_attachment.run_command(
        project, "session-name", [executable, "private-argument"], tty=False
    )

    assert result.exit_code == exit_code
    captured = capfd.readouterr()
    assert captured.err == f"{diagnostic}: {executable}\n"
    assert captured.out == ""
    assert caplog.records == []


def test_explicit_environment_reaches_attached_command(project: Path, tmp_path: Path) -> None:
    observed = tmp_path / "proxy.txt"
    script = (
        f"import os, pathlib; pathlib.Path({str(observed)!r}).write_text(os.environ['HTTPS_PROXY'])\n"
        "raise SystemExit(7)"
    )

    result = _attach(project, script, env={"HTTPS_PROXY": "http://booley-proxy:8080"})

    assert (result.exit_code, result.state, result.tree_terminal) == (7, "terminal", True)
    assert observed.read_text(encoding="utf-8") == "http://booley-proxy:8080"


@pytest.mark.parametrize(
    ("handler", "signum", "exit_code"),
    [
        pytest.param("", signal.SIGINT, 130, id="sigint"),
        pytest.param("", signal.SIGTERM, 143, id="sigterm"),
        pytest.param(
            "signal.signal(signal.SIGINT,lambda *_args: sys.exit(0))\n",
            signal.SIGINT,
            0,
            id="handled-sigint-exits-zero",
        ),
    ],
)
def test_host_signal_cancels_command_keeping_its_exit_status(
    project: Path, tmp_path: Path, handler: str, signum: int, exit_code: int
) -> None:
    ready = tmp_path / "command-ready"
    script = (
        f"import signal,sys,time\n{handler}open({str(ready)!r}, 'w').close()\ntime.sleep(120)\n"
    )
    interrupter = _signal_in_thread(ready.exists, signum)

    result = _attach(project, script)
    interrupter.join(timeout=hang_guard_s())

    assert ready.exists()
    assert (result.exit_code, result.terminal_cause, result.tree_terminal) == (
        exit_code,
        "cancelled",
        True,
    )


def test_second_sigint_forces_cleanup(project: Path, tmp_path: Path) -> None:
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
        _signal_when(ready.exists)
        if not ready.exists():
            return
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
        second_interrupt_sent.set()

    interrupter = threading.Thread(target=interrupt_twice, daemon=True)
    interrupter.start()
    result = _attach(project, script)

    interrupter.join(timeout=hang_guard_s())
    assert ready.exists()
    assert second_interrupt_sent.is_set()
    assert result.exit_code == 130
    assert result.tree_terminal is True
    assert list((project / ".booley" / ".runtime" / "executions").glob("*/force-cancel"))


def test_sigint_after_root_exit_cancels_surviving_descendant(
    project: Path, tmp_path: Path
) -> None:
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

    interrupter = _signal_in_thread(lambda: _wait_for_execution_leader_exit(project / ".booley"))
    result = _attach(project, command)
    interrupter.join(timeout=hang_guard_s())

    assert result.exit_code == 130
    assert result.terminal_cause == "cancelled"
    assert result.tree_terminal is True


def test_sigint_during_slot_queue_wait_withdraws_waiter(project: Path) -> None:
    slots = project / ".booley" / "runtime" / "jobs" / "slots"
    alone = job_slots.MemoryClaim(budget_mb=1024, run_alone=True)
    holder_store = job_slots.SlotStore(slots)
    holder = holder_store.acquire(job_slots.CLASS_MEMORY, pid=os.getpid(), memory=alone)
    command = (
        "import os\n"
        "from booley.runtime import job_slots\n"
        "from booley.runtime.booley_dir import resolve_booley_dir\n"
        "root=resolve_booley_dir()/'runtime'/'jobs'/'slots'\n"
        "store=job_slots.SlotStore(root)\n"
        "alone=job_slots.MemoryClaim(budget_mb=1024,run_alone=True)\n"
        "store.acquire(job_slots.CLASS_MEMORY,pid=os.getpid(),poll_interval=0.02,memory=alone)\n"
    )
    _signal_in_thread(lambda: bool(holder_store.snapshot(job_slots.CLASS_MEMORY)[1]))
    try:
        result = _attach(project, command)
        assert result.exit_code == 130
        assert holder_store.snapshot(job_slots.CLASS_MEMORY)[1] == []
    finally:
        holder_store.release(holder)


def test_sigint_after_promotion_allows_repeated_slot_cleanup(
    project: Path, tmp_path: Path
) -> None:
    promoted = tmp_path / "promoted"
    command = (
        "import os,time\n"
        "from booley.runtime import job_slots\n"
        "from booley.runtime.booley_dir import resolve_booley_dir\n"
        "root=resolve_booley_dir()/'runtime'/'jobs'/'slots'\n"
        "store=job_slots.SlotStore(root)\n"
        "alone=job_slots.MemoryClaim(budget_mb=1024,run_alone=True)\n"
        "store.acquire(job_slots.CLASS_MEMORY,pid=os.getpid(),poll_interval=0.02,memory=alone)\n"
        f"open({str(promoted)!r},'w').close()\n"
        "time.sleep(120)\n"
    )
    _signal_in_thread(promoted.exists)
    result = _attach(project, command)
    store = job_slots.SlotStore(project / ".booley" / "runtime" / "jobs" / "slots")

    assert result.exit_code == 130
    assert len(store.reap(job_slots.CLASS_MEMORY)) == 1
    assert store.reap(job_slots.CLASS_MEMORY) == []


def test_missing_container_protocol_fails_fast_with_actionable_message(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    install_sandbox_exec(tmp_path, monkeypatch, project / ".booley", protocol=False)

    result = runtime_attachment.run_command(project, "stale-session", ["true"], tty=False)

    assert result.exit_code == 125
    assert result.terminal_cause == "protocol_unavailable"
    assert [record.getMessage() for record in caplog.records] == [
        (
            "Sandbox execution protocol is unavailable or incompatible; restore Docker "
            "connectivity and inspect Sandbox ownership on the host before repair"
        )
    ]


def test_protocol_version_mismatch_fails_closed(
    project: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    real_read = runtime_attachment.read_json

    def mismatched_record(path: Path):
        payload = real_read(path)
        if payload is not None:
            return {**payload, "schema_version": execution_records.PROTOCOL_VERSION + 1}
        return None

    # The shipped supervisor always writes this Booley's protocol version, so a Sandbox
    # image from another version is simulated by rewriting the record the host reads.
    monkeypatch.setattr(runtime_attachment, "read_json", mismatched_record)
    result = _attach(project, "import time; time.sleep(120)")

    assert result.exit_code == 125
    assert result.tree_terminal is False
    assert result.terminal_cause == "protocol_mismatch"
    assert "execution protocol" in caplog.text


def test_cwd_disappearing_at_startup_is_not_reported_as_image_defect(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    install_sandbox_exec(tmp_path, monkeypatch, project / ".booley", protocol=False)
    runtime_attachment.run_command(project, "immutable-id", ["pwd"], tty=False, cwd=str(tmp_path))
    assert f"selected directory {str(tmp_path)!r}" in caplog.text
    assert "published image" not in caplog.text


# --- Windows sharing errors on the record poll (#2187) ------------------------------


class _ExitedTransport:
    """A transport process that has already exited."""

    def poll(self) -> int:
        return 0


# A sharing violation matters only on a record poll after the transport exited, and
# `run_command` draws a random execution id, so no public call can hold that record's
# open at that moment: these tests drive the poll loop directly.
def _drive_with_held_record(tmp_path: Path, monkeypatch, held_opens: int):
    """Drive a terminal record whose first ``held_opens`` opens hit a sharing violation."""
    paths = execution_records.execution_paths("e" * 32, booley_dir=tmp_path)
    paths.root.mkdir(parents=True)
    execution_records.atomic_write_json(
        paths.record,
        {
            "schema_version": execution_records.PROTOCOL_VERSION,
            "state": "terminal",
            "exit_code": 7,
            "tree_terminal": True,
            "terminal_cause": "exited",
        },
    )
    # With no startup grace, reading a held record as absent would fail the run.
    monkeypatch.setattr(runtime_attachment, "_STARTUP_LIMIT_SECONDS", 0.0)
    hold_opens(monkeypatch, {paths.record: held_opens})
    return runtime_attachment._drive_execution(
        _ExitedTransport(), paths, runtime_attachment._PendingSignals()
    )


@pytest.mark.parametrize(
    ("held", "logged"),
    [pytest.param(1, False, id="transient"), pytest.param(3, True, id="outlasts-one-poll")],
)
def test_record_poll_survives_sharing_error(
    tmp_path: Path, monkeypatch, caplog, held: int, logged: bool
) -> None:
    result = _drive_with_held_record(tmp_path, monkeypatch, held)
    assert (result.exit_code, result.terminal_cause) == (7, "exited")
    assert ("Cannot read execution record" in caplog.text) is logged


# In-Sandbox script tails that lose the command; killing the supervisor also ends the
# transport, as a Sandbox stop does.
_KILL = "os.kill(os.getppid(), signal.SIGKILL)\n"
_CANCEL = "r.request_cancellation(paths, signum=signal.SIGTERM, reason={!r})\ntime.sleep(60)\n"
_RECOVERED = (
    # Wait out the supervisor's own `running` write, so it cannot replace this record.
    f"end = time.monotonic() + {hang_guard_s()}\n"
    "while (r.read_json(paths.record) or {}).get('state') != 'running' and time.monotonic() < end:\n"
    "    time.sleep(0.01)\n"
    "r.atomic_write_json(paths.record, dict(schema_version=r.PROTOCOL_VERSION, state='terminal',"
    " exit_code=125, tree_terminal=True, terminal_cause='orphan_recovered'))\n"
) + _KILL
_TRANSPORT = "transport_lost"
_LEASE = "lease_recovery"
# case: (the Sandbox afterwards, script tail, exit code, recorded cause, message facts)
_LOSSES = {
    "stopped": ("stopped", _KILL, 125, "recovery_timeout", ["stopped", "`booley session up`"]),
    "replaced": ("replaced", _KILL, 125, "recovery_timeout", ["was replaced"]),
    "still-running": (
        "same",
        _KILL,
        125,
        "recovery_timeout",
        ["still running", "recovery timeout"],
    ),
    "cannot-inspect": ("unknown", _KILL, 125, "recovery_timeout", ["cannot inspect"]),
    "transport-lost": ("same", _CANCEL.format(_TRANSPORT), 143, _TRANSPORT, ["`docker exec`"]),
    "unknown-cause": ("same", _CANCEL.format(_LEASE), 143, _LEASE, ["cause lease_recovery"]),
    "orphan-recovered": ("same", _RECOVERED, 125, "orphan_recovered", ["supervisor died"]),
}


@pytest.mark.parametrize("case", list(_LOSSES))
def test_lost_execution_names_its_cause(
    project: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    after, tail, exit_code, cause, facts = _LOSSES[case]
    docker = serve_sandbox_docker(monkeypatch)
    entry = docker.containers["lost-box"] = headless_session_entry("lost-box", project)
    started_id = entry["Id"] = "c" * 64
    # What the Project's Sandbox looks like once the command has been lost.
    entry["State"]["Running"] = after != "stopped"
    if after == "replaced":
        entry["Id"] = "d" * 64
    docker.failing = {"ps"} if after == "unknown" else set()
    # boundary: the recovery window the host clock is measured against
    monkeypatch.setattr(runtime_attachment, "_RECOVERY_LIMIT_SECONDS", 0.5)
    interrupt = signal.getsignal(signal.SIGINT)
    inspecting: list[object] = []
    docker.on_call = lambda _argv: inspecting.append(signal.getsignal(signal.SIGINT))
    script = (
        "import os,signal,time\nfrom booley.runtime import execution_records as r\n"
        "paths = r.execution_paths(os.environ[r.RUNTIME_EXECUTION_ENV])\n"
    ) + tail

    result = runtime_attachment.run_command(
        project, started_id, [sys.executable, "-c", script], tty=False, sandbox_name="lost-box"
    )

    assert (result.exit_code, result.terminal_cause) == (exit_code, cause)
    [message] = [record.getMessage() for record in caplog.records]
    assert all(fact in message for fact in ["'lost-box'", *facts]), message
    # A hung inspection after the loss must stay interruptible by Ctrl-C.
    assert set(inspecting) == ({interrupt} if cause == "recovery_timeout" else set())
