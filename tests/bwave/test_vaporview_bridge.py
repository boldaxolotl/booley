"""Wire, discovery, and window-selection tests for the VaporView bridge client.

A real Unix-socket server stands in for the Booley extension's bridge
(``data/vscode/dashboard/vaporview_bridge.js``; its own Node suite covers the
JavaScript side). Each request is one connection: one JSON line in, one out.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from booley.bwave import vaporview_bridge as bridge

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(socket, "AF_UNIX"),
    reason="the bridge serves Unix sockets inside Linux Sandboxes",
)


def _ping(**overrides) -> dict:
    return {
        "protocol": 1,
        "vaporview": "1.6.0",
        "focused": False,
        "lastFocused": 0,
        "documents": [],
        **overrides,
    }


class FakeBridge:
    """Serve one scripted reply per request on a Unix socket; record requests."""

    def __init__(self, path: Path, handler: Callable[[dict], object]):
        self.path = path
        self.handler = handler
        self.requests: list[dict] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data)
                self.requests.append(request)
                reply = self.handler(request)
                if isinstance(reply, bytes):
                    conn.sendall(reply)
                elif reply is not None:
                    conn.sendall((json.dumps(reply) + "\n").encode())

    def close(self) -> None:
        self._server.close()


@pytest.fixture()
def home():
    # Short path: Unix socket names are capped near 108 bytes.
    with tempfile.TemporaryDirectory(prefix="vvb-", dir="/tmp") as raw:
        root = Path(raw)
        bridge.bridge_dir(root).mkdir(parents=True)
        yield root


@pytest.fixture()
def servers():
    started: list[FakeBridge] = []
    yield started
    for server in started:
        server.close()


def _serve(servers, home: Path, name: str, handler) -> FakeBridge:
    server = FakeBridge(bridge.bridge_dir(home) / name, handler)
    servers.append(server)
    return server


def _ok(result):
    return {"ok": True, "result": result}


def test_request_carries_protocol_and_op_and_returns_result(home, servers):
    server = _serve(servers, home, "window-1.sock", lambda req: _ok({"opened": True}))

    assert bridge.BridgeClient(server.path).open("/work/trace.fst") is True
    assert server.requests == [{"protocol": 1, "op": "open", "path": "/work/trace.fst"}]


def test_apply_and_viewport_send_their_parameters(home, servers):
    server = _serve(servers, home, "window-1.sock", lambda req: _ok({}))
    client = bridge.BridgeClient(server.path)

    client.apply("/t.fst", {"displayedSignals": [], "markerTime": 5})
    client.set_viewport("/t.fst", 10, 20)
    client.reveal_time("/t.fst", 15)

    assert server.requests == [
        {
            "protocol": 1,
            "op": "apply",
            "path": "/t.fst",
            "state": {"displayedSignals": [], "markerTime": 5},
        },
        {"protocol": 1, "op": "viewport", "path": "/t.fst", "start": 10, "end": 20},
        {"protocol": 1, "op": "viewport", "path": "/t.fst", "time": 15},
    ]


def test_bridge_error_reply_raises_request_error(home, servers):
    server = _serve(servers, home, "window-1.sock", lambda req: {"ok": False, "error": "not open"})

    with pytest.raises(bridge.BridgeRequestError, match="state: not open"):
        bridge.BridgeClient(server.path).state("/t.fst")


@pytest.mark.parametrize(
    "reply",
    [b"not json\n", b"[1]\n", b'{"ok": true}', b""],
    ids=["garbage", "non-object", "no-newline-then-eof", "eof"],
)
def test_malformed_replies_are_protocol_errors(home, servers, reply):
    server = _serve(servers, home, "window-1.sock", lambda req: reply)

    with pytest.raises(bridge.BridgeProtocolError):
        bridge.BridgeClient(server.path).state("/t.fst")


def test_state_reply_must_be_object_or_null(home, servers):
    server = _serve(servers, home, "window-1.sock", lambda req: _ok([1, 2]))
    with pytest.raises(bridge.BridgeProtocolError, match="not an object"):
        bridge.BridgeClient(server.path).state("/t.fst")


def test_open_reply_must_report_a_boolean(home, servers):
    server = _serve(servers, home, "window-1.sock", lambda req: _ok({"opened": "yes"}))
    with pytest.raises(bridge.BridgeProtocolError, match="opened"):
        bridge.BridgeClient(server.path).open("/t.fst")


def test_missing_socket_is_unavailable(home):
    with pytest.raises(bridge.BridgeUnavailableError):
        bridge.BridgeClient(bridge.bridge_dir(home) / "window-9.sock").ping()


def test_ping_parses_window_report(home, servers):
    server = _serve(
        servers,
        home,
        "window-1.sock",
        lambda req: _ok(_ping(focused=True, lastFocused=1700, documents=["/a.fst"])),
    )

    info = bridge.BridgeClient(server.path).ping()

    assert info == bridge.BridgeInfo(server.path, "1.6.0", True, 1700.0, ("/a.fst",))


@pytest.mark.parametrize(
    "result",
    [
        _ping(protocol=2),
        _ping(documents="nope"),
        _ping(lastFocused=True),
        _ping(vaporview=5),
        None,
    ],
    ids=["protocol", "documents", "bool-focus", "version-type", "null"],
)
def test_ping_rejects_unsupported_or_malformed_reports(home, servers, result):
    server = _serve(servers, home, "window-1.sock", lambda req: _ok(result))
    with pytest.raises(bridge.BridgeProtocolError):
        bridge.BridgeClient(server.path).ping()


def test_discover_finds_live_bridges_and_prunes_dead_sockets(home, servers):
    live = _serve(servers, home, "window-1.sock", lambda req: _ok(_ping()))
    dead_path = bridge.bridge_dir(home) / "window-2.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(dead_path))  # bound but never listening: connect is refused
    dead.close()
    unrelated = bridge.bridge_dir(home) / "notes.txt"
    unrelated.write_text("keep me")

    found, problems = bridge.discover(home)

    assert [info.socket_path for info in found] == [live.path]
    assert problems == []
    assert not dead_path.exists()
    assert unrelated.exists()


def test_discover_without_directory_is_empty(tmp_path):
    assert bridge.discover(tmp_path / "nowhere") == ([], [])


def _frozen(req):
    time.sleep(1.0)  # accepts the request, never answers in time


def test_discover_skips_half_working_and_frozen_windows(home, servers, monkeypatch):
    monkeypatch.setattr(bridge, "PING_TIMEOUT", 0.2)
    live = _serve(servers, home, "window-1.sock", lambda req: _ok(_ping()))
    _serve(servers, home, "window-2.sock", lambda req: b"garbage\n")
    _serve(servers, home, "window-3.sock", _frozen)

    found, problems = bridge.discover(home)

    assert [info.socket_path for info in found] == [live.path]
    assert [problem.split(":")[0] for problem in problems] == ["window-2.sock", "window-3.sock"]
    assert "timed out" in problems[1]


def test_find_bridge_asks_the_terminal_window_first_and_alone(home, servers, monkeypatch):
    monkeypatch.setattr(bridge, "PING_TIMEOUT", 0.2)
    frozen = _serve(servers, home, "window-1.sock", _frozen)
    own = _serve(servers, home, "window-2.sock", lambda req: _ok(_ping()))

    found = bridge.find_bridge("/t.fst", home=home, env={bridge.SOCKET_ENV: str(own.path)})

    assert found.socket_path == own.path
    assert frozen.requests == []  # the frozen window was never asked


def test_find_bridge_falls_back_when_the_terminal_window_is_gone(home, servers):
    live = _serve(servers, home, "window-1.sock", lambda req: _ok(_ping()))
    env = {bridge.SOCKET_ENV: str(bridge.bridge_dir(home) / "window-gone.sock")}

    assert bridge.find_bridge("/t.fst", home=home, env=env).socket_path == live.path


def test_find_bridge_survives_a_frozen_peer(home, servers, monkeypatch):
    monkeypatch.setattr(bridge, "PING_TIMEOUT", 0.2)
    _serve(servers, home, "window-1.sock", _frozen)
    live = _serve(servers, home, "window-2.sock", lambda req: _ok(_ping()))

    assert bridge.find_bridge("/t.fst", home=home, env={}).socket_path == live.path


def test_find_bridge_reports_broken_windows_only_when_none_is_usable(home, servers):
    _serve(servers, home, "window-1.sock", lambda req: b"garbage\n")

    with pytest.raises(bridge.BridgeUnavailableError) as exc:
        bridge.find_bridge("/t.fst", home=home, env={})

    assert exc.value.problems and exc.value.problems[0].startswith("window-1.sock: ")
    assert "skipped: window-1.sock" in str(exc.value)


def test_find_bridge_with_nothing_attached_has_no_problems(home):
    with pytest.raises(bridge.BridgeUnavailableError) as exc:
        bridge.find_bridge("/t.fst", home=home, env={})
    assert exc.value.problems == []


def test_doctor_summary_reports_each_window_without_raising(home, servers):
    _serve(servers, home, "window-1.sock", lambda req: _ok(_ping()))
    _serve(
        servers,
        home,
        "window-2.sock",
        lambda req: _ok(_ping(vaporview=None, error="VaporView is not installed")),
    )
    _serve(servers, home, "window-3.sock", lambda req: b"garbage\n")

    summary = bridge.doctor_summary(home)

    assert summary["bridges"][0] == {"vaporview": "1.6.0", "error": None}
    assert summary["bridges"][1] == {"vaporview": None, "error": "VaporView is not installed"}
    assert summary["bridges"][2]["vaporview"] is None
    assert "malformed" in summary["bridges"][2]["error"]


def _info(name: str, *, last: float = 0, docs=(), focused=False) -> bridge.BridgeInfo:
    return bridge.BridgeInfo(Path(f"/b/{name}"), "1.6.0", focused, last, tuple(docs))


def test_select_the_only_window():
    one = _info("window-1.sock")
    assert bridge.select_bridge([one], "/t.fst") is one


def test_select_the_only_window_already_showing_the_trace():
    one = _info("window-1.sock", last=99)
    two = _info("window-2.sock", docs=["/t.fst"])
    assert bridge.select_bridge([one, two], "/t.fst") is two


def test_select_most_recently_focused_window():
    one = _info("window-1.sock", last=5)
    two = _info("window-2.sock", last=7)
    assert bridge.select_bridge([one, two], "/t.fst") is two


def test_select_recency_among_windows_showing_the_trace():
    one = _info("window-1.sock", last=5, docs=["/t.fst"])
    two = _info("window-2.sock", last=9)
    three = _info("window-3.sock", last=7, docs=["/t.fst"])
    assert bridge.select_bridge([one, two, three], "/t.fst") is three


@pytest.mark.parametrize("last", [(0, 0), (4, 4)], ids=["never-focused", "tie"])
def test_select_refuses_to_guess(last):
    bridges = [_info("window-1.sock", last=last[0]), _info("window-2.sock", last=last[1])]
    with pytest.raises(bridge.BridgeAmbiguousError, match="2 VS Code windows"):
        bridge.select_bridge(bridges, "/t.fst")


def test_select_without_bridges_is_unavailable():
    with pytest.raises(bridge.BridgeUnavailableError):
        bridge.select_bridge([], "/t.fst")


def test_bridge_dir_follows_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert bridge.bridge_dir() == tmp_path / ".booley" / "vaporview-bridge"
    assert os.fspath(bridge.bridge_dir(Path("/h"))) == "/h/.booley/vaporview-bridge"
