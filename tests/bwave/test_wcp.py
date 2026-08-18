"""Tests for the VaporView WCP client (ADR 0035).

A threaded fake server speaks VaporView's dialect (newline-delimited JSON,
{method,id,params} → {result|error,id}, id-less events) on an ephemeral
127.0.0.1 port, so every wire-level behavior is pinned without a viewer.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from booley.bwave import wcp as bwave_wcp
from booley.bwave.wcp import (
    WcpClient,
    WcpMethodError,
    WcpProtocolError,
    WcpTimeoutError,
    try_connect,
)

_GREETING_RESULT = {
    "name": "Vaporview",
    "version": "1.5.4",
    "protocol": "WCP",
    "protocol_version": "0",
    "capabilities": [
        "greeting",
        "open_document",
        "add_signal",
        "get_item_list",
        "get_item_info",
    ],
}


class FakeWcpServer:
    """Single-connection fake VaporView WCP server.

    *responses* maps method name → result payload (or a callable taking the
    params and returning one, or an ``{"error": {...}}`` marker to reply with
    an error). Unlisted methods echo ``{"success": True}``. *events_after*
    maps method name → an event object broadcast right after that method's
    reply (e.g. ``waveform_loaded`` after ``open_document``).
    """

    DISCONNECT = object()  # respond-to-method sentinel: drop the connection

    def __init__(self, responses=None, events_after=None):
        self.responses = {"greeting": _GREETING_RESULT, **(responses or {})}
        self.events_after = events_after or {}
        self.requests: list[dict] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._listener.close()
        self._thread.join(timeout=5)

    def _serve(self):
        try:
            conn, _addr = self._listener.accept()
        except OSError:
            return  # closed before any client connected
        with conn, conn.makefile("r", encoding="utf-8") as reader:
            for line in reader:
                msg = json.loads(line)
                self.requests.append(msg)
                method = msg.get("method")
                spec = self.responses.get(method, {"success": True})
                if spec is self.DISCONNECT:
                    return  # `with conn` closes the socket → client sees EOF
                if callable(spec):
                    spec = spec(msg.get("params"))
                if isinstance(spec, dict) and "error" in spec:
                    reply = {"error": spec["error"], "id": msg.get("id")}
                else:
                    reply = {"result": spec, "id": msg.get("id")}
                payload = json.dumps(reply) + "\n"
                event = self.events_after.get(method)
                if event is not None:
                    payload += json.dumps(event) + "\n"
                try:
                    conn.sendall(payload.encode("utf-8"))
                except OSError:
                    return

    def method_calls(self, method: str) -> list[dict]:
        return [m.get("params") or {} for m in self.requests if m.get("method") == method]


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    """Keep failure-path tests snappy; happy paths never hit these."""
    monkeypatch.setattr(bwave_wcp, "RESPONSE_TIMEOUT", 2.0)


def test_connect_performs_greeting_and_captures_capabilities():
    with FakeWcpServer() as srv, WcpClient(srv.port) as client:
        assert client.capabilities == _GREETING_RESULT["capabilities"]
    assert srv.requests[0]["method"] == "greeting"
    assert srv.requests[0]["id"] == 1


def test_connect_rejected_greeting_is_protocol_error():
    err = {"error": {"code": -32600, "message": "unsupported protocol version"}}
    with FakeWcpServer(responses={"greeting": err}) as srv:
        client = WcpClient(srv.port)
        with pytest.raises(WcpProtocolError, match="greeting rejected"):
            client.connect()
        client.close()


def test_connect_malformed_greeting_is_protocol_error():
    # A peer that answers but is not a WCP server (no capabilities field).
    with FakeWcpServer(responses={"greeting": {"hello": "world"}}) as srv:
        client = WcpClient(srv.port)
        with pytest.raises(WcpProtocolError, match="malformed"):
            client.connect()
        client.close()


def test_try_connect_returns_none_when_nothing_listens():
    # Grab-and-release an ephemeral port so nothing is listening on it.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    assert try_connect(dead_port) is None


def test_try_connect_propagates_protocol_errors():
    # A half-working peer must NOT be mistaken for "no server" — silently
    # launching a second viewer over it would be confusing.
    with (
        FakeWcpServer(responses={"greeting": {"nope": 1}}) as srv,
        pytest.raises(WcpProtocolError),
    ):
        try_connect(srv.port)


def test_setup_hint_avoids_vaporview_duplicate_start_race():
    hint = bwave_wcp.setup_hint()

    assert 'Do not run "WCP: Start Server"' in hint
    assert "falsely report EADDRINUSE" in hint


def test_request_correlates_reply_across_interleaved_event():
    # open_document acks and broadcasts waveform_loaded; a follow-up request
    # must get ITS reply while the event stays buffered for wait_event.
    event = {"type": "event", "event": "waveform_loaded", "uri": "file:///t.fst"}
    with (
        FakeWcpServer(events_after={"open_document": event}) as srv,
        WcpClient(srv.port) as client,
    ):
        client.open_document("file:///t.fst")
        assert client.get_open_documents() == []  # default echo has no docs
        got = client.wait_event("waveform_loaded", timeout=2)
        assert got["uri"] == "file:///t.fst"


def test_wait_event_times_out():
    with (
        FakeWcpServer() as srv,
        WcpClient(srv.port) as client,
        pytest.raises(WcpTimeoutError, match="waveform_loaded"),
    ):
        client.wait_event("waveform_loaded", timeout=0.2)


def test_method_error_carries_method_name():
    err = {"error": {"code": -32000, "message": "No active document"}}
    with (
        FakeWcpServer(responses={"add_signal": err}) as srv,
        WcpClient(srv.port) as client,
        pytest.raises(WcpMethodError, match="add_signal: No active document"),
    ):
        client.add_signal("tb.dut.state")


def test_server_disconnect_is_protocol_error():
    # A mid-session viewer crash/reload must surface as a protocol error on
    # the in-flight request, not hang until the response timeout.
    with (
        FakeWcpServer(responses={"get_item_list": FakeWcpServer.DISCONNECT}) as srv,
        WcpClient(srv.port) as client,
        pytest.raises(WcpProtocolError, match="closed"),
    ):
        client.request("get_item_list")


def test_non_json_line_is_protocol_error(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def garbage_server():
        conn, _ = listener.accept()
        with conn:
            conn.recv(4096)  # swallow the greeting
            conn.sendall(b"this is not json\n")

    t = threading.Thread(target=garbage_server, daemon=True)
    t.start()
    client = WcpClient(port)
    try:
        with pytest.raises(WcpProtocolError, match="non-JSON"):
            client.connect()
    finally:
        client.close()
        listener.close()
        t.join(timeout=5)


def test_verb_param_shapes():
    """Pin the exact wire params of every high-level verb (vaporview dialect)."""
    with (
        FakeWcpServer(
            responses={
                "get_item_list": {"type": "response", "command": "get_item_list", "ids": [3, 7]},
                "get_open_documents": {
                    "documents": ["file:///t.fst"],
                    "last_active_document": None,
                },
            }
        ) as srv,
        WcpClient(srv.port) as client,
    ):
        uri = "file:///t.fst"
        client.open_document(uri)
        assert client.get_open_documents() == [uri]
        assert client.get_item_list(uri=uri) == [3, 7]
        client.remove_items([3, 7], uri=uri)
        client.add_signal("tb.dut.fifo.full", uri=uri)
        client.set_viewport(100, 2000, uri=uri)
        client.set_marker(1050, uri=uri)

    uri = "file:///t.fst"
    assert srv.method_calls("open_document") == [{"uri": uri}]
    assert srv.method_calls("get_item_list") == [{"uri": uri}]
    assert srv.method_calls("remove_items") == [{"ids": [3, 7], "uri": uri}]
    assert srv.method_calls("add_signal") == [{"instance_path": "tb.dut.fifo.full", "uri": uri}]
    assert srv.method_calls("set_viewport_range") == [{"start": 100, "end": 2000, "uri": uri}]
    assert srv.method_calls("set_marker") == [{"time": 1050, "uri": uri, "marker_type": 0}]


def test_add_signal_carries_the_bit_range_as_msb_lsb():
    """A vector is `path` + msb/lsb — never `path[msb:lsb]` (VaporView findChild)."""
    with FakeWcpServer() as srv, WcpClient(srv.port) as client:
        client.add_signal("tb.dut.state", uri="file:///t.fst", msb=3, lsb=0)

    assert srv.method_calls("add_signal") == [
        {"instance_path": "tb.dut.state", "uri": "file:///t.fst", "msb": 3, "lsb": 0}
    ]


def test_set_marker_addresses_both_of_vaporviews_markers():
    """START rides the main marker, END the alt one — that pair is all there is."""
    with FakeWcpServer() as srv, WcpClient(srv.port) as client:
        client.set_marker(100, uri="file:///t.fst", marker_type=WcpClient.MARKER_MAIN)
        client.set_marker(900, uri="file:///t.fst", marker_type=WcpClient.MARKER_ALT)

    assert srv.method_calls("set_marker") == [
        {"time": 100, "uri": "file:///t.fst", "marker_type": 0},
        {"time": 900, "uri": "file:///t.fst", "marker_type": 1},
    ]


def test_get_item_info_returns_instance_paths():
    info = {
        "type": "response",
        "command": "get_item_info",
        "results": [
            {"name": "tb.dut.state", "type": "reg", "id": 4},
            {"name": "tb.dut.clk", "type": "wire", "id": 9},
            "junk",  # a malformed row must not take the whole reply down
        ],
    }
    with FakeWcpServer(responses={"get_item_info": info}) as srv, WcpClient(srv.port) as client:
        rows = client.get_item_info([4, 9], uri="file:///t.fst")

    assert [row["name"] for row in rows] == ["tb.dut.state", "tb.dut.clk"]
    assert srv.method_calls("get_item_info") == [{"ids": [4, 9], "uri": "file:///t.fst"}]


def test_wcp_port_env_override(monkeypatch):
    monkeypatch.setenv("BOOLEY_WCP_PORT", "6001")
    assert bwave_wcp.wcp_port() == 6001
    monkeypatch.setenv("BOOLEY_WCP_PORT", "garbage")
    assert bwave_wcp.wcp_port() == bwave_wcp.WCP_DEFAULT_PORT
    monkeypatch.delenv("BOOLEY_WCP_PORT")
    assert bwave_wcp.wcp_port() == bwave_wcp.WCP_DEFAULT_PORT
