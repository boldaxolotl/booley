"""A fake T3 Code and Dashboard: their process boundary and the loopback ports they serve."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import socket
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.runtime import incontainer_setup, mcp_config
from tests.time_budget import hang_guard_s

TOKEN = "pairing-token-0123"
ADDED = (0, "Added project 1 (p) at /p.")
SANDBOX_T3_BASE = Path("/home/agent/.t3")  # where the T3 state volume is mounted
_DASHBOARD_PORT = 8090  # the Dashboard's fixed loopback port (ADR 0093)


@dataclass
class T3CodeFake:
    """Record `t3 serve`, `t3 project add`/`rename`, `t3 auth pairing create` and `booley dashboard`.

    `projects` maps each registered workspace root to its title, and `serve` prints the
    pairing token on stdout, as T3 Code does.
    """

    running: bool = False
    volume: Path | None = None  # this host's stand-in for the T3 state volume
    environment_id: str | None = None  # None: the listener on the port is not T3 Code
    replaced_records: list[bytes] = field(default_factory=list)
    add_replies: list[tuple[int, str] | OSError] = field(default_factory=list)
    spawn_error: OSError | None = None
    check_reply: tuple[int, str] | OSError = (0, "")
    checked: list[list[str]] = field(default_factory=list)
    served: list[list[str]] = field(default_factory=list)
    added: list[list[str]] = field(default_factory=list)
    paired: list[list[str]] = field(default_factory=list)
    projects: dict[str, str] = field(default_factory=dict)
    renamed: list[list[str]] = field(default_factory=list)
    rename_reply: tuple[int, str] | OSError | None = None
    sinks: list[str] = field(default_factory=list)
    serve_stdouts: list[object] = field(default_factory=list)
    dashboard_running: bool = False
    dashboards: list[list[str]] = field(default_factory=list)
    dashboard_cwds: list[object] = field(default_factory=list)

    def serving(self, port: int, **_kwargs: object) -> bool:
        if port == mcp_config.http_port():
            return True
        if port == incontainer_setup.T3_PORT:
            return self.running
        return port == _DASHBOARD_PORT and self.dashboard_running

    def describe(self) -> str | None:
        return self.environment_id if self.running else None

    def on_host(self, base: Path) -> Path:
        return self.volume if self.volume and base == SANDBOX_T3_BASE else base

    def publish(self, base: Path) -> None:
        record = base / "userdata" / "server-runtime.json"
        if record.exists():
            self.replaced_records.append(record.read_bytes())
        port = incontainer_setup.T3_PORT
        write_t3_runtime(base, pid=os.getpid(), port=port, environment_id="env-fake")
        self.environment_id = "env-fake"

    def popen(self, cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        if self.spawn_error:
            raise self.spawn_error
        if cmd[0] == "t3":
            self.served.append(cmd)
            self.serve_stdouts.append(kwargs["stdout"])
            self.publish(self.on_host(Path(cmd[cmd.index("--base-dir") + 1])))
            self.running = True
            if hasattr(kwargs["stdout"], "write"):
                kwargs["stdout"].write(f"Token: {TOKEN}\n".encode())
        elif cmd[1:] == ["-m", "booley.harness.booley", "dashboard"]:
            self.dashboards.append(cmd)
            self.dashboard_cwds.append(kwargs["cwd"])
            self.dashboard_running = True
        self.sinks += [s.name for s in (kwargs["stdout"], kwargs["stderr"]) if hasattr(s, "name")]
        return SimpleNamespace(poll=lambda: None)

    def run(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["auth", "pairing"]:
            self.paired.append(cmd)
            url = f"{cmd[cmd.index('--base-url') + 1]}/pair#token={TOKEN}-{len(self.paired)}"
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"pairUrl": url}, indent=2), "")
        if cmd[1:3] == ["policy", "check"]:
            self.checked.append(cmd)
            if isinstance(self.check_reply, OSError):
                raise self.check_reply
            return subprocess.CompletedProcess(cmd, self.check_reply[0], "", self.check_reply[1])
        if cmd[1:3] == ["project", "rename"]:
            return self._rename(cmd)
        self.added.append(cmd)
        if not self.running:  # `t3 project add` talks to a running server
            return subprocess.CompletedProcess(cmd, 78, "no server", "")
        reply = self.add_replies.pop(0) if self.add_replies else self._add(cmd)
        if isinstance(reply, OSError):
            raise reply
        code, out = reply
        return subprocess.CompletedProcess(cmd, code, out, "")

    def _add(self, cmd: list[str]) -> tuple[int, str]:
        root = cmd[-1]
        if root in self.projects:  # the old title stays, whatever `--title` says
            return (1, "ProjectAlreadyExistsError")
        title = cmd[cmd.index("--title") + 1] if "--title" in cmd else Path(root).name
        self.projects[root] = title
        return ADDED

    def _rename(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.renamed.append(cmd)
        if isinstance(self.rename_reply, OSError):
            raise self.rename_reply
        if self.rename_reply:
            return subprocess.CompletedProcess(cmd, self.rename_reply[0], self.rename_reply[1], "")
        root, title = cmd[-2:]
        out = "already named" if self.projects[root] == title else "Renamed"
        self.projects[root] = title
        return subprocess.CompletedProcess(cmd, 0, out, "")


def serve_t3_code(
    monkeypatch: pytest.MonkeyPatch,
    *,
    foreign: bool = False,
    volume: Path | None = None,
    add_replies: list[tuple[int, str] | OSError] | None = None,
    check_reply: tuple[int, str] | OSError = (0, ""),
    projects: dict[str, str] | None = None,
    rename_reply: tuple[int, str] | OSError | None = None,
) -> T3CodeFake:
    """Route `incontainer_setup`'s port and descriptor probes and `subprocess` to a `T3CodeFake`.

    With *foreign*, something other than T3 Code already answers on the port. *volume* stands
    in for the T3 state volume: what T3 Code writes, what is read back, what Booley prunes
    and links.
    The two probes are the network boundary and `subprocess` the process boundary; the
    global `subprocess` module stays untouched.
    """
    fake = T3CodeFake(
        running=foreign,
        volume=volume,
        add_replies=list(add_replies or []),
        check_reply=check_reply,
        projects=dict(projects or {}),
        rename_reply=rename_reply,
    )
    proven = incontainer_setup._t3_identity_proven
    monkeypatch.setattr(
        incontainer_setup, "_t3_identity_proven", lambda base: proven(fake.on_host(base))
    )
    pruned = incontainer_setup.prune_t3_logs
    # `ensure_t3_server` passes the fixed Sandbox path, which tests assert in the `t3 serve`
    # argv, so map it to *volume* here. With none, skip: a test run inside a Sandbox must never
    # prune that Sandbox's live T3 state volume.
    monkeypatch.setattr(
        incontainer_setup,
        "prune_t3_logs",
        lambda base: fake.volume and pruned(fake.on_host(base)),
    )
    monkeypatch.setattr(incontainer_setup, "_port_is_serving", fake.serving)
    monkeypatch.setattr(incontainer_setup, "_t3_environment_id", fake.describe, raising=False)
    process = SimpleNamespace(
        Popen=fake.popen,
        run=fake.run,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(incontainer_setup, "subprocess", process)
    _route_preview_browser(monkeypatch, fake)
    return fake


def _route_preview_browser(monkeypatch: pytest.MonkeyPatch, fake: T3CodeFake) -> None:
    """Stand in for the Sandbox Image's headless Chrome and version records beside the volume.

    With no volume, or without POSIX symlinks, link nothing: a test run inside a Sandbox
    must never write to that Sandbox's live T3 state volume.
    """
    # getattr and raising=False: the regression run applies this fake to a base without them.
    link = getattr(incontainer_setup, "link_t3_preview_browser", None)
    image = None
    if fake.volume and os.name == "posix":
        image = fake.volume.parent / "sandbox-image"
        chrome, record = image / "chrome", image / "preview-browser-version"
        chrome.mkdir(parents=True, exist_ok=True)
        (chrome / "chrome-headless-shell").write_bytes(b"#!/bin/sh\n")
        for path in (record, chrome / "VERSION"):
            path.write_bytes(b"1.2.3.4\n")
        for constant, target in (
            ("HEADLESS_CHROME_DIR", chrome),
            ("CHROME_VERSION_RECORD", chrome / "VERSION"),
            ("T3_BROWSER_VERSION_RECORD", record),
        ):
            # boundary: the Sandbox Image's filesystem
            monkeypatch.setattr(incontainer_setup, constant, str(target), raising=False)
    monkeypatch.setattr(  # boundary: the Sandbox's T3 state volume
        incontainer_setup,
        "link_t3_preview_browser",
        lambda base: image and link(fake.on_host(base)),
        raising=False,
    )


def write_t3_runtime(base: Path, *, pid: int | None, port: int, environment_id: str) -> bytes:
    """Write *base*'s environment ID and, with a *pid*, its runtime record, as T3 Code does."""
    userdata = base / "userdata"
    userdata.mkdir(parents=True, exist_ok=True)
    (userdata / "environment-id").write_bytes(environment_id.encode())
    if pid is None:
        return b""
    record = json.dumps({"version": 1, "pid": pid, "port": port}).encode()
    (userdata / "server-runtime.json").write_bytes(record)
    return record


@contextlib.contextmanager
def t3_listener(kind: str) -> Iterator[int]:
    """Serve a free loopback port and yield it.

    ``"drip"`` never completes a reply, ``"banner"`` does not speak HTTP, ``"not-t3"`` answers
    404, ``"nested"`` answers JSON too deep to parse, ``"oversized"`` answers ``env-own``'s
    descriptor padded past the size limit, and any other *kind* is the environment ID in a
    T3 Code descriptor.
    """
    stop = threading.Event()
    unusual = {
        "nested": b"[" * 200_000 + b"]" * 200_000,
        "oversized": json.dumps({"environmentId": "env-own", "pad": "x" * (1 << 20)}).encode(),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def handle(self) -> None:
            with contextlib.suppress(OSError):
                if kind == "drip":
                    while not stop.wait(0.2):
                        self.wfile.write(b"H")
                elif kind == "banner":
                    while self.rfile.readline().strip():  # unread input would reset the reply
                        pass
                    self.wfile.write(b"SSH-2.0-banner\r\n")
                else:
                    super().handle()

        def do_GET(self) -> None:
            if kind == "not-t3" or self.path != "/.well-known/t3/environment":
                self.send_error(404)
                return
            body = unusual.get(kind) or json.dumps({"environmentId": kind}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            stop.set()
            server.shutdown()
            thread.join(hang_guard_s())


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
