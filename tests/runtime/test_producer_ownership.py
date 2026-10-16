"""Full-lineage completion is an exact, immutable and fail-closed observation."""

import copy
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from booley.runtime.pid import capture_process_identity
from booley.runtime.producer_ownership import (
    completion_payload,
    new_ticket,
    observe_producer,
    ownership_directory,
    publish_handoff,
)


def test_completion_proof_requires_exact_token_root_and_namespace(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux child-subreaper and immutable lineage completion")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ticket = new_ticket("owned", runtime)
    identity = capture_process_identity(os.getpid()).to_payload()
    ticket.update(supervisor=identity, endpoint=identity, group=os.getpgrp(), capable=True)
    # The test process is not necessarily its group's leader; this is a decoded
    # handoff fixture, not authority to signal that group.
    ticket["group"] = identity["pid"]
    publish_handoff(ticket)
    proof = ownership_directory(ticket) / "complete.json"
    assert observe_producer(ticket, runtime, "owned").state == "live"
    proof.write_text(json.dumps(completion_payload(ticket)))
    assert observe_producer(ticket, runtime, "owned").state == "stopped"
    from booley.runtime.producer_supervisor import _publish_completion

    before = {p.name: p.read_bytes() for p in proof.parent.iterdir()}
    directory_fd = os.open(proof.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError, match="immutable"):
            _publish_completion(directory_fd, ticket)
    finally:
        os.close(directory_fd)
    assert {p.name: p.read_bytes() for p in proof.parent.iterdir()} == before
    for key, value in (
        ("token", "0" * 32),
        ("run_id", "different"),
        ("root_identity", [0, 0]),
        ("record_identity", [0, 0]),
        ("boot_id", "another-boot"),
        ("capable", False),
    ):
        corrupt = copy.deepcopy(ticket)
        corrupt[key] = value
        assert observe_producer(corrupt, runtime, "owned").state == "unavailable", key
    corrupt = copy.deepcopy(ticket)
    corrupt["owner"]["identity_scope"] = "pid:[different]"
    assert observe_producer(corrupt, runtime, "owned").state == "unavailable"
    for content in (b"bad", b"{}", b'{"all_reaped":true}'):
        proof.write_bytes(content)
        assert observe_producer(ticket, runtime, "owned").state == "unavailable"
    proof.unlink()
    assert observe_producer(ticket, runtime, "owned").state == "live"


@pytest.mark.asyncio
async def test_supervisor_completion_requires_all_adopted_children_reaped(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux child-subreaper and immutable lineage completion")
    import asyncio
    import time

    from booley.mcp.endpoint_process import _run_subprocess

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ticket = new_ticket("lineage", runtime, timeout_s=10)
    ready, release = tmp_path / "ready", tmp_path / "release"
    child = (
        "import time\nfrom pathlib import Path\n"
        f"Path({str(ready)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    script = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],env={{}},start_new_session=True,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "deadline=time.monotonic()+5\n"
        f"while not Path({str(ready)!r}).exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
    )

    @contextmanager
    def guard():
        yield ticket

    try:
        result = await _run_subprocess(
            [sys.executable, "-c", script],
            timeout=10,
            spawn_context=guard,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        )
        assert result[0] == 0 and ready.exists(), result
        assert observe_producer(ticket, runtime, "lineage").state == "live"
        assert not (ownership_directory(ticket) / "complete.json").exists()
    finally:
        release.touch()
        deadline = time.monotonic() + 10
        while observe_producer(ticket, runtime, "lineage").state == "live":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
    assert observe_producer(ticket, runtime, "lineage").state == "stopped"


@pytest.mark.asyncio
async def test_closed_gate_eof_and_invalid_handoff_never_execute_endpoint(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux child-subreaper and immutable lineage completion")
    import asyncio
    import socket

    from booley.runtime.pid import is_pid_alive

    for mode in ("eof", "wrong-token"):
        runtime = tmp_path / mode
        runtime.mkdir()
        ticket = new_ticket(mode, runtime)
        parent, child = socket.socketpair(type=socket.SOCK_SEQPACKET)
        parent.setblocking(False)
        executed = tmp_path / (mode + "-executed")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-m",
            "booley.runtime.producer_supervisor",
            str(child.fileno()),
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(executed)!r}).touch()",
            pass_fds=(child.fileno(),),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
            start_new_session=True,
        )
        child.close()
        ready = None
        try:
            loop = asyncio.get_running_loop()
            await loop.sock_sendall(parent, json.dumps(ticket).encode())
            raw = await asyncio.wait_for(loop.sock_recv(parent, 65536), 5)
            ready = json.loads(raw)["ready"]
            assert is_pid_alive(ready["endpoint"]["pid"]) and not executed.exists()
            if mode == "wrong-token":
                invalid = {**ready, "token": "0" * 32}
                await loop.sock_sendall(parent, json.dumps({"release": invalid}).encode())
            parent.close()  # Same kernel EOF as admission-owner death before release.
            assert await asyncio.wait_for(proc.wait(), 5) == 125
            assert not is_pid_alive(ready["endpoint"]["pid"])
            assert not executed.exists()
            assert observe_producer(ready, runtime, mode).state == "unavailable"
        finally:
            parent.close()
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), 5)


def test_ownership_inventory_never_suppresses_unreadable_or_orphaned_admission(
    tmp_path, monkeypatch
):
    if sys.platform != "linux":
        pytest.skip("Linux durable producer admission identity")
    from booley.runtime.producer_ownership import observe_producer_inventory

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    assert observe_producer_inventory(runtime) == []
    root = runtime / "producer-ownership"
    root.mkdir()
    assert observe_producer_inventory(runtime) == []
    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_junction", lambda path: path == root, raising=False)
        assert observe_producer_inventory(runtime)[0].state == "unavailable"
    ticket = new_ticket("orphan", runtime)
    assert observe_producer_inventory(runtime)[0].state == "live"
    assert not (runtime / "jobs").exists()  # No Job file is needed to retain ownership.
    source = ownership_directory(ticket) / "pending.json"
    saved = source.read_bytes()
    source.write_bytes(b"bad")
    assert observe_producer_inventory(runtime)[0].state == "unavailable"
    source.write_bytes(saved)
    original = Path.iterdir

    def unreadable(path):
        if path == root:
            raise PermissionError("ownership store cannot be enumerated")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "iterdir", unreadable)
        assert observe_producer_inventory(runtime)[0].state == "unavailable"
    moved = runtime / "moved"
    root.rename(moved)
    root.write_text("not a directory")
    assert observe_producer_inventory(runtime)[0].state == "unavailable"
    root.unlink()
    root.symlink_to(moved, target_is_directory=True)
    assert observe_producer_inventory(runtime)[0].state == "unavailable"
    root.unlink()
    moved.rename(root)
    assert observe_producer_inventory(runtime)[0].state == "live"


async def _run_owned_script(runtime, name, script, *, timeout_s):
    from booley.mcp.endpoint_process import _run_subprocess

    ticket = new_ticket(name, runtime, timeout_s=timeout_s)

    @contextmanager
    def guard():
        yield ticket

    result = await _run_subprocess(
        [sys.executable, "-c", script],
        timeout=10,
        spawn_context=guard,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    return ticket, result


@pytest.mark.asyncio
async def test_expired_lineage_wait_retires_only_own_group_without_completion(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux bounded producer group retirement")
    import asyncio
    import signal
    import time

    from booley.runtime.pid import (
        RUNNING,
        ProcessIdentity,
        capture_process_identity,
        observe_process,
    )

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ready, release = tmp_path / "ready", tmp_path / "release"
    child = (
        "import os,time,json\nfrom pathlib import Path\n"
        "from booley.runtime.pid import capture_process_identity\n"
        f"Path({str(ready)!r}).write_text(json.dumps(capture_process_identity(os.getpid()).to_payload()))\n"
        "deadline=time.monotonic()+10\n"
        f"while not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
    )
    script = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "deadline=time.monotonic()+5\n"
        f"while not Path({str(ready)!r}).exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
    )
    ticket, result = await _run_owned_script(runtime, "expired", script, timeout_s=1)
    supervisor = ProcessIdentity.from_payload(ticket["supervisor"])
    descendant = ProcessIdentity.from_payload(json.loads(ready.read_text()))
    try:
        assert result[0] == 0, result  # Endpoint result precedes lineage retirement.
        assert supervisor.pid == ticket["group"] and supervisor.pid != os.getpid()
        deadline = time.monotonic() + 5
        while observe_producer(ticket, runtime, "expired").state == "live":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        assert observe_producer(ticket, runtime, "expired").state == "unavailable"
        assert observe_process(supervisor).state != RUNNING
        assert observe_process(descendant).state != RUNNING
        assert not (ownership_directory(ticket) / "complete.json").exists()
    finally:
        release.touch()
        if (
            capture_process_identity(supervisor.pid) == supervisor
            and observe_process(supervisor).state == RUNNING
        ):
            os.killpg(ticket["group"], signal.SIGKILL)
        if (
            descendant
            and capture_process_identity(descendant.pid) == descendant
            and observe_process(descendant).state == RUNNING
        ):
            os.kill(descendant.pid, signal.SIGKILL)
