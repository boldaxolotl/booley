"""Crash boundaries that were absent from the first #1537 implementation."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from tests.runtime.scratch_support import scratch_process_ownership_supported

from booley.core import temporary_workspace as tw

pytestmark = pytest.mark.skipif(
    not scratch_process_ownership_supported(), reason="Linux scratch identity"
)


def test_marker_publication_failure_preserves_previous_complete_proof(monkeypatch):
    with tw.temporary_workspace() as raw:
        root = Path(raw)
        data, _ = tw._validated_marker(root)
        original = (root / tw._MARKER).read_bytes()
        replace = Path.replace

        def fail_publication(source, destination):
            if destination == root / tw._MARKER:
                raise OSError("synthetic publication interruption")
            return replace(source, destination)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", fail_publication)
            with pytest.raises(OSError, match="publication interruption"):
                tw._write_marker(root, {**data, "launches": {"pending": {"state": "pending"}}})
        assert (root / tw._MARKER).read_bytes() == original
        assert not list(root.glob(".booley-publish-*"))
    assert not root.exists()


def test_real_directory_replacement_after_claim_is_never_traversed(monkeypatch):
    remove = tw._remove_claimed
    moved = []

    def replace_after_validation(claimed, data):
        original = claimed.with_name("owned-original")
        claimed.rename(original)
        claimed.mkdir(mode=0o700)
        (claimed / "foreign-sentinel").write_text("foreign")
        moved.extend((original, claimed))
        return remove(claimed, data)

    with monkeypatch.context() as patch:
        patch.setattr(tw, "_remove_claimed", replace_after_validation)
        with tw.temporary_workspace() as raw:
            (Path(raw) / "owned-sentinel").write_text("owned")
    original, foreign = moved
    assert (original / "owned-sentinel").read_text() == "owned"
    assert (foreign / "foreign-sentinel").read_text() == "foreign"


def test_interrupted_payload_deletion_keeps_recovery_proof(monkeypatch):
    remove = tw.shutil.rmtree

    def interrupt_payload(path, **kwargs):
        if kwargs.get("dir_fd") is not None:
            raise OSError("synthetic deleter death")
        return remove(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(tw.shutil, "rmtree", interrupt_payload)
        interrupt_payload.avoids_symlink_attacks = True
        with tw.temporary_workspace() as raw:
            (Path(raw) / "payload").mkdir()
    roots = list(Path(raw).parent.glob(".booley-reaping-*"))
    assert len(roots) == 1
    data, _ = tw._validated_marker(roots[0])
    assert data["launches"] == {}
    assert tw._delete_owned(roots[0], require_dead=False)
    assert not roots[0].exists()


@pytest.mark.asyncio
async def test_cancelled_async_entry_is_joined_and_releases_its_context(monkeypatch):
    from booley.core.workspace_async import async_workspace

    entered, release = threading.Event(), threading.Event()
    allocate = tw._allocate

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return allocate(*args)

    async def operation():
        async with async_workspace():
            raise AssertionError("cancelled entry reached its body")

    monkeypatch.setattr(tw, "_allocate", blocked)
    task = asyncio.create_task(operation())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert tw.active_guard_fds() == ()
    assert not list(tw.registry_root().glob("booley-owned-*"))


_LEAF = """import os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
name = sys.argv[2]
def flush(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    started = time.monotonic()
    time.sleep(9.1)
    (root / (name + '.flush')).write_text(str(time.monotonic() - started))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, flush)
if name == 'native':
    subprocess.Popen([sys.executable, __file__, str(root), 'detached'], start_new_session=True)
(root / (name + '.ready')).write_text(str(os.getpid()))
while True:
    time.sleep(.02)
"""

_INNER = """import pathlib, subprocess, sys
from booley.core.temporary_workspace import temporary_workspace, prepare_launch, launch_finished
root = pathlib.Path(sys.argv[1])
with temporary_workspace() as raw:
    (root / 'inner-root').write_text(raw)
    launch = prepare_launch([sys.executable, str(root / 'leaf.py'), str(root), 'native'])
    launch.command[1] = str(root / 'delayed.py')
    process = subprocess.Popen(launch.command, **launch.kwargs)
    try:
        process.wait(timeout=40)
    finally:
        launch_finished(launch)
"""

_DELAYED = """import sys, time
from booley.core import lease_exec
from booley.core.lease_shutdown import ShutdownGroup
original = ShutdownGroup.tick
delayed = False
def tick(self):
    global delayed
    deadline = original(self)
    if deadline is not None and not delayed:
        delayed = True
        time.sleep(3.2)
    return deadline
ShutdownGroup.tick = tick
raise SystemExit(lease_exec.supervise(sys.argv[1], sys.argv[2:]))
"""

_OUTER = """import os, pathlib, sys
os.setsid()
from booley.core.temporary_workspace import temporary_workspace, run_guarded
root = pathlib.Path(sys.argv[1])
with temporary_workspace() as raw:
    (root / 'outer-root').write_text(raw)
    run_guarded([sys.executable, str(root / 'inner.py'), str(root)], timeout=40)
"""


def _start_nested_supervision(tmp_path):
    from tests.execution_test_support import runtime_env

    from booley.runtime.execution_records import (
        ExecutionId,
        execution_paths,
        write_attachment_heartbeat,
    )

    state = tmp_path / "state"
    state.mkdir()
    paths = execution_paths(ExecutionId("b" * 32), booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    for name, source in (
        ("leaf", _LEAF),
        ("inner", _INNER),
        ("delayed", _DELAYED),
        ("outer", _OUTER),
    ):
        (tmp_path / (name + ".py")).write_text(source)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            "b" * 32,
            "--attachment-timeout-seconds",
            "60",
            "--",
            sys.executable,
            str(tmp_path / "outer.py"),
            str(tmp_path),
        ],
        env=runtime_env(state),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, paths


def test_actual_nested_supervisors_share_deadline_and_preserve_full_native_flush(tmp_path):
    from tests.execution_test_support import wait_for

    from booley.runtime.execution_records import read_json, request_cancellation

    process, paths = _start_nested_supervision(tmp_path)
    try:
        wait_for((tmp_path / "detached.ready").exists, failure="native tree not ready")
        request_cancellation(paths, signum=signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=25)
        assert process.returncode == 143, (stdout, stderr)
        record = read_json(paths.record)
        assert record["state"] == "terminal", record
        assert record["tree_terminal"] is True
        for name in ("native", "detached"):
            assert float((tmp_path / (name + ".flush")).read_text()) >= 9.1
        for name in ("inner", "outer"):
            root = Path((tmp_path / (name + "-root")).read_text())
            tw.sweep_stale_workspaces(parent=root.parent)
            assert not root.exists(), stderr
    finally:
        if process.poll() is None:
            request_cancellation(paths, force=True)
            process.communicate(timeout=10)


@pytest.mark.asyncio
async def test_native_success_before_deadline_survives_slower_descendant_cleanup(tmp_path):
    from booley.core.models import AgentCallParams
    from booley.runtime._codex_backend import _codex_run_subprocess

    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib,signal,sys,time\n"
        "ready=pathlib.Path(sys.argv[1])\n"
        "def finish(signum, frame):\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(1.2)\n"
        "    ready.with_suffix('.flushed').touch()\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, finish)\n"
        "ready.touch()\n"
        "time.sleep(20)\n"
    )
    native = tmp_path / "native.py"
    native.write_text(
        "import pathlib,subprocess,sys,time\n"
        "sys.stdin.read()\n"
        "ready=pathlib.Path(sys.argv[1])\n"
        "subprocess.Popen([sys.executable, sys.argv[2], str(ready)], start_new_session=True)\n"
        "deadline=time.monotonic()+5\n"
        "while not ready.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        "print('native success',flush=True)\n"
    )
    ready = tmp_path / "ready"
    output, _, status = await _codex_run_subprocess(
        [sys.executable, str(native), str(ready), str(child)],
        AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path, timeout_seconds=1),
        full_prompt="synthetic",
        on_event=None,
    )
    assert status == 0
    assert "native success" in output
    assert ready.with_suffix(".flushed").exists()


@pytest.mark.asyncio
async def test_stdio_startup_remains_responsive_and_joins_its_cleanup(monkeypatch):
    from booley.mcp import server

    entered, release = threading.Event(), threading.Event()

    def cleanup():
        entered.set()
        assert release.wait(5)

    async def serve():
        assert await asyncio.to_thread(entered.wait, 5)
        # Serving initialization is possible while filesystem cleanup is blocked.
        await asyncio.sleep(0)
        release.set()

    monkeypatch.setattr(server, "_sweep_startup_workspaces", cleanup)
    monkeypatch.setattr(server, "_serve_stdio", serve)
    await asyncio.wait_for(server._main(), 5)
    assert release.is_set()


@pytest.mark.timeout(120)  # Sixty-five real guardians must also finish under coverage.
def test_actual_supervisor_prunes_completed_registrations_before_65th_launch(tmp_path):
    from tests.execution_test_support import runtime_env

    from booley.runtime.execution_records import (
        ExecutionId,
        execution_paths,
        read_json,
        write_attachment_heartbeat,
    )

    state = tmp_path / "state"
    state.mkdir()
    paths = execution_paths(ExecutionId("c" * 32), booley_dir=state)
    write_attachment_heartbeat(paths, generation=1)
    source = (
        "from booley.core.temporary_workspace import temporary_workspace,run_guarded\n"
        "with temporary_workspace():\n"
        "    for index in range(65):\n"
        "        result=run_guarded(['true'],timeout=5)\n"
        "        assert result.returncode==0\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            "c" * 32,
            "--attachment-timeout-seconds",
            "120",
            "--",
            sys.executable,
            "-c",
            source,
        ],
        env=runtime_env(state),
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert read_json(paths.record)["tree_terminal"] is True


@pytest.mark.parametrize("force", [False, True])
def test_endpoint_group_sigkill_or_explicit_force_settles_registered_tree(tmp_path, force):
    from tests.execution_test_support import wait_for

    from booley.runtime.execution_records import read_json, request_cancellation

    process, paths = _start_nested_supervision(tmp_path)
    try:
        wait_for((tmp_path / "detached.ready").exists, failure="native tree not ready")
        if force:
            request_cancellation(paths, force=True, signum=signal.SIGTERM)
        else:
            leader = read_json(paths.record)["leader"]["pid"]
            assert os.getpgid(leader) == leader
            os.killpg(leader, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=25)
        assert process.returncode == (143 if force else 137), (stdout, stderr)
        assert read_json(paths.record)["tree_terminal"] is True
        assert (tmp_path / "native.flush").exists() is not force
        assert (tmp_path / "detached.flush").exists() is not force
        if force:
            import shutil

            shutil.rmtree(paths.record.parent)
        for name in ("inner", "outer"):
            root = Path((tmp_path / (name + "-root")).read_text())
            tw.sweep_stale_workspaces(parent=root.parent)
            assert not root.exists(), stderr
    finally:
        if process.poll() is None:
            request_cancellation(paths, force=True)
            process.communicate(timeout=10)


def _completed_group_with_unfinished_marker(root):
    from booley.core.lease_shutdown import ShutdownGroup

    launch = tw.prepare_launch([sys.executable, "-c", "pass"])
    group = ShutdownGroup.create(root)
    group.register(launch.roots, launch.token)
    descriptors = tw.register_launch(launch.roots, launch.token, group=group)
    for fd in descriptors:
        os.close(fd)
    with group.locked() as data:
        data["terminal"] = True
    return group


def test_interrupted_deletion_survives_removal_of_shutdown_proof(monkeypatch):
    remove = tw.shutil.rmtree
    group = None

    def interrupt_after_group(path, **kwargs):
        remove(path, **kwargs)
        if group is not None and path == group.path.name and kwargs.get("dir_fd") is not None:
            raise OSError("synthetic death after shutdown proof removal")

    with monkeypatch.context() as patch:
        patch.setattr(tw, "owner_alive", lambda _: False)
        patch.setattr(tw.shutil, "rmtree", interrupt_after_group)
        interrupt_after_group.avoids_symlink_attacks = True
        with tw.temporary_workspace() as raw:
            group = _completed_group_with_unfinished_marker(Path(raw))
    claimed = list(Path(raw).parent.glob(".booley-reaping-*"))
    assert len(claimed) == 1
    marker, _ = tw._validated_marker(claimed[0])
    assert marker["launches"] == {}
    assert not (claimed[0] / group.path.name).exists()
    assert tw._delete_owned(claimed[0], require_dead=False)
    assert not claimed[0].exists()
