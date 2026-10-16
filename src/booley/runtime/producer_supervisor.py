"""Private bound endpoint gate and Linux adopted-child completion witness."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import select
import signal
import socket
import sys
import time
from typing import Any

from booley.runtime.pid import RUNNING, ProcessIdentity, capture_process_identity, observe_process
from booley.runtime.producer_ownership import (
    completion_payload,
    ownership_directory,
    require_handoff,
)


def _send(control: socket.socket, value: dict[str, Any]) -> None:
    control.sendall(json.dumps(value).encode())


def _receive(control: socket.socket, seconds: float) -> dict[str, Any]:
    control.settimeout(seconds)
    value = json.loads(control.recv(65536))
    if not isinstance(value, dict):
        raise ValueError("invalid producer control message")
    return value


def _subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    library = ctypes.CDLL(None, use_errno=True)
    return library.prctl(36, 1, 0, 0, 0) == 0


def _parked_child(read_gate: int, write_gate: int, control: socket.socket, cmd: list[str]) -> None:
    control.close()
    os.close(write_gate)
    try:
        ready, _, _ = select.select([read_gate], [], [], 10)
        if not ready or os.read(read_gate, 1) != b"1":
            os._exit(125)
        os.close(read_gate)
        os.execvpe(cmd[0], cmd, os.environ)
    except OSError as exc:
        message = f"Subprocess launch failed for {cmd[0]!r}: {exc}\n"
        os.write(2, message.encode("utf-8", errors="replace")[:8192])
        os._exit(2)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        os._exit(126)


def _publish_completion(directory_fd: int, ticket: dict[str, Any]) -> None:
    try:
        os.stat("complete.json", dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("producer completion is immutable")
    data = json.dumps(completion_payload(ticket), sort_keys=True).encode()
    fd = os.open("complete.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # RENAME_NOREPLACE is atomic and cannot replace a previous immutable proof.
    # This publication is the final artifact mutation; no later cleanup can race prune.
    if rename(directory_fd, b"complete.tmp", directory_fd, b"complete.json", 1) != 0:
        raise OSError(ctypes.get_errno(), "cannot publish immutable producer completion")
    os.fsync(directory_fd)


def _reap(control: socket.socket, endpoint: int, budget: float, owner: ProcessIdentity) -> bool:
    """Report endpoint exit independently; witness ECHILD only after every reap."""
    endpoint_done = False
    deadline = None
    while deadline is None or time.monotonic() < deadline:
        # A live admission owner enforces queue-aware endpoint budgets. Once it
        # disappears, this witness has its own finite remaining supervision budget.
        if deadline is None and observe_process(owner).state != RUNNING:
            deadline = time.monotonic() + budget
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return endpoint_done
        if pid == endpoint:
            endpoint_done = True
            deadline = time.monotonic() + budget
            with contextlib.suppress(OSError):
                _send(control, {"exit_code": os.waitstatus_to_exitcode(status)})
        if pid == 0:
            # A finite poll never holds any owner's lock or depends on inherited FDs.
            time.sleep(0.01)
    return False


def _launch(control: socket.socket, pending: dict[str, Any], cmd: list[str]) -> None:
    directory = ownership_directory(pending)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.getpgrp() != os.getpid():
        raise ValueError("producer supervisor requires its own captured process group")
    capable = _subreaper()
    read_gate, write_gate = os.pipe()
    endpoint = os.fork()
    if endpoint == 0:
        _parked_child(read_gate, write_gate, control, cmd)
        os._exit(126)
    os.close(read_gate)
    try:
        try:
            ticket = _handoff(control, pending, endpoint, capable)
            os.write(write_gate, b"1")
        finally:
            os.close(write_gate)
    except (OSError, ValueError, TypeError, KeyError):
        _reap_closed_gate(endpoint)
        raise
    # Only the endpoint lineage owns the output pipes now.
    os.close(1)
    os.close(2)
    owner = ProcessIdentity.from_payload(ticket["owner"])
    if owner is None:
        os._exit(125)
    _complete_lineage(control, endpoint, ticket, owner, capable, directory_fd)


def _complete_lineage(
    control: socket.socket,
    endpoint: int,
    ticket: dict[str, Any],
    owner: ProcessIdentity,
    capable: bool,
    directory_fd: int,
) -> None:
    complete = _reap(control, endpoint, float(ticket["timeout_s"]), owner)
    if not complete:
        # This process is still its own validated session/group leader. Force
        # only that group; escaped descendants keep the missing proof unknown.
        os.killpg(os.getpgrp(), signal.SIGKILL)
        os._exit(125)
    if capable:
        _publish_completion(directory_fd, ticket)
    os._exit(0)


def _reap_closed_gate(endpoint: int) -> None:
    """A parked child has no descendants; EOF closes its gate before reaping."""
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        pid, _status = os.waitpid(endpoint, os.WNOHANG)
        if pid == endpoint:
            return
        time.sleep(0.01)
    os.kill(endpoint, signal.SIGKILL)
    os.waitpid(endpoint, 0)


def _handoff(
    control: socket.socket, pending: dict[str, Any], endpoint: int, capable: bool
) -> dict[str, Any]:
    supervisor_identity = capture_process_identity(os.getpid())
    endpoint_identity = capture_process_identity(endpoint)
    ticket = {
        **pending,
        "supervisor": None if supervisor_identity is None else supervisor_identity.to_payload(),
        "endpoint": None if endpoint_identity is None else endpoint_identity.to_payload(),
        "group": os.getpgrp(),
        "capable": capable,
    }
    _send(control, {"ready": ticket})
    if _receive(control, 10) != {"release": ticket}:
        raise ValueError("producer gate handoff differs from readiness")
    require_handoff(ticket)
    return ticket


def main() -> None:
    """Execute the private inherited control socket protocol, never a public CLI."""
    try:
        control = socket.socket(fileno=int(sys.argv[1]))
        pending = _receive(control, 10)
        _launch(control, pending, sys.argv[2:])
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        # No Python shutdown hooks or late artifact writes after a proof or failure.
        os._exit(125)


if __name__ == "__main__":
    main()
