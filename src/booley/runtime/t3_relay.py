"""Host-loopback relay into the Sandbox's T3 Code server.

``start`` spawns a detached process (``python -m booley.runtime.t3_relay``) that listens
on ``127.0.0.1`` and splices each connection into the Sandbox through ``docker exec``
stdio. A per-Project record keeps its port stable across ``stop`` and ``start``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import BoundaryError, require_int, require_str
from booley.core.private_store import PrivateStore, ProjectStore
from booley.core.process_identity import ProcessIdentity
from booley.core.user_paths import config_dir
from booley.runtime.incontainer_setup import T3_PORT
from booley.runtime.pid import ProcessState, capture_process_identity, observe_process
from booley.runtime.process_group import (
    ProcessGroup,
    new_group_kwargs,
    terminate_adopted_process_group,
)

PORTS = range(3780, 3880)
_START_TIMEOUT_S = 30.0
_CHUNK = 65536

# Runs in the Sandbox: one TCP connection to T3 Code, spliced onto stdin and stdout.
_SPLICE = """
import socket, sys, threading
sock = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
def send():
    try:
        while data := sys.stdin.buffer.read1(65536):
            sock.sendall(data)
    finally:
        sock.shutdown(socket.SHUT_WR)
threading.Thread(target=send, daemon=True).start()
while data := sock.recv(65536):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
"""


class T3RelayError(RuntimeError):
    """The T3 Code relay cannot start, or its record is unusable."""


@dataclass(frozen=True, slots=True)
class RelayStart:
    """The relay's port, and the port it left when that port was held."""

    port: int
    moved_from: int | None


@dataclass(frozen=True, slots=True)
class _Record:
    port: int
    container: str
    identity: ProcessIdentity | None


def relay_store() -> ProjectStore:
    """The per-Project relay records."""
    private = PrivateStore(
        config_dir() / "runtime" / "t3-relays",
        config_dir().parent,
        "T3 Code relay record",
        T3RelayError,
    )
    return ProjectStore(private, _repair)


def _repair(path: Path, _root: str) -> str:
    return f"remove {path}, then run `booley session up` again (the Project may get a new port)"


def _parse(path: Path, document: dict[str, object]) -> _Record:
    try:
        port = require_int(document.get("port"), field="port")
        container = require_str(document, "container")
        raw = document.get("process")
        identity = None if raw is None else ProcessIdentity.from_payload(raw)
        if raw is not None and identity is None:
            raise BoundaryError("process is not a process identity")
        if port not in PORTS:
            raise BoundaryError(f"port {port} is outside {PORTS.start}-{PORTS[-1]}")
    except BoundaryError as exc:
        raise T3RelayError(
            f"T3 Code relay record {path} is invalid: {exc}; {_repair(path, '')}"
        ) from exc
    return _Record(port, container, identity)


def _read(store: ProjectStore, root: str) -> _Record | None:
    document = store.read(root)
    return None if document is None else _parse(store.path(root), document)


def _write(store: ProjectStore, root: str, record: _Record) -> None:
    process = None if record.identity is None else record.identity.to_payload()
    document = {
        "project_root": root,
        "port": record.port,
        "container": record.container,
        "process": process,
    }
    store.write(root, json.dumps(document, indent=2) + "\n")


def _terminate(pid: int) -> None:
    asyncio.run(terminate_adopted_process_group(ProcessGroup(pid)))


def _candidates(recorded: int | None, others: set[int]) -> list[str]:
    """Order the range: the recorded port onward, other Projects' recorded ports last."""
    ports = list(PORTS)
    if recorded is not None:
        ports = ports[ports.index(recorded) :] + ports[: ports.index(recorded)]
    ordered = [p for p in ports if p not in others] + [p for p in ports if p in others]
    return [str(port) for port in ordered]


def start(workspace: Path, container: str) -> RelayStart:
    """Ensure this Project's relay into *container* runs; return its port."""
    store = relay_store()
    root = str(workspace.resolve())
    record = _read(store, root)
    if record is not None and record.identity is not None:
        state = observe_process(record.identity).state
        if state is ProcessState.UNKNOWN:
            raise T3RelayError(
                f"cannot tell whether the T3 Code relay (process {record.identity.pid}) "
                "still runs; run `booley session up` again"
            )
        if state is ProcessState.RUNNING and record.container == container:
            return RelayStart(record.port, None)
        if state is ProcessState.RUNNING:
            _terminate(record.identity.pid)
    recorded = None if record is None else record.port
    others = {
        _parse(entry.path, entry.document).port
        for entry in store.scan()
        if entry.project_root != root
    }
    port, identity = _spawn(container, _candidates(recorded, others))
    try:
        _write(store, root, _Record(port, container, identity))
    except BaseException:
        _terminate(identity.pid)
        raise
    moved = recorded if recorded is not None and recorded != port else None
    return RelayStart(port, moved)


def stop(workspace: Path) -> bool:
    """Stop this Project's relay, keeping its port on record; return whether one ran."""
    store = relay_store()
    root = str(workspace.resolve())
    record = _read(store, root)
    if record is None or record.identity is None:
        return False
    state = observe_process(record.identity).state
    if state is ProcessState.UNKNOWN:
        return False
    if state is ProcessState.RUNNING:
        _terminate(record.identity.pid)
    _write(store, root, _Record(record.port, record.container, None))
    return state is ProcessState.RUNNING


def _spawn(container: str, candidates: list[str]) -> tuple[int, ProcessIdentity]:
    """Start the relay process; return the port it bound and its identity."""
    kwargs = new_group_kwargs()
    if sys.platform == "win32":  # pragma: no cover - the Windows console detaches only there
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    command = [sys.executable, "-m", "booley.runtime.t3_relay", container, str(T3_PORT)]
    # stderr goes nowhere: the relay carries T3 Code's HTTP traffic, pairing tokens included.
    process = subprocess.Popen(
        [*command, *candidates],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        **kwargs,
    )
    # Reaps the relay when it ends, so a stopped relay is neither a zombie nor a leaked Popen.
    threading.Thread(target=process.wait, daemon=True).start()
    stdout = process.stdout
    assert stdout is not None
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.append(stdout.readline()), daemon=True)
    reader.start()
    reader.join(_START_TIMEOUT_S)
    identity = (
        None
        if reader.is_alive() or not lines[0].strip()
        else capture_process_identity(process.pid)
    )
    if identity is None:
        if process.poll() is None:
            _terminate(process.pid)
        first, last = PORTS[0], PORTS[-1]
        raise T3RelayError(
            f"the T3 Code relay did not bind a port in 127.0.0.1:{first}-{last}; stop a "
            "process holding one, then run `booley session up` again"
        )
    stdout.close()
    return int(lines[0]), identity


async def _pump_in(reader: asyncio.StreamReader, sink: asyncio.subprocess.Process) -> None:
    assert sink.stdin is not None
    while data := await reader.read(_CHUNK):
        sink.stdin.write(data)
        await sink.stdin.drain()
    sink.stdin.close()  # half-close: the splice shuts the write side towards T3 Code


async def _handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, container: str, target: int
) -> None:
    """Splice one client connection into the Sandbox through ``docker exec`` stdio."""
    process = await asyncio.create_subprocess_exec(
        *("docker", "exec", "-i", container, "python3", "-I", "-c", _SPLICE, str(target)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None
    inbound = asyncio.ensure_future(_pump_in(reader, process))
    try:
        while data := await process.stdout.read(_CHUNK):
            writer.write(data)
            await writer.drain()
    finally:
        inbound.cancel()
        writer.close()
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def _serve(container: str, target: int, candidates: list[int]) -> int:
    """Listen on the first free candidate port, report it on stdout, and serve forever."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(reader, writer, container, target)

    for port in candidates:
        try:
            server = await asyncio.start_server(handle, "127.0.0.1", port)
        except OSError:
            continue
        sys.stdout.write(f"{port}\n")
        sys.stdout.flush()
        async with server:
            await server.serve_forever()
    return 1


def main(argv: list[str]) -> int:
    """Entry of the relay process: ``<container> <target port> <candidate port>...``."""
    container, target, *ports = argv
    # Exit through the interpreter's normal shutdown, so SIGTERM from `stop` ends the relay cleanly.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    return asyncio.run(_serve(container, int(target), [int(port) for port in ports]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
