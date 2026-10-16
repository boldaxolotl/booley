"""Faults must preserve cancellation control and pre-spawn rollback proof."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.runtime.scratch_support import reap_guardian, wait

from booley.core import lease_shutdown as shutdown
from booley.core import temporary_workspace as tw

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux shutdown fault boundaries")


@pytest.mark.parametrize("failed_root", [0, 1])
def test_post_replace_fsync_failure_rolls_back_every_prepared_root(monkeypatch, failed_root):
    original = os.fsync
    fired = False
    with tw.temporary_workspace() as outer, tw.temporary_workspace() as inner:
        roots = [Path(outer), Path(inner)]

        def fail_after_replace(fd):
            nonlocal fired
            if not fired and stat.S_ISDIR(os.fstat(fd).st_mode):
                data, _ = tw._validated_marker(roots[failed_root])
                if any(value == {"state": "pending"} for value in data["launches"].values()):
                    fired = True
                    raise OSError("synthetic post-replace directory fsync failure")
            return original(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_after_replace)
            with pytest.raises(OSError, match="post-replace"):
                tw.prepare_launch([sys.executable, "-c", "pass"])
        assert fired
        assert all(tw._validated_marker(root)[0]["launches"] == {} for root in roots)
    assert all(not root.exists() for root in roots)


def test_signal_error_falls_back_and_continues_other_owned_descendants(tmp_path, monkeypatch):
    group = shutdown.ShutdownGroup.create(tmp_path)
    identity = tw.process_identity(os.getpid())
    signals = []
    monkeypatch.setattr(
        group, "tick", lambda: (_ for _ in ()).throw(PermissionError("signal denied"))
    )
    monkeypatch.setattr(shutdown, "descendants", lambda _: {102, 103})
    monkeypatch.setattr(shutdown, "_identity", lambda pid: {**identity, "pid": pid})

    def deliver(owner, signum, **_kwargs):
        if owner["pid"] == 102:
            raise PermissionError("synthetic different-credential descendant")
        signals.append((owner["pid"], signum))

    monkeypatch.setattr(shutdown, "signal_identity", deliver)
    deadline = group.advance(signal.SIGTERM)
    assert signals == [(103, signal.SIGTERM)]
    assert group.active()
    monkeypatch.setattr(shutdown.time, "monotonic", lambda: deadline + 1)
    assert group.advance() == deadline
    assert signals[-1] == (103, signal.SIGKILL)


def test_pidfd_binding_works_without_new_glibc_symbols(monkeypatch):
    identity = tw.process_identity(os.getpid())
    descriptors, calls = [], []

    def syscall(number, *args):
        calls.append(number.value)
        if number.value == shutdown._SYS_PIDFD_OPEN:
            fd = os.open(os.devnull, os.O_RDONLY)
            descriptors.append(fd)
            return fd
        return 0

    monkeypatch.setattr(
        shutdown.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(syscall=syscall)
    )
    monkeypatch.setattr(shutdown.platform, "machine", lambda: "x86_64")
    shutdown.signal_identity(identity, signal.SIGTERM)
    assert calls == [shutdown._SYS_PIDFD_OPEN, shutdown._SYS_PIDFD_SEND_SIGNAL]
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


_NATIVE = """import json,os,pathlib,subprocess,sys,time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(30)
"""


def _start_guardian(tmp_path):
    native = tmp_path / "native.py"
    native.write_text(_NATIVE)
    ready = tmp_path / "ready.json"
    process, launch = tw.guarded_popen([sys.executable, str(native), str(ready)])
    wait(ready.exists)
    return process, launch, json.loads(ready.read_text())


def test_corrupt_group_metadata_keeps_guardian_alive_to_reap_native_tree(tmp_path):
    with tw.temporary_workspace() as raw:
        process, launch, native_pids = _start_guardian(tmp_path)
        try:
            marker, _ = tw._validated_marker(Path(raw))
            group = Path(marker["launches"][launch.token]["group"]["path"])
            (group / "state.json").write_text("{synthetic malformed state")
            process.wait(timeout=8)
            assert all(not Path(f"/proc/{pid}").exists() for pid in native_pids)
            assert tw._validated_marker(Path(raw))[0]["launches"][launch.token] == {
                "state": "complete"
            }
        finally:
            if process.poll() is None:
                tw.stop_guarded_process(process)
            process.wait(timeout=2)
    assert not Path(raw).exists()


def test_stopped_guardian_gets_bounded_force_with_its_native_descendants(
    tmp_path, monkeypatch, linux_subreaper
):
    with tw.temporary_workspace() as raw:
        process, _launch, native_pids = _start_guardian(tmp_path)
        wait_process = process.wait
        monkeypatch.setattr(
            process, "wait", lambda timeout=None: wait_process(timeout=min(timeout, 0.2))
        )
        try:
            process.send_signal(signal.SIGSTOP)
            assert tw.stop_guarded_process(process)
            assert process.returncode == -signal.SIGKILL
            for pid in native_pids:
                reap_guardian(pid)
        finally:
            if process.poll() is None:
                process.kill()
            wait_process(timeout=2)
    assert Path(raw).exists(), "a force-killed guardian cannot authorize payload deletion"


def test_cancelling_multiple_guardians_starts_their_cleanup_concurrently(tmp_path, monkeypatch):
    from booley.runtime import supervised_execution

    barrier = threading.Barrier(2)
    stopped = []

    def stop(process):
        barrier.wait(timeout=2)
        stopped.append(process)

    monkeypatch.setattr(supervised_execution, "kill_process_tree", stop)
    processes = supervised_execution.SupervisedProcessSet(None, tmp_path, lambda: False)
    children = [object(), object()]
    for child in children:
        processes.register(child)
    processes.terminate_all()
    assert set(stopped) == set(children)


def test_second_communicate_timeout_still_stops_the_guardian(monkeypatch):
    stopped = []
    calls = 0

    def communicate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise subprocess.TimeoutExpired("native", 1)
        return "output", "error"

    process = SimpleNamespace(communicate=communicate)
    monkeypatch.setattr(tw, "launch_result", lambda _: 0)
    monkeypatch.setattr(tw, "stop_guarded_process", stopped.append)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        tw._communicate_supervised(process, object(), ["native"], None, 1)
    assert error.value.output == "output"
    assert stopped == [process]


def test_corrupt_group_metadata_does_not_strand_outer_execution_record(tmp_path):
    from tests.execution_test_support import start_supervisor_with_detached_descendant

    from booley.runtime.execution_records import (
        ExecutionId,
        execution_paths,
        read_json,
        request_cancellation,
        write_attachment_heartbeat,
    )

    state = tmp_path / "state"
    state.mkdir()
    execution_id = ExecutionId("c" * 32)
    paths = execution_paths(execution_id, booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    ready = tmp_path / "detached.pid"
    process = start_supervisor_with_detached_descendant(state, execution_id, ready)
    try:
        wait(ready.exists)
        group = next(paths.root.glob(".booley-shutdown-*"))
        (group / "state.json").write_text("{synthetic malformed shared state")
        request_cancellation(paths, signum=signal.SIGTERM)
        assert process.wait(timeout=8) == 143
        record = read_json(paths.record)
        assert record["state"] == "terminal"
        assert record["tree_terminal"] is True
        assert not Path(f"/proc/{ready.read_text()}").exists()
    finally:
        if process.poll() is None:
            request_cancellation(paths, force=True)
        process.wait(timeout=8)


@pytest.mark.parametrize("reuse", ["before", "during"])
def test_guardian_pid_reuse_cannot_authorize_force(monkeypatch, reuse):
    observed, enumerated, signals = [], [], []
    original = tw.process_identity
    expected = {**original(os.getpid()), "pid": 313131, "start": 1}
    replacement = {**expected, "start": 2}
    with tw.temporary_workspace() as raw:
        launch = tw.prepare_launch([sys.executable, "-c", "pass"])
        marker, _ = tw._validated_marker(Path(raw))
        marker["launches"][launch.token] = {"state": "running", "supervisor": expected}
        tw._write_marker(Path(raw), marker)

        def identity(pid):
            if pid != expected["pid"]:
                return original(pid)
            observed.append(pid)
            return expected if reuse == "during" and len(observed) == 1 else replacement

        with monkeypatch.context() as patch:
            patch.setattr(tw, "process_identity", identity)
            patch.setattr(shutdown, "descendants", lambda pid: enumerated.append(pid) or {414141})
            patch.setattr(
                shutdown, "signal_identity", lambda *args, **kwargs: signals.append(args)
            )
            tw.force_guarded_descendants(expected["pid"], launch)
        assert not signals
        assert len(enumerated) == (reuse == "during")
        tw._clear_launch(launch.roots, launch.token, pending_only=False)
    assert not Path(raw).exists()


def test_a_reused_descendant_pid_must_still_have_the_owned_ancestor(monkeypatch):
    target = tw.process_identity(os.getpid())
    unrelated_ancestor = {**target, "pid": 313131}
    descriptors, delivered = [], []

    def pidfd(_libc, name, _number, *args):
        if name == "pidfd_open":
            fd = os.open(os.devnull, os.O_RDONLY)
            descriptors.append(fd)
            return fd
        delivered.append(args)
        return 0

    monkeypatch.setattr(shutdown, "_pidfd_call", pidfd)
    shutdown.signal_identity(target, signal.SIGKILL, ancestor=unrelated_ancestor)
    assert not delivered
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_reparented_process_cannot_join_a_reused_parent_identity(monkeypatch):
    child, parent = {"pid": 10, "start": 1}, {"pid": 20, "start": 2}
    snapshots = iter(["10 (synthetic) S 20", "10 (synthetic) S 30"])
    monkeypatch.setattr(
        shutdown, "Path", lambda _: SimpleNamespace(read_text=lambda: next(snapshots))
    )
    monkeypatch.setattr(shutdown, "_identity", lambda pid: child if pid == 10 else parent)
    assert shutdown._parent_identity(child) is None


def test_ancestry_depth_failure_skips_signal_and_closes_pidfd(monkeypatch):
    target = tw.process_identity(os.getpid())
    descriptors, delivered = [], []

    def pidfd(_libc, name, _number, *args):
        if name == "pidfd_open":
            fd = os.open(os.devnull, os.O_RDONLY)
            descriptors.append(fd)
            return fd
        delivered.append(args)
        return 0

    def too_deep(_):
        raise RuntimeError("synthetic ancestry depth exceeded")

    monkeypatch.setattr(shutdown, "_pidfd_call", pidfd)
    monkeypatch.setattr(shutdown, "ancestors", too_deep)
    shutdown.signal_identity(target, signal.SIGTERM, ancestor=target)
    assert not delivered
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize("inherited", ["missing", "malformed", "terminal"])
def test_rejected_inherited_group_clears_proven_unstarted_handoff(
    tmp_path, monkeypatch, inherited
):
    group = shutdown.ShutdownGroup.create(tmp_path)
    if inherited == "terminal":
        group.complete(owner=True)
    elif inherited == "malformed":
        (group.path / "state.json").write_text("{synthetic malformed state")
    path = tmp_path / "missing" if inherited == "missing" else group.path
    monkeypatch.setenv(shutdown.ENV, str(path))
    started = tmp_path / "native-started"
    with tw.temporary_workspace() as raw:
        process, launch = tw.guarded_popen(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(started)!r}).touch()"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        process.communicate(timeout=5)
        assert process.returncode != 0
        assert not started.exists()
        assert tw._validated_marker(Path(raw))[0]["launches"] == {}
        tw.launch_finished(launch)
    assert not Path(raw).exists()
