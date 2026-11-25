"""A fake T3 Code: the `t3` process boundary and the loopback port it serves."""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from booley.runtime import incontainer_setup, mcp_config
from tests.time_budget import hang_guard_s

TOKEN = "pairing-token-0123"
ADDED = (0, "Added project 1 (p) at /p.")


@dataclass
class T3CodeFake:
    """Record `t3 serve`, `t3 project add` and `t3 auth pairing create`; print the pairing token on stdout as T3 Code does."""

    running: bool = False
    add_replies: list[tuple[int, str] | OSError] = field(default_factory=list)
    spawn_error: OSError | None = None
    served: list[list[str]] = field(default_factory=list)
    added: list[list[str]] = field(default_factory=list)
    paired: list[list[str]] = field(default_factory=list)
    sinks: list[str] = field(default_factory=list)
    serve_stdouts: list[object] = field(default_factory=list)

    def serving(self, port: int, **_kwargs: object) -> bool:
        return port == mcp_config.http_port() or self.running

    def popen(self, cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        if self.spawn_error:
            raise self.spawn_error
        if cmd[0] == "t3":
            self.served.append(cmd)
            self.serve_stdouts.append(kwargs["stdout"])
            self.running = True
        self.sinks += [s.name for s in (kwargs["stdout"], kwargs["stderr"]) if hasattr(s, "name")]
        if hasattr(kwargs["stdout"], "write"):
            kwargs["stdout"].write(f"Token: {TOKEN}\n".encode())
        return SimpleNamespace(poll=lambda: None)

    def run(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["auth", "pairing"]:
            self.paired.append(cmd)
            url = f"{cmd[cmd.index('--base-url') + 1]}/pair#token={TOKEN}-{len(self.paired)}"
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"pairUrl": url}, indent=2), "")
        self.added.append(cmd)
        if not self.running:  # `t3 project add` talks to a running server
            return subprocess.CompletedProcess(cmd, 78, "no server", "")
        reply = self.add_replies.pop(0) if self.add_replies else ADDED
        if isinstance(reply, OSError):
            raise reply
        code, out = reply
        return subprocess.CompletedProcess(cmd, code, out, "")


def serve_t3_code(
    monkeypatch: pytest.MonkeyPatch,
    *,
    running: bool = False,
    add_replies: list[tuple[int, str] | OSError] | None = None,
) -> T3CodeFake:
    """Route `incontainer_setup`'s port probe and `subprocess` use to a `T3CodeFake`.

    The port probe is the network boundary and `subprocess` the process boundary; the
    global `subprocess` module stays untouched.
    """
    fake = T3CodeFake(running=running, add_replies=list(add_replies or []))
    monkeypatch.setattr(incontainer_setup, "_port_is_serving", fake.serving)
    process = SimpleNamespace(
        Popen=fake.popen,
        run=fake.run,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(incontainer_setup, "subprocess", process)
    return fake


@dataclass(frozen=True)
class EchoListener:
    """The port of an echo server, and the event that its one connection has ended."""

    port: int
    closed: threading.Event


@contextlib.contextmanager
def echo_listener() -> Iterator[EchoListener]:
    """Serve one echo connection on a free loopback port, standing in for `t3 serve`."""
    closed = threading.Event()
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(hang_guard_s())

        def echo() -> None:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection, contextlib.suppress(OSError):
                while data := connection.recv(1024):
                    connection.sendall(data)
            closed.set()

        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        try:
            yield EchoListener(server.getsockname()[1], closed)
        finally:
            thread.join(hang_guard_s())
