"""Tests for `bwave gui` — the Waveform Viewer wrapper verb (ADR 0040/0035).

Three layers:
  - trace resolution / format gate (shared _resolve_trace, unchanged by 0035)
  - bare open: WCP focus/open when a viewer listens, editor-CLI fallback else
  - scoped open (--signals/--time/--cursor/--append): drives a fake VaporView
    WCP server; the Rust binary is faked with canned JSON envelopes.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.bwave.cli import SESSION_FILE
from tests.bwave.test_wcp import FakeWcpServer
from tests.conftest import MINIMAL_FST_BYTES

BOOLEY_ROOT = Path(__file__).resolve().parent.parent.parent

# `open` never reads the trace contents (the viewer does), but discovery does
# check the store is real — header plus at least one value-change block, so a
# header-only stub does not read as a queryable trace.
_FAKE_TRACE_BYTES = MINIMAL_FST_BYTES


def _dead_port() -> int:
    """An ephemeral port with nothing listening on it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture(autouse=True)
def _inside_session_runtime(monkeypatch):
    """bwave is container-only (ADR 0028); simulate the Session Runtime."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


@pytest.fixture(autouse=True)
def _no_wcp_server(monkeypatch):
    """Point the WCP probe at a dead port by default.

    Bare-open tests must exercise the editor-CLI fallback deterministically
    even when a real VaporView happens to listen on 54322 on the dev host;
    WCP tests override this with their fake server's port.
    """
    monkeypatch.setenv("BOOLEY_WCP_PORT", str(_dead_port()))


@pytest.fixture(autouse=True)
def _clean_session():
    """Remove session file before and after each test."""
    SESSION_FILE.unlink(missing_ok=True)
    yield
    SESSION_FILE.unlink(missing_ok=True)


def _write_sessions(entries: dict) -> None:
    from booley.bwave import cli as bwave

    bwave.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    bwave.SESSION_FILE.write_text(json.dumps(entries, indent=2) + "\n")


def _make_entry(trace: str, markers: dict | None = None) -> dict:
    entry = {"trace": trace, "registered_at": datetime.now(UTC).isoformat()}
    if markers:
        entry["markers"] = markers
    return entry


def _make_trace(tmp_path: Path, name: str = "trace.fst") -> Path:
    trace = tmp_path / name
    trace.write_bytes(_FAKE_TRACE_BYTES)
    return trace


def _gui(target: str | None = None, **kwargs) -> None:
    from booley.bwave import cli as bwave

    ns = argparse.Namespace(
        target=target,
        signals=kwargs.pop("signals", []),
        time=kwargs.pop("time", None),
        cursor=kwargs.pop("cursor", None),
        append=kwargs.pop("append", False),
        no_clock=kwargs.pop("no_clock", False),
        max_signals=kwargs.pop("max_signals", None),
    )
    assert not kwargs, f"unknown open args: {kwargs}"
    bwave.cmd_gui(ns)


def _probe_finds(monkeypatch, present: set[str]) -> None:
    """Pretend exactly *present* editor CLIs resolve on PATH, VaporView installed.

    Stubbing `_extension_missing` keeps these tests from probing a real host
    `code --list-extensions`; the probe itself is tested separately below.
    """
    from booley.bwave import cli as bwave

    monkeypatch.setattr(
        bwave.shutil,
        "which",
        lambda name: name if name in present else None,
    )
    monkeypatch.setattr(bwave, "_extension_missing", lambda cli: False)


@pytest.fixture()
def launched(monkeypatch):
    """Stub `code` onto PATH and record successful _launch_viewer calls."""
    from booley.bwave import cli as bwave

    _probe_finds(monkeypatch, {"code"})
    calls: list[list[str]] = []
    monkeypatch.setattr(
        bwave,
        "_launch_viewer",
        lambda cli, trace: calls.append([cli, trace]) or 0,
    )
    return calls


# ── resolution (reuses _resolve_trace; keep lean) ─────────────────────────


def test_gui_alias_resolves_registered_trace(tmp_path, launched):
    trace = _make_trace(tmp_path)
    _write_sessions({"dut": _make_entry(str(trace))})

    _gui("@dut")

    assert launched == [["code", str(trace.resolve())]]


def test_gui_unknown_alias_errors(tmp_path, launched):
    _write_sessions({"dut": _make_entry(str(_make_trace(tmp_path)))})

    with pytest.raises(SystemExit) as exc:
        _gui("@ghost")

    assert "No registered alias" in str(exc.value)
    assert launched == []


def test_gui_bare_uses_session_default(tmp_path, launched):
    trace = _make_trace(tmp_path)
    _write_sessions({"_last": _make_entry(str(trace))})

    _gui()

    assert launched == [["code", str(trace.resolve())]]


def test_gui_no_session_errors(launched):
    with pytest.raises(SystemExit) as exc:
        _gui()

    assert "register" in str(exc.value)
    assert launched == []


def test_gui_explicit_path_wins_over_session(tmp_path, launched):
    session_trace = _make_trace(tmp_path, "session.fst")
    explicit = _make_trace(tmp_path, "explicit.fst")
    _write_sessions({"_last": _make_entry(str(session_trace))})

    _gui(str(explicit))

    assert launched == [["code", str(explicit.resolve())]]


def test_gui_nonexistent_path_errors_not_falls_back(tmp_path, launched):
    # `query` treats a nonexistent token as query args and uses the session
    # trace; `open` must refuse rather than silently open a different trace.
    _write_sessions({"_last": _make_entry(str(_make_trace(tmp_path)))})

    with pytest.raises(SystemExit) as exc:
        _gui(str(tmp_path / "missing.fst"))

    assert "not found" in str(exc.value)
    assert launched == []


def test_gui_empty_alias_errors_not_default_session(tmp_path, launched):
    # `bwave open @` must not silently resolve `_last` — an empty alias is
    # a typo. (Guard lives in shared _resolve_trace, so `query` gets it too.)
    _write_sessions({"_last": _make_entry(str(_make_trace(tmp_path)))})

    with pytest.raises(SystemExit) as exc:
        _gui("@")

    assert "Empty alias" in str(exc.value)
    assert launched == []


def test_gui_directory_discovers_trace(tmp_path, launched):
    # Users type `bwave open SIM_DIR` by analogy with `bwave register
    # SIM_DIR` — discover the trace instead of opening a folder window.
    sim_dir = tmp_path / "work"
    sim_dir.mkdir()
    trace = _make_trace(sim_dir)

    _gui(str(sim_dir))

    assert launched == [["code", str(trace.resolve())]]


def test_gui_directory_without_trace_errors(tmp_path, launched):
    sim_dir = tmp_path / "empty_sim"
    sim_dir.mkdir()

    with pytest.raises(SystemExit) as exc:
        _gui(str(sim_dir))

    assert "No trace file found" in str(exc.value)
    assert launched == []


# ── format gate ────────────────────────────────────────────────────────────


def test_gui_legacy_bwave_rejected(tmp_path, launched, capsys):
    legacy = tmp_path / "trace.bwave"
    legacy.write_bytes(_FAKE_TRACE_BYTES)
    _write_sessions({"dut": _make_entry(str(legacy))})

    with pytest.raises(SystemExit) as exc:
        _gui("@dut")

    # Exit 2 matches the Rust binary's own legacy-.bwave rejection.
    assert exc.value.code == 2
    msg = capsys.readouterr().err
    assert "legacy .bwave" in msg
    assert "re-run the sim" in msg
    assert launched == []


def test_gui_legacy_bwave_gate_is_case_insensitive(tmp_path, launched, capsys):
    # A case-insensitive host FS resolves TRACE.BWAVE to a real .bwave file;
    # the gate must not be fooled by suffix casing.
    legacy = tmp_path / "TRACE.BWAVE"
    legacy.write_bytes(_FAKE_TRACE_BYTES)
    _write_sessions({"dut": _make_entry(str(legacy))})

    with pytest.raises(SystemExit) as exc:
        _gui("@dut")

    assert exc.value.code == 2
    assert "legacy .bwave" in capsys.readouterr().err
    assert launched == []


@pytest.mark.parametrize("suffix", [".fst", ".vcd"])
def test_gui_fst_and_vcd_pass_through(tmp_path, launched, suffix):
    trace = _make_trace(tmp_path, f"trace{suffix}")
    _write_sessions({"_last": _make_entry(str(trace))})

    _gui()

    assert launched == [["code", str(trace.resolve())]]


# ── bare open: editor-CLI fallback (no WCP server listening) ────────────────


def _seed_default(tmp_path) -> Path:
    trace = _make_trace(tmp_path)
    _write_sessions({"_last": _make_entry(str(trace))})
    return trace


def test_probe_uses_code_when_present(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    trace = _seed_default(tmp_path)
    calls = []
    _probe_finds(monkeypatch, {"code"})
    monkeypatch.setattr(bwave, "_launch_viewer", lambda c, t: calls.append([c, t]) or 0)

    _gui()

    assert calls == [["code", str(trace.resolve())]]


def test_probe_falls_back_to_cursor(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    trace = _seed_default(tmp_path)
    calls = []
    _probe_finds(monkeypatch, {"cursor"})
    monkeypatch.setattr(bwave, "_launch_viewer", lambda c, t: calls.append([c, t]) or 0)

    _gui()

    assert calls == [["cursor", str(trace.resolve())]]


def test_probe_prefers_code_over_cursor(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    trace = _seed_default(tmp_path)
    calls = []
    _probe_finds(monkeypatch, {"code", "cursor"})
    monkeypatch.setattr(bwave, "_launch_viewer", lambda c, t: calls.append([c, t]) or 0)

    _gui()

    assert calls == [["code", str(trace.resolve())]]


def test_no_viewer_cli_prints_path_and_hint(tmp_path, monkeypatch):
    trace = _seed_default(tmp_path)
    _probe_finds(monkeypatch, set())

    with pytest.raises(SystemExit) as exc:
        _gui()

    msg = str(exc.value)
    assert str(trace.resolve()) in msg
    assert "lramseyer.vaporview" in msg
    assert exc.value.code  # nonzero exit


def test_failed_launch_prints_same_fallback(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    trace = _seed_default(tmp_path)
    _probe_finds(monkeypatch, {"code"})
    monkeypatch.setattr(bwave, "_launch_viewer", lambda c, t: 1)

    with pytest.raises(SystemExit) as exc:
        _gui()

    msg = str(exc.value)
    assert str(trace.resolve()) in msg
    assert "lramseyer.vaporview" in msg
    assert exc.value.code


def test_launch_viewer_missing_cli_is_nonzero(tmp_path):
    from booley.bwave import cli as bwave

    rc = bwave._launch_viewer(str(tmp_path / "no-such-editor-cli"), "trace.fst")

    assert rc != 0


# ── extension probe (pre-rebuild containers) ────────────────────────────────
#
# The editor CLI exits 0 opening a trace even without the VaporView extension,
# so the fallback path probes `--list-extensions` before launching.


def _fake_list_extensions(monkeypatch, *, rc: int = 0, stdout: str = "") -> None:
    from booley.bwave import cli as bwave

    monkeypatch.setattr(
        bwave.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=""),
    )


def test_extension_missing_true_when_absent(monkeypatch):
    from booley.bwave import cli as bwave

    _fake_list_extensions(monkeypatch, stdout="ms-python.python\n")

    assert bwave._extension_missing("code") is True


def test_extension_missing_false_when_listed_any_case(monkeypatch):
    from booley.bwave import cli as bwave

    _fake_list_extensions(
        monkeypatch,
        stdout="ms-python.python\nLramseyer.VaporView\n",
    )

    assert bwave._extension_missing("code") is False


def test_extension_missing_false_on_probe_failure(monkeypatch):
    # Nonzero probe exit means "unknown", not "missing" — never block a
    # launch that might work.
    from booley.bwave import cli as bwave

    _fake_list_extensions(monkeypatch, rc=1)

    assert bwave._extension_missing("code") is False


def test_extension_missing_false_on_oserror(tmp_path):
    from booley.bwave import cli as bwave

    assert bwave._extension_missing(str(tmp_path / "no-such-editor-cli")) is False


def test_missing_extension_prints_hint_and_skips_launch(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    trace = _seed_default(tmp_path)
    monkeypatch.setattr(
        bwave.shutil,
        "which",
        lambda name: name if name == "code" else None,
    )
    monkeypatch.setattr(bwave, "_extension_missing", lambda cli: True)
    calls = []
    monkeypatch.setattr(bwave, "_launch_viewer", lambda c, t: calls.append([c, t]) or 0)

    with pytest.raises(SystemExit) as exc:
        _gui()

    msg = str(exc.value)
    assert str(trace.resolve()) in msg
    assert f"code --install-extension {bwave._VAPORVIEW_EXTENSION}" in msg
    assert exc.value.code
    assert calls == []  # a raw-binary tab helps nobody — don't launch


def test_missing_extension_hint_names_the_probed_cli(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    _seed_default(tmp_path)
    monkeypatch.setattr(
        bwave.shutil,
        "which",
        lambda name: name if name == "cursor" else None,
    )
    monkeypatch.setattr(bwave, "_extension_missing", lambda cli: True)

    with pytest.raises(SystemExit) as exc:
        _gui()

    assert f"cursor --install-extension {bwave._VAPORVIEW_EXTENSION}" in str(exc.value)


# ── bare open over WCP ──────────────────────────────────────────────────────


def _use_server(monkeypatch, srv: FakeWcpServer) -> None:
    monkeypatch.setenv("BOOLEY_WCP_PORT", str(srv.port))


def test_bare_open_uses_wcp_when_reachable(tmp_path, monkeypatch, launched, capsys):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    event = {"type": "event", "event": "waveform_loaded", "uri": uri}
    with FakeWcpServer(
        responses={"get_open_documents": {"documents": [], "last_active_document": None}},
        events_after={"open_document": event},
    ) as srv:
        _use_server(monkeypatch, srv)

        _gui()

    assert srv.method_calls("open_document") == [{"uri": uri}]
    assert srv.method_calls("add_signal") == []  # bare open keeps the default view
    assert launched == []  # WCP path never launches the editor CLI
    assert "Opened in the Waveform Viewer" in capsys.readouterr().out


def test_bare_open_focuses_already_open_document(tmp_path, monkeypatch, launched, capsys):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with FakeWcpServer(
        responses={"get_open_documents": {"documents": [uri], "last_active_document": uri}},
    ) as srv:
        _use_server(monkeypatch, srv)

        _gui()

    assert srv.method_calls("open_document") == []
    assert launched == []
    assert "Already open" in capsys.readouterr().out


# ── scoped open (--signals / --time / --cursor / --append) ──────────────────
#
# The Rust binary is faked at the subprocess seam with canned JSON envelopes:
# list → signal paths, value --at TOKEN → target_tick, stats → total_ticks.

# What `bwave list` prints: (name, width). The vector is the crux of this
# fixture — its name carries the bit range, VaporView's netlist does not.
_CANNED_SIGNALS = [
    ("tb.dut.fifo.full", 1),
    ("tb.dut.fifo.empty", 1),
    ("tb.dut.fifo.count[3:0]", 4),
]
# The viewer's netlist: BARE instance paths, plus a couple of items standing in
# for the ones VaporView auto-populates on a fresh open.
_CANNED_NETLIST = [
    "tb.dut.fifo.full",
    "tb.dut.fifo.empty",
    "tb.dut.fifo.count",
    "tb.clk",
    "tb.rst_n",
]
# token → tick; the fake uses a 10-tick clock and a 1-ps file timescale.
_TICKS_PER_CYCLE = 10
_TICKS_PER_NS = 1000


def _fake_async_target_tick(token: str) -> int:
    if token.endswith("ps"):
        return int(token[:-2])
    if token.endswith("ns"):
        return int(token[:-2]) * _TICKS_PER_NS
    if token.endswith("us"):
        return int(token[:-2]) * _TICKS_PER_NS * 1000
    if token.endswith("ms"):
        return int(token[:-2]) * _TICKS_PER_NS * 1_000_000
    if token.endswith("t"):
        return int(token[:-1])
    raise AssertionError(f"unexpected async time token: {token}")


def _list_row(sig) -> dict:
    """One `bwave list` JSON row; a bare string is shorthand for a 1-bit signal."""
    name, width = sig if isinstance(sig, tuple) else (sig, 1)
    return {"name": name, "width": width}


class _FakeViewer:
    """VaporView's netlist + displayed list, modeled the way the extension is.

    Netlist items are keyed by their BARE instance path — a vector's bit range
    lives in msb/lsb, never in the name (see the extension's findChild()). And
    `add_signal` acks `{"success": true}` whether the lookup hits or misses,
    which is precisely how bracketed vector paths used to vanish in silence.
    """

    def __init__(self, netlist: list[str], displayed=()):
        self.ids = {path: i for i, path in enumerate(netlist, start=1)}
        self.paths = {i: path for path, i in self.ids.items()}
        self.displayed = [self.ids[p] for p in displayed]

    def shown(self) -> list[str]:
        return [self.paths[i] for i in self.displayed]

    def add_signal(self, params) -> dict:
        netlist_id = self.ids.get(params.get("instance_path"))
        if netlist_id is not None:
            self.displayed.append(netlist_id)
        return {"success": True}  # acks the miss too — the bug in one line

    def remove_items(self, params) -> dict:
        for netlist_id in params.get("ids", []):
            if netlist_id in self.displayed:
                self.displayed.remove(netlist_id)
        return {"success": True}

    def get_item_list(self, _params) -> dict:
        return {"type": "response", "command": "get_item_list", "ids": list(self.displayed)}

    def get_item_info(self, params) -> dict:
        return {
            "type": "response",
            "command": "get_item_info",
            "results": [
                {"name": self.paths[i], "type": "wire", "id": i}
                for i in params.get("ids", [])
                if i in self.paths
            ],
        }


@pytest.fixture()
def fake_rust(monkeypatch):
    """Fake the Rust bwave binary; returns the recorded query argv list."""
    from booley.bwave import cli as bwave

    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-rust"])
    # The viewer model answers synchronously, so only a genuine drop waits out
    # the settle poll; keep that wait short enough not to drag the suite.
    monkeypatch.setattr(bwave, "_VIEW_SETTLE_TIMEOUT", 0.3)
    state = {"signals": list(_CANNED_SIGNALS), "total_ticks": 100_000, "clock": "tb.clk"}
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd[0] != "bwave-rust":  # editor-CLI probes etc. keep real behavior
            raise AssertionError(f"unexpected subprocess in scoped test: {cmd}")
        calls.append(cmd)
        sub = cmd[1]
        if sub == "list":
            # `list` carries the trace header too: the clock the Rust binary
            # detected (gui makes it row 1) and the end of the trace. Both used
            # to come off `stats`, which walks every transition and times out on
            # a large store.
            data = {
                "scope_prefix": "",
                "clock": state["clock"],
                "total_ticks": state["total_ticks"],
                "signals": [_list_row(s) for s in state["signals"]],
            }
        elif sub == "value":
            token = cmd[cmd.index("--at") + 1]
            async_mode = "--async" in cmd
            if async_mode:
                target_tick = _fake_async_target_tick(token)
            else:
                cycles = int(token[:-1]) if token.endswith("c") else int(token)
                target_tick = cycles * _TICKS_PER_CYCLE
            data = {
                "scope_prefix": "",
                "mode": "async" if async_mode else "sync",
                "at": target_tick if async_mode else cycles,
                "at_unit": "tick" if async_mode else "cycle",
                "target_tick": target_tick,
                "time_label": token,
                "signals": [],
            }
        elif sub == "stats":
            data = {
                "simulation_ns": 1000,
                "total_ticks": state["total_ticks"],
                "total_cycles": state["total_ticks"] // _TICKS_PER_CYCLE,
                "clock_period_ns": 10,
                "signals": [],
            }
        else:
            raise AssertionError(f"unexpected bwave subcommand: {cmd}")
        envelope = {"command": sub, "data": data, "warnings": []}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr(bwave.subprocess, "run", fake_run)
    calls_and_state = state
    calls_and_state["calls"] = calls
    return calls_and_state


def _loaded_server(
    uri: str,
    *,
    open_docs: list[str] | None = None,
    netlist: list[str] | None = None,
    displayed=(),
):
    """Fake VaporView preloaded for a scoped open against *uri*."""
    viewer = _FakeViewer(_CANNED_NETLIST if netlist is None else netlist, displayed)
    event = {"type": "event", "event": "waveform_loaded", "uri": uri}
    srv = FakeWcpServer(
        responses={
            "get_open_documents": {
                "documents": open_docs or [],
                "last_active_document": (open_docs or [None])[0],
            },
            "get_item_list": viewer.get_item_list,
            "get_item_info": viewer.get_item_info,
            "add_signal": viewer.add_signal,
            "remove_items": viewer.remove_items,
        },
        events_after={"open_document": event},
    )
    srv.viewer = viewer
    return srv


def test_scoped_gui_happy_path(tmp_path, monkeypatch, fake_rust, capsys):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    # VaporView auto-populates a fresh document (openFile maxSignals default),
    # so a replace-mode view must clear items even right after open_document.
    with _loaded_server(uri, displayed=["tb.rst_n"]) as srv:
        stale = [srv.viewer.ids["tb.rst_n"]]
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.*"], time="100c:200c")

    assert srv.method_calls("open_document") == [{"uri": uri}]
    assert srv.method_calls("remove_items") == [{"ids": stale, "uri": uri}]
    assert srv.method_calls("add_signal") == [
        {"instance_path": "tb.clk", "uri": uri},  # clock is row 1, unasked-for
        {"instance_path": "tb.dut.fifo.full", "uri": uri},
        {"instance_path": "tb.dut.fifo.empty", "uri": uri},
        {"instance_path": "tb.dut.fifo.count", "uri": uri, "msb": 3, "lsb": 0},
    ]
    assert srv.method_calls("set_viewport_range") == [
        {"start": 100 * _TICKS_PER_CYCLE, "end": 200 * _TICKS_PER_CYCLE, "uri": uri}
    ]
    # The range is bracketed by the two markers, so the viewer reports its span.
    assert srv.method_calls("set_marker") == [
        {"time": 100 * _TICKS_PER_CYCLE, "uri": uri, "marker_type": 0},  # START
        {"time": 200 * _TICKS_PER_CYCLE, "uri": uri, "marker_type": 1},  # END
    ]
    out = capsys.readouterr().out
    assert "tb.dut.fifo.full" in out and "viewport: 1000..2000 ticks" in out
    assert "START marker: 1000 ticks" in out and "END marker: 2000 ticks" in out


def test_multibit_vector_reaches_the_viewer(tmp_path, monkeypatch, fake_rust, capsys):
    """Regression: every multi-bit signal used to be dropped in silence.

    `bwave list` names a 4-bit register `count[3:0]`; VaporView's netlist keys
    it as `count` with msb/lsb, so the bracketed path resolved to nothing —
    and add_signal acked the miss as success, so only 1-bit rows ever rendered
    while stdout cheerfully listed the buses too.
    """
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.*"])

    assert {"instance_path": "tb.dut.fifo.count", "uri": uri, "msb": 3, "lsb": 0} in (
        srv.method_calls("add_signal")
    )
    # The viewer really holds the vector — not just an ack that says so.
    assert srv.viewer.shown() == [
        "tb.clk",
        "tb.dut.fifo.full",
        "tb.dut.fifo.empty",
        "tb.dut.fifo.count",
    ]
    captured = capsys.readouterr()
    # Reported under the name the user typed and `bwave list` prints.
    assert "tb.dut.fifo.count[3:0]" in captured.out
    assert "WARNING" not in captured.err


# ── clock-first / markers ───────────────────────────────────────────────────


def test_new_view_gets_the_clock_as_row_one(tmp_path, monkeypatch, fake_rust, capsys):
    """A waveform without its clock is unreadable — you cannot count cycles."""
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.full"])

    assert srv.viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]
    # It rides on the `list` call gui already makes — never a `stats` walk,
    # which on a large store runs for minutes and blows the query timeout.
    assert [c[1] for c in fake_rust["calls"]] == ["list"]
    assert "tb.clk" in capsys.readouterr().out


def test_no_clock_opts_out(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.full"], no_clock=True)

    assert srv.viewer.shown() == ["tb.dut.fifo.full"]


def test_append_does_not_re_add_the_clock(tmp_path, monkeypatch, fake_rust):
    """--append builds on a view that already has its clock row."""
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    with _loaded_server(uri, open_docs=[uri], displayed=["tb.clk"]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.full"], append=True)

    assert srv.method_calls("add_signal") == [{"instance_path": "tb.dut.fifo.full", "uri": uri}]
    assert srv.viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]


def test_append_does_not_duplicate_an_already_displayed_signal(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    with _loaded_server(uri, open_docs=[uri], displayed=["tb.clk", "tb.dut.fifo.full"]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.full"], append=True)

    assert srv.method_calls("add_signal") == []
    assert srv.viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]


def test_clock_is_not_duplicated_when_the_glob_already_matched_it(
    tmp_path, monkeypatch, fake_rust
):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["signals"] = [("tb.clk", 1), ("tb.dut.fifo.full", 1)]
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.*"])

    assert srv.method_calls("add_signal") == [
        {"instance_path": "tb.clk", "uri": uri},
        {"instance_path": "tb.dut.fifo.full", "uri": uri},
    ]


def test_async_trace_without_a_clock_still_opens(tmp_path, monkeypatch, fake_rust):
    """No clock detected (async trace) → no row 1, no crash."""
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["clock"] = None
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.full"])

    assert srv.viewer.shown() == ["tb.dut.fifo.full"]


def test_gui_never_walks_the_trace_with_stats(tmp_path, monkeypatch, fake_rust):
    """`stats` is minutes on a large store — gui must not touch it, ever.

    An open-ended range used to resolve its end through `stats`, which walks
    every transition of every signal: on a 440 MB store that ran past the 120 s
    query timeout, so `--time 100c:` hung instead of opening. Both the trace end
    and the clock now ride on `list`.
    """
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.*"], time="100c:")

    assert "stats" not in [c[1] for c in fake_rust["calls"]]
    assert srv.method_calls("set_viewport_range") == [
        {"start": 1000, "end": fake_rust["total_ticks"], "uri": uri}
    ]


def test_cursor_moves_start_marker_and_end_stays(tmp_path, monkeypatch, fake_rust):
    """--cursor relocates START (main) inside the range; END (alt) holds."""
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(time="100c:200c", cursor="120c", append=True)

    assert srv.method_calls("set_marker") == [
        {"time": 1200, "uri": uri, "marker_type": 0},  # cursor wins START
        {"time": 2000, "uri": uri, "marker_type": 1},  # END unmoved
    ]


def test_cursor_without_a_range_is_a_lone_main_marker(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(cursor="120c", append=True)

    assert srv.method_calls("set_marker") == [{"time": 1200, "uri": uri, "marker_type": 0}]


def test_signals_the_viewer_drops_are_reported(tmp_path, monkeypatch, fake_rust, capsys):
    """No more echoing the request: a signal the viewer refuses gets a warning."""
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["signals"] = [("tb.dut.fifo.full", 1), ("tb.dut.ghost[7:0]", 8)]
    # `ghost` is in the trace but absent from the viewer's netlist.
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.*"])

    assert srv.viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]
    captured = capsys.readouterr()
    assert "tb.dut.fifo.full" in captured.out
    assert "tb.dut.ghost[7:0]" not in captured.out  # never claim what did not land
    assert "WARNING" in captured.err and "tb.dut.ghost[7:0]" in captured.err


def test_bracketed_identifier_falls_back_to_the_raw_name(tmp_path, monkeypatch, fake_rust, capsys):
    """A 1-bit `mem[0]` may be an array element, not a bit-select.

    The name alone cannot settle it, so the bare-path attempt is retried under
    the raw name when the viewer does not take it.
    """
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["signals"] = [("tb.dut.mem[0]", 1)]
    with _loaded_server(uri, open_docs=[uri], netlist=["tb.dut.mem[0]"]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.mem*"], no_clock=True)

    assert srv.method_calls("add_signal") == [
        {"instance_path": "tb.dut.mem", "uri": uri, "msb": 0, "lsb": 0},  # bit-select reading
        {"instance_path": "tb.dut.mem[0]", "uri": uri},  # …refused, so: identifier
    ]
    assert srv.viewer.shown() == ["tb.dut.mem[0]"]
    captured = capsys.readouterr()
    assert "tb.dut.mem[0]" in captured.out
    assert "WARNING" not in captured.err


def test_wide_bracketed_identifier_is_never_split(tmp_path, monkeypatch, fake_rust):
    """A wider-than-1-bit `mem[0]` can only be an identifier — no retry needed."""
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["signals"] = [("tb.dut.mem[0]", 8)]
    with _loaded_server(uri, open_docs=[uri], netlist=["tb.dut.mem[0]"]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.mem*"], no_clock=True)

    assert srv.method_calls("add_signal") == [{"instance_path": "tb.dut.mem[0]", "uri": uri}]
    assert srv.viewer.shown() == ["tb.dut.mem[0]"]


def test_append_adds_without_clearing(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri], displayed=["tb.clk"]) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.*"], append=True)

    assert srv.method_calls("open_document") == []  # already open
    assert srv.method_calls("remove_items") == []  # append never clears
    assert len(srv.method_calls("add_signal")) == 3
    assert srv.viewer.shown() == [
        "tb.clk",  # kept
        "tb.dut.fifo.full",
        "tb.dut.fifo.empty",
        "tb.dut.fifo.count",
    ]


def test_scoped_replace_on_already_open_document(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri], displayed=["tb.clk", "tb.rst_n"]) as srv:
        stale = [srv.viewer.ids["tb.clk"], srv.viewer.ids["tb.rst_n"]]
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.fifo.*"])

    assert srv.method_calls("open_document") == []
    assert srv.method_calls("remove_items") == [{"ids": stale, "uri": uri}]


def test_time_only_moves_viewport_without_signals(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(time="100c:200c", append=True)

    assert srv.method_calls("add_signal") == []
    assert srv.method_calls("remove_items") == []
    assert srv.method_calls("set_viewport_range") == [{"start": 1000, "end": 2000, "uri": uri}]


def test_tick_range_is_exact_on_clockless_trace(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["clock"] = None
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(time="10000t:20000t", append=True, no_clock=True)

    value_calls = [call for call in fake_rust["calls"] if call[1] == "value"]
    assert all("--async" in call for call in value_calls)
    assert srv.method_calls("set_viewport_range") == [{"start": 10000, "end": 20000, "uri": uri}]


def test_physical_range_works_on_clockless_trace(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    fake_rust["clock"] = None
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(time="100ns:200ns", append=True, no_clock=True)

    value_calls = [call for call in fake_rust["calls"] if call[1] == "value"]
    assert all("--async" in call for call in value_calls)
    assert srv.method_calls("set_viewport_range") == [{"start": 100000, "end": 200000, "uri": uri}]


def test_physical_time_range_is_not_shifted_by_sync_origin(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(time="50ns:150ns", append=True)

    value_calls = [call for call in fake_rust["calls"] if call[1] == "value"]
    assert all("--async" in call for call in value_calls)
    assert srv.method_calls("set_viewport_range") == [{"start": 50000, "end": 150000, "uri": uri}]


def test_scoped_open_without_wcp_hard_errors(tmp_path, monkeypatch, fake_rust, launched):
    # _no_wcp_server autouse fixture points at a dead port.
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.*"])

    msg = str(exc.value)
    assert "lramseyer.vaporview" in msg
    assert "vaporview.wcp.enabled" in msg
    assert "BOOLEY_WCP_PORT" in msg
    assert exc.value.code
    assert launched == []  # never degrade a scoped request to a bare launch


def test_glob_without_match_errors_before_wcp(tmp_path, monkeypatch, fake_rust, capsys):
    _seed_default(tmp_path)
    fake_rust["signals"] = []

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.nope.*"])

    # Caller-input class: exit 2, message on stderr — same contract as the
    # Rust binary's total-miss (see _bwave_contract).
    assert exc.value.code == 2
    assert "no signals match" in capsys.readouterr().err


def test_signal_cap_is_a_hard_error(tmp_path, monkeypatch, fake_rust, capsys):
    _seed_default(tmp_path)
    fake_rust["signals"] = [f"tb.dut.bus.bit{i}" for i in range(65)]

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.bus.*"])

    assert exc.value.code == 2
    msg = capsys.readouterr().err
    assert "65 signals" in msg and "--max-signals" in msg


def test_max_signals_raises_the_cap(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    bus = [f"tb.dut.bus.bit{i}" for i in range(65)]
    fake_rust["signals"] = bus
    with _loaded_server(uri, open_docs=[uri], netlist=bus) as srv:
        _use_server(monkeypatch, srv)

        _gui(signals=["tb.dut.bus.*"], max_signals=100, no_clock=True)

    assert len(srv.method_calls("add_signal")) == 65
    assert srv.viewer.shown() == bus


def test_radix_suffix_in_signals_rejected(tmp_path, monkeypatch, fake_rust, capsys):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.data%d"])

    assert exc.value.code == 2
    assert "%RADIX" in capsys.readouterr().err


def test_marker_names_resolve_in_time_range(tmp_path, monkeypatch, fake_rust):
    trace = _make_trace(tmp_path)
    _write_sessions({"dut": _make_entry(str(trace), markers={"overflow": 1200})})
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui("@dut", time="overflow:1400c")

    # The marker resolves to its cycle with an explicit 'c' suffix before
    # hitting the Rust converter.
    value_tokens = [c[c.index("--at") + 1] for c in fake_rust["calls"] if c[1] == "value"]
    assert value_tokens == ["1200c", "1400c"]
    assert srv.method_calls("set_viewport_range") == [
        {"start": 1200 * _TICKS_PER_CYCLE, "end": 1400 * _TICKS_PER_CYCLE, "uri": uri}
    ]


def test_gui_ended_range_uses_stats_total_ticks(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    uri = trace.resolve().as_uri()
    with _loaded_server(uri, open_docs=[uri]) as srv:
        _use_server(monkeypatch, srv)

        _gui(time="100c:", append=True)

    assert srv.method_calls("set_viewport_range") == [
        {"start": 1000, "end": fake_rust["total_ticks"], "uri": uri}
    ]


def test_empty_time_range_rejected(tmp_path, monkeypatch, fake_rust):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(time="200c:100c", append=True)

    assert "empty --time range" in str(exc.value)


def test_time_without_colon_rejected(tmp_path, monkeypatch, fake_rust):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(time="150c", append=True)

    assert "START:END" in str(exc.value)


def test_append_with_nothing_to_do_errors(tmp_path, monkeypatch, fake_rust):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(append=True)

    assert "--append needs" in str(exc.value)


# ── inner-binary errors reach the user (F-47) ───────────────────────────────

# What the Rust binary actually does on an out-of-range --at-time: writes a
# legible ERROR + did-you-mean HINT to stderr, emits NO json, and exits 0.
_OUT_OF_RANGE_STDERR = (
    "ERROR: --at-time 160000 (cycle) is beyond simulation range (sim length: 15 cycles)\n"
    "HINT: did you mean --at-time 160000 with --async? In sync mode, --at-time "
    "expects a cycle number (0..15).\n"
)


@pytest.fixture()
def failing_rust(monkeypatch):
    """Fake the Rust binary as a failure; the test picks rc/stdout/stderr."""
    from booley.bwave import cli as bwave

    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-rust"])
    outcome = {"rc": 0, "stdout": "", "stderr": _OUT_OF_RANGE_STDERR}

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, outcome["rc"], stdout=outcome["stdout"], stderr=outcome["stderr"]
        )

    monkeypatch.setattr(bwave.subprocess, "run", fake_run)
    return outcome


def test_gui_surfaces_the_inner_preflight_error(tmp_path, failing_rust):
    """gui's --time preflight must relay the binary's message, not its plumbing."""
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(time="160000c:160100c", append=True)

    msg = str(exc.value)
    assert "beyond simulation range (sim length: 15 cycles)" in msg
    assert "HINT: did you mean" in msg  # the hint the user actually needed
    assert "non-JSON" not in msg  # plumbing stays on the debug channel
    assert "ERROR: ERROR" not in msg  # and it does not stutter the prefix


def test_nonzero_exit_message_is_not_double_prefixed(tmp_path, failing_rust):
    _seed_default(tmp_path)
    failing_rust["rc"] = 2

    with pytest.raises(SystemExit) as exc:
        _gui(time="160000c:160100c", append=True)

    assert str(exc.value).startswith("ERROR: --at-time 160000")


def test_plain_text_failure_on_stdout_is_surfaced(tmp_path, failing_rust):
    """No stderr, non-JSON stdout: stdout is the message."""
    _seed_default(tmp_path)
    failing_rust["stderr"] = ""
    failing_rust["stdout"] = "could not open store: bad magic"

    with pytest.raises(SystemExit) as exc:
        _gui(time="100c:200c", append=True)

    assert "could not open store: bad magic" in str(exc.value)


def test_silent_failure_still_names_the_plumbing(tmp_path, failing_rust):
    """Nothing to relay -> the plumbing detail is all the user can be given."""
    _seed_default(tmp_path)
    failing_rust["stderr"] = ""

    with pytest.raises(SystemExit) as exc:
        _gui(time="100c:200c", append=True)

    msg = str(exc.value)
    assert "non-JSON output" in msg and "value" in msg


def test_missing_data_object_relays_the_binary_message(tmp_path, failing_rust):
    """Well-formed JSON without `data`: still the binary's own words, not ours."""
    _seed_default(tmp_path)
    failing_rust["stdout"] = json.dumps({"command": "value", "warnings": []})
    failing_rust["stderr"] = "ERROR: store has no signals\n"

    with pytest.raises(SystemExit) as exc:
        _gui(time="100c:200c", append=True)

    assert str(exc.value) == "ERROR: store has no signals"


def test_long_inner_output_is_tail_capped(tmp_path, failing_rust):
    _seed_default(tmp_path)
    failing_rust["stderr"] = "x" * 20_000 + "\nERROR: the part that matters"

    with pytest.raises(SystemExit) as exc:
        _gui(time="100c:200c", append=True)

    msg = str(exc.value)
    assert "ERROR: the part that matters" in msg
    assert "truncated" in msg
    assert len(msg) < 5000


# ── name → (instance_path, msb, lsb) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "width", "expected"),
    [
        # Plain scalar: nothing to strip.
        ("tb.dut.clk", 1, ("tb.dut.clk", None, None, None)),
        # Vector: the range moves out of the name and into msb/lsb.
        ("tb.dut.state[3:0]", 4, ("tb.dut.state", 3, 0, None)),
        ("tb.dut.cnt[31:0]", 32, ("tb.dut.cnt", 31, 0, None)),
        # Descending/ascending both pass through as written.
        ("tb.dut.v[0:7]", 8, ("tb.dut.v", 0, 7, None)),
        # 1-bit single index: could be a bit-select OR an `mem[0]` identifier —
        # take the bit-select reading, keep the raw name as the fallback.
        ("tb.dut.mem[0]", 1, ("tb.dut.mem", 0, 0, "tb.dut.mem[0]")),
        # Wider than a bit: the bracket can only belong to the identifier.
        ("tb.dut.mem[0]", 8, ("tb.dut.mem[0]", None, None, None)),
        # Width unknown (older `bwave list` JSON): fall back to the ambiguous read.
        ("tb.dut.mem[2]", None, ("tb.dut.mem", 2, 2, "tb.dut.mem[2]")),
        # A bracket that is not a bit range is left alone.
        ("tb.dut.gen[i].q", 1, ("tb.dut.gen[i].q", None, None, None)),
        ("tb.dut.arr[a:b]", 1, ("tb.dut.arr[a:b]", None, None, None)),
    ],
)
def test_split_bit_range(name, width, expected):
    from booley.bwave import cli as bwave

    sig = bwave._split_bit_range(name, width)

    assert sig.bwave_name == name  # the user-facing name is never rewritten
    assert (sig.instance_path, sig.msb, sig.lsb, sig.fallback) == expected


# ── the `open` → `gui` rename ───────────────────────────────────────────────


def test_open_is_gone_and_says_so(monkeypatch, capsys):
    """`open` must not fall through to the query translator and die about globs."""
    from booley.bwave import cli as bwave

    monkeypatch.setattr("booley.runtime.runtime_context.inside_session_runtime", lambda: True)
    monkeypatch.setattr(sys, "argv", ["bwave", "open", "@dut", "-s", "tb.dut.fifo.*"])

    with pytest.raises(SystemExit) as exc:
        bwave.main()

    msg = str(exc.value)
    assert "`bwave open` is now `bwave gui`" in msg
    # …and hands back the exact command to rerun.
    assert "bwave gui @dut -s tb.dut.fifo.*" in msg


# ── venue guard ─────────────────────────────────────────────────────────────


def test_gui_is_container_only(monkeypatch, capsys):
    """Shared guard, but `open` is the most human-facing verb — pin it."""
    from booley.bwave import cli as bwave

    monkeypatch.setattr("booley.runtime.runtime_context.inside_session_runtime", lambda: False)
    monkeypatch.setattr(sys, "argv", ["bwave", "gui"])

    with pytest.raises(SystemExit) as exc:
        bwave.main()

    assert exc.value.code == 2
    assert "Session Runtime" in capsys.readouterr().err


# ── tier 2: subprocess smoke ────────────────────────────────────────────────


def test_gui_subprocess_smoke(tmp_path):
    """`python -m booley.bwave.cli gui @dut` end-to-end with a stub editor CLI.

    Covers the argv-routing seam (_KNOWN_COMMANDS, dispatch) that in-process
    Namespace tests bypass — an unrouted `open` would fall through to query
    and die on "No query mode specified".
    """
    trace = _make_trace(tmp_path)
    _write_sessions({"dut": _make_entry(str(trace))})

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    argv_log = tmp_path / "code_argv.txt"
    # The stub must answer the pre-launch `--list-extensions` probe with
    # VaporView's id, and log argv for the actual launch call.
    if sys.platform == "win32":
        stub = stub_dir / "code.cmd"
        stub.write_text(
            "@echo off\r\n"
            'if "%1"=="--list-extensions" (\r\n'
            "  echo lramseyer.vaporview\r\n"
            ") else (\r\n"
            f'  echo %* > "{argv_log}"\r\n'
            ")\r\n"
        )
    else:
        stub = stub_dir / "code"
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--list-extensions" ]; then\n'
            "  echo lramseyer.vaporview\n"
            "else\n"
            f'  echo "$@" > "{argv_log}"\n'
            "fi\n"
        )
        stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = str(stub_dir) + os.pathsep + env["PATH"]
    env["PYTHONPATH"] = str(BOOLEY_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["BOOLEY_CONTAINER"] = "1"
    env["BOOLEY_WCP_PORT"] = str(_dead_port())  # force the editor-CLI fallback

    r = subprocess.run(
        [sys.executable, "-m", "booley.bwave.cli", "gui", "@dut"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )

    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert r.stderr.strip() == ""
    assert argv_log.exists(), "stub `code` CLI was never invoked"
    assert str(trace.resolve()) in argv_log.read_text()
