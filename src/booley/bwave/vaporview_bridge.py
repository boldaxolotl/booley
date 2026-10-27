"""Client for the Booley VaporView bridge (ADR 0078).

`bwave gui` never talks to VaporView directly. The Booley workspace extension
(``data/vscode/dashboard/vaporview_bridge.js``) runs inside every attached VS
Code window's extension host, next to VaporView, and listens on a Unix socket:

    ~/.booley/vaporview-bridge/window-<extension-host-pid>.sock

One request per connection: the client writes one JSON object terminated by a
newline, the bridge answers with one JSON line and closes. Requests carry
``{"protocol": 1, "op": ...}``; replies are ``{"ok": true, "result": ...}`` or
``{"ok": false, "error": "..."}``. The bridge only offers generic primitives
(``ping``, ``open``, ``state``, ``apply``, ``viewport``); building the view and
verifying the result is this package's job, where it is testable.

Several windows may be attached to one Sandbox, each with its own bridge. A
window's integrated terminals carry ``BOOLEY_VAPORVIEW_BRIDGE`` naming its own
socket, so a command typed there targets that window. Elsewhere the bridges
are discovered from the socket directory; see :func:`select_bridge`.

Split out of ``cli.py`` (principle 8): the CLI owns the view, this module owns
the transport.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict

PROTOCOL = 1
# Terminals of an attached window name that window's bridge socket.
SOCKET_ENV = "BOOLEY_VAPORVIEW_BRIDGE"
_SOCKET_GLOB = "window-*.sock"
# Liveness probes must be snappy; state reads are extension-host work.
PING_TIMEOUT = 3.0
REQUEST_TIMEOUT = 15.0
# `open` waits for VaporView to parse the trace and initialize its webview; a
# large FST is the slow part. Kept well under the MCP server's 600 s budget.
OPEN_TIMEOUT = 120.0
# Replies hold at most one viewer session; anything larger is a broken peer.
_MAX_REPLY_BYTES = 16 * 1024 * 1024


class BridgeError(Exception):
    """Base class for bridge failures."""


class BridgeUnavailableError(BridgeError):
    """No live bridge, so no VS Code window is attached with the Booley extension."""


class BridgeAmbiguousError(BridgeError):
    """Several attached windows could show the trace and none is preferred."""


class BridgeProtocolError(BridgeError):
    """The peer broke the wire protocol (bad JSON, EOF, oversized reply)."""


class BridgeRequestError(BridgeError):
    """The bridge ran the operation and reported that it failed."""

    def __init__(self, op: str, message: str):
        super().__init__(f"{op}: {message}")
        self.op = op


@dataclass(frozen=True)
class BridgeInfo:
    """What one live bridge reported about its window (the ``ping`` result)."""

    socket_path: Path
    vaporview: str | None
    focused: bool
    last_focused: float
    documents: tuple[str, ...]
    error: str | None = None


def bridge_dir(home: Path | None = None) -> Path:
    """Directory holding every bridge socket for the Sandbox user."""
    root = home if home is not None else Path(os.environ.get("HOME", "/home/agent"))
    return root / ".booley" / "vaporview-bridge"


class BridgeClient:
    """Requests against one bridge socket; each request is its own connection."""

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    def request(self, op: str, *, timeout: float = REQUEST_TIMEOUT, **params: Any) -> Any:
        """Send one operation and return its ``result``, raising on failure."""
        message = json.dumps({"protocol": PROTOCOL, "op": op, **params}) + "\n"
        reply = _exchange(self.socket_path, message.encode("utf-8"), timeout)
        try:
            payload = require_dict(json.loads(reply), field=f"{op} reply")
        except (ValueError, BoundaryError) as exc:
            raise BridgeProtocolError(f"{op}: malformed reply from the bridge: {exc}") from exc
        if payload.get("ok") is True:
            return payload.get("result")
        error = payload.get("error")
        raise BridgeRequestError(op, error if isinstance(error, str) else repr(payload))

    def ping(self) -> BridgeInfo:
        """Describe the bridge's window; a malformed answer is a protocol error."""
        result = self.request("ping", timeout=PING_TIMEOUT)
        return _parse_ping(self.socket_path, result)

    def open(self, path: str) -> bool:
        """Open or reveal *path*; True when the viewer had to open it."""
        result = self.request("open", timeout=OPEN_TIMEOUT, path=path)
        opened = result.get("opened") if isinstance(result, dict) else None
        if not isinstance(opened, bool):
            raise BridgeProtocolError(f"open: reply lacks a boolean 'opened': {result!r}")
        return opened

    def state(self, path: str) -> dict | None:
        """The viewer's session for *path*, or None when it is not open."""
        result = self.request("state", path=path)
        if result is None or isinstance(result, dict):
            return result
        raise BridgeProtocolError(f"state: reply is not an object: {result!r}")

    def apply(self, path: str, state: dict) -> None:
        """Ask the viewer to load *state* for *path*; it applies asynchronously."""
        self.request("apply", path=path, state=state)

    def set_viewport(self, path: str, start: int, end: int) -> None:
        """Show exactly ``[start, end]`` in file timescale ticks."""
        self.request("viewport", path=path, start=start, end=end)

    def reveal_time(self, path: str, tick: int) -> None:
        """Scroll *tick* into view without changing the zoom."""
        self.request("viewport", path=path, time=tick)


def _exchange(socket_path: Path, payload: bytes, timeout: float) -> bytes:
    """Write one request line and read the single reply line."""
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        raise BridgeUnavailableError("this platform has no Unix sockets")
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(socket_path))
        except OSError as exc:
            raise BridgeUnavailableError(f"cannot connect to {socket_path}: {exc}") from exc
        try:
            sock.sendall(payload)
            return _read_line(sock)
        except TimeoutError as exc:
            raise BridgeProtocolError(
                f"timed out after {timeout:.0f}s waiting for the bridge"
            ) from exc
        except OSError as exc:
            raise BridgeProtocolError(f"bridge connection failed: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def _read_line(sock: socket.socket) -> bytes:
    """Read until the reply's newline; EOF first is a protocol error."""
    chunks: list[bytes] = []
    size = 0
    while size <= _MAX_REPLY_BYTES:
        chunk = sock.recv(65536)
        if not chunk:
            raise BridgeProtocolError("the bridge closed the connection without a reply")
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    raise BridgeProtocolError(f"bridge reply exceeds {_MAX_REPLY_BYTES} bytes")


def _parse_ping(socket_path: Path, result: Any) -> BridgeInfo:
    """Validate the ``ping`` result at the boundary."""
    if not isinstance(result, dict) or result.get("protocol") != PROTOCOL:
        raise BridgeProtocolError(
            f"ping: unsupported bridge reply {result!r}; refresh the Sandbox"
        )
    vaporview = result.get("vaporview")
    documents = result.get("documents")
    last_focused = result.get("lastFocused")
    error = result.get("error")
    if (
        (vaporview is not None and not isinstance(vaporview, str))
        or not isinstance(documents, list)
        or not all(isinstance(doc, str) for doc in documents)
        or isinstance(last_focused, bool)
        or not isinstance(last_focused, int | float)
        or (error is not None and not isinstance(error, str))
    ):
        raise BridgeProtocolError(f"ping: malformed bridge reply {result!r}")
    return BridgeInfo(
        socket_path=socket_path,
        vaporview=vaporview,
        focused=result.get("focused") is True,
        last_focused=float(last_focused),
        documents=tuple(documents),
        error=error,
    )


def _ping_or_prune(path: Path) -> BridgeInfo | None:
    """Ping one socket; a refused connection means a dead window, so remove it."""
    try:
        return BridgeClient(path).ping()
    except BridgeUnavailableError as exc:
        cause = exc.__cause__
        if isinstance(cause, OSError) and cause.errno == errno.ECONNREFUSED:
            with contextlib.suppress(OSError):
                path.unlink()
        return None


def _candidate_sockets(home: Path | None) -> list[Path]:
    try:
        return sorted(bridge_dir(home).glob(_SOCKET_GLOB))
    except OSError:
        return []


def discover(home: Path | None = None) -> list[BridgeInfo]:
    """Every live bridge in the socket directory, pruning stale sockets.

    A bridge that answers but breaks the protocol raises: silently skipping a
    half-working window would hide the reason `bwave gui` cannot use it.
    """
    return [
        info for path in _candidate_sockets(home) if (info := _ping_or_prune(path)) is not None
    ]


def select_bridge(
    bridges: list[BridgeInfo], trace: str, *, env: Mapping[str, str] | None = None
) -> BridgeInfo:
    """Choose the window that should show *trace*.

    Preference: the window whose terminal ran this command, the only window,
    the only window already showing the trace, then the most recently focused
    window when that is unambiguous.
    """
    if not bridges:
        raise BridgeUnavailableError("no VS Code window with the Booley bridge is attached")
    named = (env if env is not None else os.environ).get(SOCKET_ENV)
    if named:
        own = [info for info in bridges if str(info.socket_path) == named]
        if own:
            return own[0]
    if len(bridges) == 1:
        return bridges[0]
    showing = [info for info in bridges if trace in info.documents]
    if len(showing) == 1:
        return showing[0]
    pool = showing or bridges
    latest = max(info.last_focused for info in pool)
    recent = [info for info in pool if info.last_focused == latest]
    if latest > 0 and len(recent) == 1:
        return recent[0]
    raise BridgeAmbiguousError(
        f"{len(bridges)} VS Code windows are attached to this Sandbox and none is "
        "clearly the one to use"
    )


def doctor_summary(home: Path | None = None) -> dict[str, Any]:
    """JSON-ready bridge inventory for Doctor's in-Sandbox probe.

    Unlike :func:`discover`, a misbehaving bridge is reported, not raised, so
    Doctor can name it next to the healthy ones.
    """
    bridges: list[dict[str, Any]] = []
    for path in _candidate_sockets(home):
        try:
            info = _ping_or_prune(path)
        except BridgeError as exc:
            bridges.append({"vaporview": None, "error": str(exc)})
            continue
        if info is not None:
            bridges.append({"vaporview": info.vaporview, "error": info.error})
    return {"bridges": bridges}
