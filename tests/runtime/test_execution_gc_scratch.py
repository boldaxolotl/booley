"""Execution retention must release rendezvous metadata without losing scratch proof."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import pytest
from tests.execution_test_support import runtime_env, wait_for
from tests.runtime.scratch_support import scratch_process_ownership_supported

from booley.core import temporary_workspace as tw
from booley.core.lease_shutdown import ShutdownGroup
from booley.runtime.execution_records import (
    ExecutionId,
    execution_paths,
    gc_terminal_executions,
    read_json,
    write_attachment_heartbeat,
)

pytestmark = pytest.mark.skipif(
    not scratch_process_ownership_supported(), reason="Linux scratch identity"
)


@pytest.fixture
def completed_execution(tmp_path):
    state, registry = tmp_path / "state", tmp_path / "scratch"
    state.mkdir()
    registry.mkdir()
    paths = execution_paths(ExecutionId("d" * 32), booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    snapshot, receipt = tmp_path / "running.json", tmp_path / "root"
    native = "import shutil,sys; shutil.copyfile(sys.argv[1],sys.argv[2])"
    endpoint = (
        "from pathlib import Path\n"
        "import sys\n"
        "from booley.core import temporary_workspace as tw\n"
        "tw._delete_owned = lambda *args, **kwargs: False\n"
        f"with tw.temporary_workspace(parent=Path({str(registry)!r})) as raw:\n"
        f"    Path({str(receipt)!r}).write_text(raw)\n"
        f"    tw.run_guarded([sys.executable, '-c', {native!r}, "
        f"str(Path(raw) / tw._MARKER), {str(snapshot)!r}], timeout=15)\n"
    )
    _run_supervisor(
        state,
        paths.root.name,
        endpoint,
        setup="ShutdownGroup.remove_terminal = lambda *args, **kwargs: False",
    )
    assert read_json(paths.record)["tree_terminal"] is True
    root = Path(receipt.read_text())
    # Replay the authentic running marker: publication after reaping was interrupted.
    tw._write_marker(root, json.loads(snapshot.read_text()))
    return state, paths, root, next(paths.root.glob(".booley-shutdown-*"))


def _run_supervisor(state: Path, execution_id: str, endpoint: str, *, setup: str = "") -> None:
    bootstrap = (
        "from booley.core.lease_shutdown import ShutdownGroup\n"
        "from booley.runtime import execution_supervisor as supervisor\n"
        f"{setup}\n"
        "raise SystemExit(supervisor.main())\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            bootstrap,
            "run",
            "--execution-id",
            execution_id,
            "--attachment-timeout-seconds",
            "60",
            "--",
            sys.executable,
            "-c",
            endpoint,
        ],
        env=runtime_env(state),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("publication_failure", [False, True])
def test_actual_terminal_gc_preserves_recoverable_scratch_proof(
    completed_execution, monkeypatch, publication_failure
):
    state, paths, root, group = completed_execution
    now = paths.record.stat().st_mtime + 10
    if publication_failure:

        def fail_publication(*_) -> None:
            raise OSError("full")

        with monkeypatch.context() as patch:
            patch.setattr(tw, "_write_marker", fail_publication)
            assert gc_terminal_executions(state, now=now, retention_seconds=1) == []
        assert paths.record.exists() and group.exists()
        marker, _ = tw._validated_marker(root)
        assert any(value["state"] == "running" for value in marker["launches"].values())
    assert gc_terminal_executions(state, now=now, retention_seconds=1) == [paths.root.name]
    assert not paths.root.exists()
    marker, _ = tw._validated_marker(root)
    assert marker["launches"] and all(
        value == {"state": "complete"} for value in marker["launches"].values()
    )
    assert tw._delete_owned(root, require_dead=True)
    assert not root.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "live",
        "live_member",
        "nonterminal",
        "corrupt",
        "null",
        "array",
        "scalar",
        "unknown",
        "symlink",
        "marker",
    ],
)
def test_terminal_gc_retains_uncertain_shutdown_groups(completed_execution, damage):
    state, paths, root, group_path = completed_execution
    group = ShutdownGroup(group_path)
    if damage in {"live", "live_member", "nonterminal"}:
        with group.locked() as data:
            if damage == "live":
                data["owner"] = tw.process_identity(os.getpid())
            elif damage == "live_member":
                member = next(iter(data["members"].values()))
                member["identity"] = tw.process_identity(os.getpid())
                member["ancestors"] = [member["identity"]]
            else:
                data["terminal"] = False
    elif damage == "corrupt":
        (group_path / "state.json").write_text("invalid")
    elif damage in {"null", "array", "scalar"}:
        (group_path / "state.json").write_text(
            {"null": "null", "array": "[{}]", "scalar": "1"}[damage]
        )
    elif damage == "unknown":
        (group_path / "foreign").write_text("preserve")
    elif damage == "marker":
        (root / tw._MARKER).unlink()
    else:
        original = group_path.with_name("retained-original")
        group_path.rename(original)
        group_path.symlink_to(original, target_is_directory=True)
    assert (
        gc_terminal_executions(state, now=paths.record.stat().st_mtime + 10, retention_seconds=1)
        == []
    )
    assert paths.record.exists() and root.exists() and group_path.exists()


def test_terminal_gc_removes_metadata_when_referenced_scratch_is_already_gone(completed_execution):
    state, paths, root, _ = completed_execution
    assert tw._delete_owned(root, require_dead=True)
    assert gc_terminal_executions(
        state, now=paths.record.stat().st_mtime + 10, retention_seconds=1
    ) == [paths.root.name]
    assert not paths.root.exists()


def test_terminal_gc_preserves_unrecognized_execution_payload(completed_execution):
    state, paths, root, group = completed_execution
    payload = paths.root / "foreign"
    payload.mkdir()
    (payload / "sentinel").write_text("preserve")
    assert (
        gc_terminal_executions(state, now=paths.record.stat().st_mtime + 10, retention_seconds=1)
        == []
    )
    assert paths.record.exists() and root.exists() and group.exists()
    assert (payload / "sentinel").read_text() == "preserve"


def test_terminal_gc_never_unlinks_replaced_metadata(completed_execution, monkeypatch):
    state, paths, _, group = completed_execution
    remove = ShutdownGroup._remove_metadata
    original = group.with_name("retained-original")

    def substitute(self, identity):
        self.path.rename(original)
        self.path.mkdir(mode=0o700)
        (self.path / "foreign").write_text("preserve")
        return remove(self, identity)

    monkeypatch.setattr(ShutdownGroup, "_remove_metadata", substitute)
    assert (
        gc_terminal_executions(state, now=paths.record.stat().st_mtime + 10, retention_seconds=1)
        == []
    )
    assert paths.record.exists() and (original / "state.json").exists()
    assert (group / "foreign").read_text() == "preserve"


def test_recent_terminal_record_retires_group_before_record_retention(completed_execution):
    state, paths, root, group = completed_execution
    assert gc_terminal_executions(state) == []
    assert paths.record.exists() and not group.exists()
    assert tw._delete_owned(root, require_dead=True)


@pytest.mark.parametrize("lock_kind", ["group", "scratch"])
def test_terminal_gc_does_not_wait_for_busy_proof_locks(completed_execution, lock_kind):
    from concurrent.futures import ThreadPoolExecutor

    _, paths, root, group_path = completed_execution
    group = ShutdownGroup(group_path)
    lock = group.locked() if lock_kind == "group" else tw._exclusion(root.parent)
    with ThreadPoolExecutor(max_workers=1) as workers, lock:
        assert workers.submit(group.remove_terminal).result(timeout=1) is False
    assert paths.record.exists() and group_path.exists()


def test_opportunistic_gc_io_failure_does_not_prevent_native_command(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    paths = execution_paths("e" * 32, booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    _run_supervisor(
        state,
        paths.root.name,
        "pass",
        setup="def fail(*args): raise OSError('synthetic GC I/O failure')\nsupervisor.gc_terminal_executions = fail",
    )
    assert read_json(paths.record)["exit_code"] == 0


@pytest.mark.parametrize("condition", ["non-owner", "live-member", "busy-root"])
def test_owner_retirement_refuses_unproven_or_busy_proof(completed_execution, condition):
    _, paths, root, group_path = completed_execution
    group = ShutdownGroup(group_path)
    if condition != "non-owner":
        with group.locked() as data:
            data["owner"] = tw.process_identity(os.getpid())
            if condition == "live-member":
                member = next(iter(data["members"].values()))
                member["identity"] = data["owner"]
                member["ancestors"] = [data["owner"]]
    before = (root / tw._MARKER).read_bytes()
    lock = tw._exclusion(root.parent) if condition == "busy-root" else nullcontext()
    with lock:
        assert group.remove_terminal(owner=True) is False
    assert paths.record.exists() and group_path.exists()
    assert (root / tw._MARKER).read_bytes() == before


@pytest.mark.parametrize("lock_kind", ["scratch", "group"])
def test_actual_supervisor_publishes_terminal_result_with_busy_proof_lock(tmp_path, lock_kind):
    state, registry = tmp_path / "state", tmp_path / "scratch"
    state.mkdir()
    registry.mkdir()
    paths = execution_paths(ExecutionId("c" * 32), booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    ready, release, receipt = (tmp_path / name for name in ("ready", "release", "root"))
    process = _start_exit_after_native(state, paths.root.name, registry, ready, release, receipt)
    try:
        wait_for(
            ready.exists,
            failure="Native did not complete before terminal-lock probe",
            timeout_s=15,
        )
        root = Path(receipt.read_text())
        before = (root / tw._MARKER).read_bytes()
        group_path = next(paths.root.glob(".booley-shutdown-*"))
        lock = (
            tw._exclusion(registry)
            if lock_kind == "scratch"
            else ShutdownGroup(group_path).locked()
        )
        with lock:
            release.touch()
            stdout, stderr = process.communicate(timeout=3)
            assert process.returncode == 0, (stdout, stderr)
            assert read_json(paths.record)["tree_terminal"] is True
            assert (root / tw._MARKER).read_bytes() == before
            assert list(paths.root.glob(".booley-shutdown-*"))
        assert gc_terminal_executions(state) == []
        assert group_path.exists() is (lock_kind == "group")
        assert tw._delete_owned(root, require_dead=True)
    finally:
        release.touch()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_terminal_publication_times_out_on_busy_group_lock(completed_execution):
    from concurrent.futures import ThreadPoolExecutor

    _, paths, root, group_path = completed_execution
    group = ShutdownGroup(group_path)
    with group.locked() as data:
        data["owner"] = tw.process_identity(os.getpid())
    before = (group_path / "state.json").read_bytes(), (root / tw._MARKER).read_bytes()
    with ThreadPoolExecutor(max_workers=1) as workers, group.locked():
        assert workers.submit(group.finish, owner=True).result(timeout=2) is False
        assert before == (
            (group_path / "state.json").read_bytes(),
            (root / tw._MARKER).read_bytes(),
        )
    assert paths.record.exists() and group_path.exists()
    assert group.finish(owner=True)
    assert group.remove_terminal(owner=True)
    assert tw._delete_owned(root, require_dead=True)


def test_actual_guardian_returns_native_status_with_busy_scratch_lock(tmp_path):
    ready, release = tmp_path / "ready", tmp_path / "release"
    native = (
        "import sys,time\nfrom pathlib import Path\n"
        f"Path({str(ready)!r}).touch()\n"
        "deadline = time.monotonic() + 30\n"
        f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(.01)\n"
        "print('native complete')\nsys.exit(17)\n"
    )
    with tw.temporary_workspace(parent=tmp_path) as raw:
        root = Path(raw)
        process, launch = tw.guarded_popen(
            [sys.executable, "-c", native],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for(ready.exists, failure="Native did not become ready", timeout_s=15)
            before = (root / tw._MARKER).read_bytes()
            with tw._exclusion(tmp_path):
                release.touch()
                stdout, stderr = process.communicate(timeout=3)
                assert process.returncode == 17, (stdout, stderr)
                assert stdout.strip() == "native complete"
                assert (root / tw._MARKER).read_bytes() == before
        finally:
            release.touch()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        tw.launch_finished(launch)
    assert not root.exists()


def test_public_guarded_call_and_workspace_exit_defer_busy_registry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ready, release, receipt = (tmp_path / name for name in ("ready", "release", "root"))
    native = (
        "import sys,time\nfrom pathlib import Path\n"
        f"Path({str(ready)!r}).touch()\n"
        "deadline = time.monotonic() + 30\n"
        f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(.01)\n"
        "print('native complete')\nsys.exit(17)\n"
    )

    def invoke():
        with tw.temporary_workspace(parent=tmp_path) as raw:
            receipt.write_text(raw)
            result = tw.run_guarded(
                [sys.executable, "-c", native], capture_output=True, text=True, timeout=10
            )
        return result

    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(invoke)
        try:
            wait_for(ready.exists, failure="Native did not become ready", timeout_s=15)
            root = Path(receipt.read_text())
            before = (root / tw._MARKER).read_bytes()
            with tw._exclusion(tmp_path):
                release.touch()
                completed = result.result(timeout=3)
                assert completed.returncode == 17 and completed.stdout.strip() == "native complete"
                assert (root / tw._MARKER).read_bytes() == before
        finally:
            release.touch()
    assert tw._delete_owned(root, require_dead=False)


@pytest.mark.parametrize("operation", ["rollback", "normal-exit"])
def test_brief_contention_does_not_strand_proven_cleanup(tmp_path, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from booley.core import file_lock

    ready, proceed, contended = Event(), Event(), Event()
    acquire = file_lock.acquire_descriptor_lock
    roots = []

    def observe_contention(fd, **kwargs):
        try:
            acquire(fd, **kwargs)
        except file_lock.LockContentionError:
            contended.set()
            raise

    def invoke():
        with tw.temporary_workspace(parent=tmp_path) as raw:
            roots.append(Path(raw))
            launch = (
                tw.prepare_launch([sys.executable, "-c", "pass"])
                if operation == "rollback"
                else None
            )
            ready.set()
            assert proceed.wait(timeout=5)
            if launch is not None:
                tw.launch_failed(launch)

    monkeypatch.setattr(file_lock, "acquire_descriptor_lock", observe_contention)
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(invoke)
        assert ready.wait(timeout=5)
        with tw._exclusion(tmp_path):
            proceed.set()
            assert contended.wait(timeout=5)
        result.result(timeout=5)
    assert not roots[0].exists()


def test_group_lock_stall_does_not_cancel_healthy_native(tmp_path):
    ready, release, interrupted = (tmp_path / name for name in ("ready", "release", "interrupted"))
    native = (
        "import signal,sys,time\nfrom pathlib import Path\n"
        "def stopped(*_):\n"
        f"    Path({str(interrupted)!r}).touch()\n    sys.exit(99)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        f"Path({str(ready)!r}).touch()\n"
        "deadline = time.monotonic() + 30\n"
        f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(.01)\n"
        "print('healthy completion')\nsys.exit(17)\n"
    )
    with tw.temporary_workspace(parent=tmp_path) as raw:
        process, launch = tw.guarded_popen(
            [sys.executable, "-c", native],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for(ready.exists, failure="Native did not become ready", timeout_s=15)
            marker, _ = tw._validated_marker(Path(raw))
            group = ShutdownGroup(Path(marker["launches"][launch.token]["group"]["path"]))
            with group.locked():
                time.sleep(2)
                assert not interrupted.exists(), "a metadata waiter cancelled healthy native work"
            release.touch()
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 17 and stdout.strip() == "healthy completion", stderr
        finally:
            release.touch()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        tw.launch_finished(launch)
    assert not Path(raw).exists()


def test_scratch_proof_does_not_wait_for_a_group_lock(completed_execution):
    from concurrent.futures import ThreadPoolExecutor

    _, _, root, group_path = completed_execution
    before = (root / tw._MARKER).read_bytes()
    with ThreadPoolExecutor(max_workers=1) as workers, ShutdownGroup(group_path).locked():
        assert (
            workers.submit(tw._delete_owned, root, require_dead=True).result(timeout=0.5) is False
        )
        assert (root / tw._MARKER).read_bytes() == before
    assert tw._delete_owned(root, require_dead=True)


def test_observed_shutdown_deadline_survives_later_group_lock_timeout(tmp_path, monkeypatch):
    import signal

    from booley.core import lease_shutdown

    monkeypatch.setattr(lease_shutdown, "descendants", lambda _: set())
    group = ShutdownGroup.create(tmp_path)
    deadline = group.advance(signal.SIGINT)
    assert deadline is not None
    with group.locked():
        assert group.advance() == deadline


@pytest.mark.parametrize("failure", ["delivery", "publication"])
def test_first_stop_observation_keeps_deadline_and_signal_on_fault(tmp_path, monkeypatch, failure):
    import signal

    from booley.core import lease_shutdown

    group = ShutdownGroup.create(tmp_path)
    group.stop(signal.SIGINT)
    deadline = time.monotonic() + 2
    with group.locked() as data:
        next(iter(data["stops"].values()))["deadline"] = deadline
    identity = tw.process_identity(os.getpid())
    monkeypatch.setattr(lease_shutdown, "_identity", lambda pid: {**identity, "pid": pid})
    monkeypatch.setattr(lease_shutdown, "descendants", lambda _: {999999})
    signals = []

    def deliver(_identity, signum, **_kwargs):
        signals.append(signum)
        if failure == "delivery" and len(signals) == 1:
            raise PermissionError("synthetic first delivery failure")

    def fail_publication(*_args):
        raise OSError("synthetic first stop publication failure")

    monkeypatch.setattr(lease_shutdown, "signal_identity", deliver)
    if failure == "publication":
        monkeypatch.setattr(tw, "_publish_json", fail_publication)
    assert group.advance() == deadline
    assert signals == [signal.SIGINT, signal.SIGINT]


def _start_exit_after_native(state, execution_id, registry, ready, release, receipt):
    endpoint = (
        "import os,sys,time\nfrom pathlib import Path\n"
        "from booley.core import temporary_workspace as tw\n"
        f"with tw.temporary_workspace(parent=Path({str(registry)!r})) as raw:\n"
        f"    Path({str(receipt)!r}).write_text(raw)\n"
        "    tw.run_guarded([sys.executable, '-c', 'pass'], timeout=10)\n"
        f"    Path({str(ready)!r}).touch()\n"
        "    deadline = time.monotonic() + 30\n"
        f"    while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
        "        time.sleep(.01)\n"
        f"    assert Path({str(release)!r}).exists()\n"
        "    os._exit(0)\n"
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
            endpoint,
        ],
        env=runtime_env(state),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
