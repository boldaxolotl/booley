"""WCP client for the VaporView Waveform Viewer (ADR 0035).

`bwave gui` drives the VaporView VS Code extension (`lramseyer.vaporview`)
over its Waveform Control Protocol server: TCP on 127.0.0.1, newline-delimited
JSON messages of the shape ``{"method": ..., "params": ..., "id": ...}`` with
``{"result": ...|"error": {...}, "id": ...}`` replies. This is VaporView's
dialect — it deviates from the null-byte-framed WCP draft spec — so every
framing assumption lives in this module only (see vaporview WCP_DOCS.md and
src/extension_core/wcp_server.ts, verified against v1.5.4).

Server-to-client *events* (``{"type": "event", "event": ..., "uri": ...}``,
no ``id``) arrive interleaved with replies; they are buffered so a caller can
wait for ``waveform_loaded`` after an ``open_document`` (which acks
immediately and loads asynchronously).

Time values sent without a ``units`` field are interpreted by VaporView in
the waveform file's native timescale units — exactly what the Rust bwave
binary reports as ticks (``target_tick`` / ``total_ticks``), so no unit
conversion happens here.

Split out of ``cli.py`` like ``sessions.py`` (principle 8): the CLI owns the
CLI surface, this module owns the wire protocol.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from typing import Any

WCP_HOST = "127.0.0.1"
# Must match the `vaporview.wcp.port` pinned in the generated devcontainer
# spec (harness/devcontainer.py). Env var is the escape hatch, not a knob —
# ADR 0035 keeps the port out of booley.toml.
WCP_DEFAULT_PORT = 54322
WCP_PORT_ENV = "BOOLEY_WCP_PORT"

# The probe gates the bare-open fallback, so it must be snappy; replies to
# normal requests are UI-thread work and get a little more room.
CONNECT_TIMEOUT = 2.0
RESPONSE_TIMEOUT = 10.0
# open_document acks instantly and streams the file in the background; the
# `waveform_loaded` event on a large FST is the slow part. Kept well under
# the MCP server's 600 s subprocess budget.
LOAD_TIMEOUT = 60.0


class WcpError(Exception):
    """Base class for WCP client failures."""


class WcpUnreachableError(WcpError):
    """No WCP server is listening (connect refused / timed out)."""


class WcpProtocolError(WcpError):
    """The peer broke the wire protocol (non-JSON line, bad greeting, EOF)."""


class WcpTimeoutError(WcpError):
    """The server accepted the connection but a reply/event never came."""


class WcpMethodError(WcpError):
    """The server rejected a method call; carries the method name."""

    def __init__(self, method: str, message: str):
        super().__init__(f"{method}: {message}")
        self.method = method


def wcp_port() -> int:
    """WCP port: BOOLEY_WCP_PORT override, else the pinned default."""
    raw = os.environ.get(WCP_PORT_ENV, "")
    try:
        return int(raw) if raw else WCP_DEFAULT_PORT
    except ValueError:
        return WCP_DEFAULT_PORT


def setup_hint(port: int | None = None) -> str:
    """One authoritative unreachable-server message (used by probe and errors)."""
    port = port if port is not None else wcp_port()
    return (
        f"ERROR: scoped `bwave gui` needs the VaporView WCP control server on "
        f"{WCP_HOST}:{port} and none is reachable.\n"
        '  - Most likely: run "Developer: Reload Window" in the attached VS Code\n'
        "    window. On the first window of a fresh container the patch that makes\n"
        "    the viewer auto-start its server lands after the extension host is\n"
        "    already up, so it only takes effect on the next reload, or\n"
        '  - Do not run "WCP: Start Server" while Booley auto-start is enabled.\n'
        "    VaporView 1.5.4 can bind the port during activation, try it again, and\n"
        "    falsely report EADDRINUSE even though the first start succeeded.\n"
        "  - Rebuild the devcontainer (installs lramseyer.vaporview and enables its\n"
        "    WCP server), or\n"
        '  - In VS Code: install lramseyer.vaporview, set "vaporview.wcp.enabled": true\n'
        f'    and "vaporview.wcp.port": {WCP_DEFAULT_PORT}.\n'
        f"  Port override: {WCP_PORT_ENV}."
    )


class WcpClient:
    """One TCP connection to a VaporView WCP server.

    Usage::

        with WcpClient() as client:  # connects + greeting handshake
            client.open_document(uri)
            client.wait_event("waveform_loaded")
            client.add_signal("tb.dut.state")
    """

    def __init__(self, port: int | None = None):
        self.port = port if port is not None else wcp_port()
        self._sock: socket.socket | None = None
        self._reader = None
        self._next_id = 1
        self._events: list[dict] = []
        self.capabilities: list[str] = []

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((WCP_HOST, self.port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            raise WcpUnreachableError(f"no WCP server on {WCP_HOST}:{self.port}: {exc}") from exc
        self._reader = self._sock.makefile("r", encoding="utf-8", newline="\n")
        # Handshake. A greeting failure means whatever answered is not a WCP
        # server (or an incompatible one) — surface as a protocol error, not
        # a method error, so callers treat it like a broken peer.
        try:
            result = self.request("greeting")
        except WcpMethodError as exc:
            raise WcpProtocolError(f"greeting rejected: {exc}") from exc
        if not isinstance(result, dict) or "capabilities" not in result:
            raise WcpProtocolError(f"greeting reply malformed: {result!r}")
        caps = result.get("capabilities")
        self.capabilities = caps if isinstance(caps, list) else []

    def close(self) -> None:
        for closable in (self._reader, self._sock):
            if closable is not None:
                with contextlib.suppress(OSError):
                    closable.close()
        self._reader = None
        self._sock = None

    def __enter__(self) -> WcpClient:
        if self._sock is None:
            self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- wire protocol ------------------------------------------------------
    def request(self, method: str, params: dict | None = None) -> Any:
        """Send one request and return its ``result`` (raises on ``error``)."""
        assert self._sock is not None and self._reader is not None, "not connected"
        req_id = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"method": method, "id": req_id}
        if params is not None:
            msg["params"] = params
        try:
            self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        except OSError as exc:
            raise WcpProtocolError(f"send failed: {exc}") from exc

        deadline = time.monotonic() + RESPONSE_TIMEOUT
        while True:
            reply = self._read_message(deadline, waiting_for=f"reply to {method}")
            if reply.get("id") == req_id:
                if "error" in reply:
                    err = reply["error"]
                    detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise WcpMethodError(method, detail)
                return reply.get("result")
            # Not our reply: async events (and any stray replies) are buffered
            # for wait_event; discarding them here would race open_document
            # against its waveform_loaded broadcast.
            self._events.append(reply)

    def wait_event(self, name: str, timeout: float = LOAD_TIMEOUT) -> dict:
        """Return the next buffered/incoming event named *name*."""
        for i, msg in enumerate(self._events):
            if msg.get("event") == name:
                return self._events.pop(i)
        deadline = time.monotonic() + timeout
        while True:
            msg = self._read_message(deadline, waiting_for=f"event {name}")
            if msg.get("event") == name:
                return msg
            self._events.append(msg)

    def _read_message(self, deadline: float, waiting_for: str) -> dict:
        """Read one JSON line, honoring *deadline*."""
        assert self._sock is not None and self._reader is not None, "not connected"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WcpTimeoutError(f"timed out waiting for {waiting_for}")
        self._sock.settimeout(remaining)
        try:
            line = self._reader.readline()
        except TimeoutError as exc:
            raise WcpTimeoutError(f"timed out waiting for {waiting_for}") from exc
        except OSError as exc:
            raise WcpProtocolError(f"read failed: {exc}") from exc
        if not line:
            raise WcpProtocolError(f"server closed the connection ({waiting_for})")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WcpProtocolError(f"non-JSON line from server: {line[:200]!r}") from exc
        if not isinstance(msg, dict):
            raise WcpProtocolError(f"non-object message from server: {msg!r}")
        return msg

    # -- high-level verbs (param shapes per vaporview wcp_server.ts) ---------
    # Every verb takes an explicit uri: VaporView defaults to the *active*
    # document, which may be a different waveform tab than the one bwave is
    # scoping — never rely on focus.
    def open_document(self, uri: str) -> None:
        """Open *uri*; acks immediately — follow with wait_event("waveform_loaded")."""
        self.request("open_document", {"uri": uri})

    def get_open_documents(self) -> list[str]:
        result = self.request("get_open_documents")
        docs = result.get("documents") if isinstance(result, dict) else None
        return docs if isinstance(docs, list) else []

    def get_item_list(self, uri: str | None = None) -> list[int]:
        """Netlist ids of the items currently displayed in the document."""
        result = self.request("get_item_list", self._with_uri({}, uri))
        ids = result.get("ids") if isinstance(result, dict) else None
        return ids if isinstance(ids, list) else []

    def get_item_info(self, ids: list[int], uri: str | None = None) -> list[dict]:
        """Resolve netlist ids to their instance paths (`name` per entry)."""
        result = self.request("get_item_info", self._with_uri({"ids": list(ids)}, uri))
        rows = result.get("results") if isinstance(result, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def remove_items(self, ids: list[int], uri: str | None = None) -> None:
        self.request("remove_items", self._with_uri({"ids": ids}, uri))

    def add_signal(
        self,
        instance_path: str,
        uri: str | None = None,
        msb: int | None = None,
        lsb: int | None = None,
    ) -> None:
        """Add one variable by instance path.

        VaporView keys its netlist on the BARE leaf name and carries the bit
        range in separate msb/lsb fields (see its findChild()), so a vector is
        `tb.dut.state` + msb/lsb — never `tb.dut.state[3:0]`. It answers
        `{"success": true}` either way and only pops a GUI warning when the
        lookup misses, so callers must verify with get_item_list/get_item_info
        rather than trust this ack.
        """
        params: dict[str, Any] = {"instance_path": instance_path}
        if msb is not None:
            params["msb"] = msb
        if lsb is not None:
            params["lsb"] = lsb
        self.request("add_signal", self._with_uri(params, uri))

    def set_viewport(self, start_tick: int, end_tick: int, uri: str | None = None) -> None:
        """Show exactly [start_tick, end_tick] (native file timescale units)."""
        self.request(
            "set_viewport_range",
            self._with_uri({"start": start_tick, "end": end_tick}, uri),
        )

    # VaporView has exactly two markers; their delta is what the status bar
    # reports, which is why `gui --time` brackets the range with them.
    MARKER_MAIN = 0
    MARKER_ALT = 1

    def set_marker(
        self, tick: int, uri: str | None = None, marker_type: int = MARKER_MAIN
    ) -> None:
        """Place a marker at *tick* (native file timescale units)."""
        self.request(
            "set_marker",
            self._with_uri({"time": tick, "marker_type": marker_type}, uri),
        )

    @staticmethod
    def _with_uri(params: dict[str, Any], uri: str | None) -> dict[str, Any]:
        if uri:
            params["uri"] = uri
        return params


def try_connect(port: int | None = None) -> WcpClient | None:
    """Reachability probe: a connected+greeted client, or None if nothing listens.

    Only *absence* of a server maps to None (that is the bare-open fallback
    signal); a peer that answers but breaks protocol raises, because silently
    launching a second viewer over a half-working one would be confusing.
    """
    client = WcpClient(port)
    try:
        client.connect()
    except WcpUnreachableError:
        client.close()
        return None
    except WcpError:
        client.close()
        raise
    return client
