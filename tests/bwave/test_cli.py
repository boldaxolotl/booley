"""Tests for `bwave gui` — the Waveform Viewer wrapper verb (ADR 0040/0078).

Three layers:
  - trace resolution / format gate (shared _resolve_trace)
  - bare open: the bridge opens/focuses when a window answers, editor-CLI
    fallback otherwise
  - scoped open (--signals/--time/--cursor/--append): drives a fake VaporView
    behind the bridge client; the Rust binary is faked with canned JSON
    envelopes. The bridge wire itself is covered by test_vaporview_bridge.py.
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.bwave import vaporview_bridge as bwave_bridge
from booley.bwave.cli import SESSION_FILE
from booley.runtime import vaporview
from tests.conftest import MINIMAL_FST_BYTES

BOOLEY_ROOT = Path(__file__).resolve().parent.parent.parent

# `open` never reads the trace contents (the viewer does), but discovery does
# check the store is real — header plus at least one value-change block, so a
# header-only stub does not read as a queryable trace.
_FAKE_TRACE_BYTES = MINIMAL_FST_BYTES


@pytest.fixture(autouse=True)
def _inside_session_runtime(monkeypatch):
    """bwave is container-only (ADR 0028); simulate the Sandbox."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


@pytest.fixture(autouse=True)
def _no_bridge(monkeypatch):
    """No attached VS Code window by default.

    Bare-open tests must exercise the editor-CLI fallback deterministically
    even when a real bridge happens to run on the dev host; viewer tests
    install their fake window explicitly.
    """
    monkeypatch.setattr(bwave_bridge, "discover", lambda home=None: [])


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
        groups=kwargs.pop("groups", []),
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
    from booley.runtime.vaporview import ExtensionState

    monkeypatch.setattr(
        bwave.shutil,
        "which",
        lambda name: name if name in present else None,
    )
    monkeypatch.setattr(bwave, "_extension_missing", lambda cli: False)
    monkeypatch.setattr(bwave, "_viewer_extension_state", lambda: ExtensionState.INSTALLED)


def test_gui_parser_accepts_repeatable_named_groups():
    from booley.bwave import cli as bwave

    args = bwave._build_parser().parse_args(
        [
            "gui",
            "@dut",
            "--group",
            "Request=tb.dut.req_*",
            "-g",
            "Response=tb.dut.resp_*",
        ]
    )

    assert args.groups == ["Request=tb.dut.req_*", "Response=tb.dut.resp_*"]


def test_gui_help_advertises_composable_radix_and_color_suffixes(capsys):
    from booley.bwave import cli as bwave

    with pytest.raises(SystemExit):
        bwave._build_parser().parse_args(["gui", "--help"])

    help_text = capsys.readouterr().out
    assert "GLOB[%RADIX][@COLOR]" in help_text
    assert "%b/%h/%d" in help_text
    assert "@red/@blue/@green" in help_text


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


# ── bare open: editor-CLI fallback (no attached window) ─────────────────────


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
    assert f"code --install-extension {vaporview.PINNED_EXTENSION}" in msg
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

    assert f"cursor --install-extension {vaporview.PINNED_EXTENSION}" in str(exc.value)


# ── bare open through the bridge ────────────────────────────────────────────


def test_bare_open_uses_the_bridge_when_a_window_answers(tmp_path, monkeypatch, launched, capsys):
    trace = _seed_default(tmp_path)
    viewer = _install_viewer(monkeypatch, open_docs=[])

    _gui()

    assert viewer.ops() == ["open"]  # bare open keeps VaporView's default view
    assert viewer.docs == {str(trace.resolve())}
    assert launched == []  # the bridge path never launches the editor CLI
    assert "Opened in the Waveform Viewer" in capsys.readouterr().out


def test_bare_open_focuses_already_open_document(tmp_path, monkeypatch, launched, capsys):
    trace = _seed_default(tmp_path)
    _install_viewer(monkeypatch, open_docs=[str(trace.resolve())])

    _gui()

    assert launched == []
    assert "Already open" in capsys.readouterr().out


def test_bare_open_without_vaporview_in_the_window_uses_the_editor_cli(
    tmp_path, monkeypatch, launched
):
    trace = _seed_default(tmp_path)
    viewer = _install_viewer(monkeypatch, open_docs=[], version=None)

    _gui()

    assert viewer.ops() == []
    assert launched == [["code", str(trace.resolve())]]


def test_bare_open_with_ambiguous_windows_uses_the_editor_cli(tmp_path, monkeypatch, launched):
    from booley.bwave import cli as bwave

    trace = _seed_default(tmp_path)

    def ambiguous(_trace):
        raise bwave.vaporview_bridge.BridgeAmbiguousError("2 VS Code windows are attached")

    monkeypatch.setattr(bwave, "_select_viewer", ambiguous)

    _gui()

    assert launched == [["code", str(trace.resolve())]]


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


def _fake_signal_matches(name: str, pattern: str) -> bool:
    """Model B-Wave's public glob contract, including literal indices."""
    index = re.search(r"\[(?:\d+|\d+:\d+)\]$", pattern)
    meta_scan = pattern[: index.start()] if index else pattern
    wrapped = pattern if any(char in meta_scan for char in "*?[") else f"*{pattern}"
    original = wrapped
    if index:
        open_bracket = len(wrapped) - (len(pattern) - index.start())
        original = f"{wrapped[:open_bracket]}[[]{wrapped[open_bracket + 1 :]}"
    stripped_name = name.split("[", 1)[0]
    stripped_pattern = wrapped.split("[", 1)[0]
    return fnmatch.fnmatchcase(name, original) or fnmatch.fnmatchcase(
        stripped_name, stripped_pattern
    )


class _FakeViewer:
    """VaporView 1.6.0 behind the Booley bridge, modeled the way it behaves.

    - ``apply`` replaces every row; markers/viewport only when their keys are
      present (viewport needs both zoomRatio and scrollLeft).
    - Rows resolve by BARE instance path; an unresolved row stays on screen as
      a placeholder whose saved form lacks ``netlistId``.
    - Saved rows never carry msb/lsb, and always report a number format and a
      color index (VaporView's defaults when none was requested).
    - State readback lags an apply by ``lag`` reads, like the real webview.
    """

    def __init__(self, netlist: list[str], *, rows=(), open_docs=(), version="1.6.0"):
        self.ids = {path: i for i, path in enumerate(netlist, start=1)}
        self.version = version
        self.docs = set(open_docs)
        self.rows = [self._resolve(row) for row in rows]
        self.session = {
            "markerTime": 11,
            "altMarkerTime": 22,
            "displayTimeUnit": "ns",
            "zoomRatio": 1.5,
            "scrollLeft": 7,
        }
        self.calls: list[tuple[str, dict]] = []
        self.lag = 0
        self._pending = 0
        self._stale: dict | None = None
        self.after_apply = None  # hook(viewer, state) to model viewer faults
        self.fail_op: dict[str, str] = {}

    # -- modeled behavior ---------------------------------------------------
    def _resolve(self, row: dict) -> dict:
        if row.get("dataType") == "signal-group":
            return {
                "dataType": "signal-group",
                "groupName": row.get("groupName"),
                "collapseState": row.get("collapseState", 2),
                "children": [self._resolve(child) for child in row.get("children", [])],
            }
        if row.get("dataType") != "netlist-variable":
            return copy.deepcopy(row)
        saved = {
            "dataType": "netlist-variable",
            "name": row["name"],
            "numberFormat": row.get("numberFormat", "binary"),
            "colorIndex": row.get("colorIndex", 1),
        }
        if row["name"] in self.ids:
            saved["netlistId"] = self.ids[row["name"]]
        return saved

    def _snapshot(self) -> dict:
        return copy.deepcopy({**self.session, "displayedSignals": self.rows})

    # -- BridgeClient surface ---------------------------------------------
    def _record(self, op: str, **params) -> None:
        self.calls.append((op, copy.deepcopy(params)))
        if op in self.fail_op:
            raise bwave_bridge.BridgeRequestError(op, self.fail_op[op])

    def open(self, path: str) -> bool:
        self._record("open", path=path)
        opened = path not in self.docs
        self.docs.add(path)
        return opened

    def state(self, path: str) -> dict | None:
        if path not in self.docs:
            return None
        if self._pending:
            self._pending -= 1
            return copy.deepcopy(self._stale)
        return self._snapshot()

    def apply(self, path: str, state: dict) -> None:
        self._record("apply", path=path, state=state)
        if path not in self.docs:  # the real bridge refuses a closed document
            raise bwave_bridge.BridgeRequestError("apply", f"{path} is not open")
        self._stale, self._pending = self._snapshot(), self.lag
        self.rows = [self._resolve(row) for row in state["displayedSignals"]]
        for key in ("markerTime", "altMarkerTime", "displayTimeUnit"):
            if key in state:
                self.session[key] = state[key]
        if "zoomRatio" in state and "scrollLeft" in state:
            self.session["zoomRatio"] = state["zoomRatio"]
            self.session["scrollLeft"] = state["scrollLeft"]
        if self.after_apply is not None:
            self.after_apply(self, state)

    def set_viewport(self, path: str, start: int, end: int) -> None:
        self._record("viewport", path=path, start=start, end=end)

    def reveal_time(self, path: str, tick: int) -> None:
        self._record("reveal", path=path, time=tick)

    # -- assertions ---------------------------------------------------------
    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    def applied(self) -> list[dict]:
        return [params["state"] for op, params in self.calls if op == "apply"]

    @classmethod
    def names(cls, rows: list[dict]) -> list[str]:
        out: list[str] = []
        for row in rows:
            if row.get("dataType") == "signal-group":
                out.extend(cls.names(row.get("children", [])))
            elif isinstance(row.get("name"), str):
                out.append(row["name"])
        return out

    def shown(self) -> list[str]:
        return self.names(self.rows)


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
            patterns = [cmd[index + 1] for index, token in enumerate(cmd) if token == "-s"]
            selected = [
                signal
                for signal in state["signals"]
                if not patterns
                or any(
                    _fake_signal_matches(
                        signal[0] if isinstance(signal, tuple) else signal, pattern
                    )
                    for pattern in patterns
                )
            ]
            data = {
                "scope_prefix": "",
                "clock": state["clock"],
                "total_ticks": state["total_ticks"],
                "signals": [_list_row(s) for s in selected],
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


def _install_viewer(
    monkeypatch,
    *,
    netlist: list[str] | None = None,
    rows=(),
    open_docs=(),
    version: str | None = "1.6.0",
) -> _FakeViewer:
    """Attach one fake window: discovery finds it and the client drives it."""
    viewer = _FakeViewer(
        _CANNED_NETLIST if netlist is None else netlist,
        rows=rows,
        open_docs=open_docs,
        version=version,
    )
    info = bwave_bridge.BridgeInfo(
        Path("/fake/window-1.sock"),
        version,
        True,
        1.0,
        tuple(viewer.docs),
        None if version else "VaporView (lramseyer.vaporview) is not installed",
    )
    monkeypatch.setattr(bwave_bridge, "discover", lambda home=None: [info])
    monkeypatch.setattr(bwave_bridge, "BridgeClient", lambda path: viewer)
    return viewer


def _viewer_for(trace: Path, monkeypatch, *, opened: bool = True, **kwargs) -> _FakeViewer:
    docs = [str(trace.resolve())] if opened else []
    return _install_viewer(monkeypatch, open_docs=docs, **kwargs)


def _row(name: str, **extra) -> dict:
    return {"dataType": "netlist-variable", "name": name, **extra}


def test_scoped_gui_happy_path(tmp_path, monkeypatch, fake_rust, capsys):
    trace = _seed_default(tmp_path)
    # A stale row in a fresh document must not survive the replacement.
    viewer = _viewer_for(trace, monkeypatch, opened=False, rows=[_row("tb.rst_n")])

    _gui(signals=["tb.dut.fifo.*"], time="100c:200c")

    assert viewer.ops() == ["open", "apply", "viewport"]
    state = viewer.applied()[0]
    assert state["displayedSignals"] == [
        _row("tb.clk"),  # clock is row 1, unasked-for
        _row("tb.dut.fifo.full"),
        _row("tb.dut.fifo.empty"),
        _row("tb.dut.fifo.count", msb=3, lsb=0),  # bare path + bit range
    ]
    # The range is bracketed by the two markers, so the viewer reports its span.
    assert state["markerTime"] == 100 * _TICKS_PER_CYCLE
    assert state["altMarkerTime"] == 200 * _TICKS_PER_CYCLE
    assert "zoomRatio" not in state  # the exact window is set after the rows
    assert viewer.calls[-1] == (
        "viewport",
        {"path": str(trace.resolve()), "start": 1000, "end": 2000},
    )
    assert viewer.shown() == [
        "tb.clk",
        "tb.dut.fifo.full",
        "tb.dut.fifo.empty",
        "tb.dut.fifo.count",
    ]
    out = capsys.readouterr().out
    assert "opened, scoped to" in out
    assert "tb.dut.fifo.count[3:0]" in out and "viewport: 1000..2000 ticks" in out
    assert "START marker: 1000 ticks" in out and "END marker: 2000 ticks" in out


def test_readback_lag_is_polled_until_the_view_settles(tmp_path, monkeypatch, fake_rust, capsys):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.rst_n")])
    viewer.lag = 3

    _gui(signals=["tb.dut.fifo.full"])

    assert viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]
    assert "WARNING" not in capsys.readouterr().err


def test_explicit_named_groups_share_one_native_group(tmp_path, monkeypatch, fake_rust, capsys):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1), ("tb.dut.fifo.empty", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    _gui(groups=["Status=tb.dut.fifo.full", "Status=tb.dut.fifo.empty"])

    rows = viewer.applied()[-1]["displayedSignals"]
    assert rows[0] == _row("tb.clk")
    assert rows[1]["groupName"] == "Status"
    assert [item["name"] for item in rows[1]["children"]] == [
        "tb.dut.fifo.full",
        "tb.dut.fifo.empty",
    ]
    assert "Status:" in capsys.readouterr().out


def test_radix_and_color_suffixes_apply_to_flat_grouped_and_clock_rows(
    tmp_path, monkeypatch, fake_rust
):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.clk", 1), ("tb.dut.fifo.full", 1), ("tb.dut.fifo.count[3:0]", 4)]
    viewer = _viewer_for(trace, monkeypatch)

    _gui(
        signals=["tb.clk%b@red", "tb.dut.fifo.full%b@green"],
        groups=["State=tb.dut.fifo.count*%d@blue"],
    )

    # Annotations are wrapper syntax; only clean signal globs reach B-Wave.
    list_calls = [call for call in fake_rust["calls"] if call[1] == "list"]
    assert list_calls
    assert all("%" not in token and "@red" not in token for call in list_calls for token in call)
    rows = viewer.applied()[-1]["displayedSignals"]
    assert rows[0] == _row("tb.clk", numberFormat="binary", colorIndex=4)
    assert rows[1] == _row("tb.dut.fifo.full", numberFormat="binary", colorIndex=6)
    assert rows[2]["groupName"] == "State"
    assert rows[2]["children"] == [
        _row("tb.dut.fifo.count", msb=3, lsb=0, numberFormat="decimal", colorIndex=5)
    ]


def test_exact_indexed_selector_keeps_radix_and_color(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.words[56]", 1)]
    viewer = _viewer_for(trace, monkeypatch, netlist=["tb.dut.words"])

    _gui(signals=["words[56]%h@blue"], no_clock=True)

    assert viewer.rows == [
        _row("tb.dut.words", numberFormat="hexadecimal", colorIndex=5, netlistId=1)
    ]


def test_append_restyles_an_existing_row_without_adding_it(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    existing = [_row("tb.dut.fifo.full", numberFormat="hexadecimal", colorIndex=0)]
    viewer = _viewer_for(trace, monkeypatch, rows=existing)

    _gui(signals=["tb.dut.fifo.full%b@blue"], append=True)

    assert viewer.names(viewer.applied()[-1]["displayedSignals"]) == ["tb.dut.fifo.full"]
    assert viewer.rows == [
        _row("tb.dut.fifo.full", numberFormat="binary", colorIndex=5, netlistId=1)
    ]


def test_style_readback_mismatch_is_an_error_and_restores(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    def drop_style(model, state):
        for row in model.rows:
            row["numberFormat"] = "binary"
            row["colorIndex"] = 1

    viewer.after_apply = drop_style

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.full%h@green"], append=True)

    assert "did not apply the exact requested signal presentation" in str(exc.value)
    assert viewer.rows == []


def test_replace_rejects_inexact_tree_and_restores_previous_rows(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    previous = [_row("tb.dut.fifo.empty", colorIndex=3)]
    viewer = _viewer_for(trace, monkeypatch, rows=previous)
    original = copy.deepcopy(viewer.rows)

    def add_unrequested_row(model, state):
        if model.names(state["displayedSignals"]) != ["tb.dut.fifo.empty"]:
            model.rows.append(model._resolve(_row("tb.rst_n")))

    viewer.after_apply = add_unrequested_row

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.full"], no_clock=True)

    assert "did not apply the requested signal rows" in str(exc.value)
    assert viewer.rows == original
    restore = viewer.applied()[-1]
    assert restore["markerTime"] == 11 and restore["altMarkerTime"] == 22
    assert restore["zoomRatio"] == 1.5 and restore["scrollLeft"] == 7


def test_failed_restore_reports_both_failures(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch)
    viewer.fail_op["viewport"] = "Invalid time range"

    def break_restore(model, state):
        if "zoomRatio" in state:  # only the restore carries the old viewport
            model.rows = [model._resolve(_row("tb.junk"))]

    viewer.after_apply = break_restore

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.full"], time="100c:200c")

    message = str(exc.value)
    assert "Invalid time range" in message
    assert "restoring the previous presentation also failed" in message


@pytest.mark.parametrize(
    ("selector", "message"),
    [
        ("tb.dut.fifo.full%u", "unknown radix suffix '%u'"),
        ("tb.dut.fifo.full@orange", "unknown waveform color '@orange'"),
        ("tb.dut.fifo.full@red%h", "radix must precede color"),
    ],
)
def test_invalid_display_suffix_fails_before_viewer_mutation(
    tmp_path, monkeypatch, fake_rust, capsys, selector, message
):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=[selector])

    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    assert fake_rust["calls"] == []
    assert viewer.calls == []


def test_distinct_named_groups_preserve_requested_order(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1), ("tb.dut.fifo.empty", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    _gui(groups=["Backpressure=tb.dut.fifo.full", "Availability=tb.dut.fifo.empty"])

    rows = viewer.applied()[-1]["displayedSignals"]
    assert [item["groupName"] for item in rows[1:]] == ["Backpressure", "Availability"]


def test_group_readback_rejects_reordered_hierarchy(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1), ("tb.dut.fifo.empty", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    def reorder_groups(model, state):
        if len(model.rows) > 2:
            model.rows[1:] = reversed(model.rows[1:])

    viewer.after_apply = reorder_groups

    with pytest.raises(SystemExit, match="requested signal rows"):
        _gui(groups=["Backpressure=tb.dut.fifo.full", "Availability=tb.dut.fifo.empty"])


def test_append_merges_into_existing_group_and_preserves_its_state(
    tmp_path, monkeypatch, fake_rust
):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1), ("tb.dut.fifo.empty", 1)]
    existing = [
        _row("tb.clk"),
        {
            "dataType": "signal-group",
            "groupName": "Status",
            "collapseState": 1,
            "children": [_row("tb.dut.fifo.full", colorIndex=3)],
        },
    ]
    viewer = _viewer_for(trace, monkeypatch, rows=existing)

    _gui(groups=["Status=tb.dut.fifo.*"], append=True)

    status = viewer.applied()[0]["displayedSignals"][1]
    assert status["collapseState"] == 1
    assert status["children"][0]["colorIndex"] == 3
    assert [item["name"] for item in status["children"]] == [
        "tb.dut.fifo.full",
        "tb.dut.fifo.empty",
    ]
    assert viewer.shown() == ["tb.clk", "tb.dut.fifo.full", "tb.dut.fifo.empty"]


def test_append_moves_an_existing_top_level_row_into_a_group(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    existing = [_row("tb.clk"), _row("tb.dut.fifo.full", colorIndex=3)]
    viewer = _viewer_for(trace, monkeypatch, rows=existing)

    _gui(groups=["Backpressure=tb.dut.fifo.full"], append=True)

    rows = viewer.applied()[0]["displayedSignals"]
    assert [item.get("name") for item in rows if "name" in item] == ["tb.clk"]
    assert rows[1]["groupName"] == "Backpressure"
    assert [(r["name"], r["colorIndex"]) for r in rows[1]["children"]] == [("tb.dut.fifo.full", 3)]


def test_append_recognizes_native_saved_vector_without_lookup_range(
    tmp_path, monkeypatch, fake_rust
):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.bus[3:0]", 4)]
    viewer = _viewer_for(trace, monkeypatch, netlist=["tb.dut.bus"], rows=[_row("tb.dut.bus")])

    _gui(groups=["Low word=tb.dut.bus*"], append=True)

    rows = viewer.applied()[0]["displayedSignals"]
    assert len(rows) == 1
    assert rows[0]["children"][0]["name"] == "tb.dut.bus"
    assert "msb" not in rows[0]["children"][0]  # the preserved row, not a new one


def test_late_viewport_failure_restores_the_original_view(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.rst_n", colorIndex=4)])
    original = copy.deepcopy(viewer.rows)
    viewer.fail_op["viewport"] = "viewport rejected"

    with pytest.raises(SystemExit, match="viewport rejected"):
        _gui(groups=["Status=tb.dut.fifo.full"], time="10c:20c")

    assert viewer.rows == original
    assert viewer.session["markerTime"] == 11
    assert viewer.session["altMarkerTime"] == 22


def test_marker_readback_mismatch_is_an_error(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    def ignore_markers(model, state):
        model.session["altMarkerTime"] = 22

    viewer.after_apply = ignore_markers

    with pytest.raises(SystemExit, match="exact requested signal presentation"):
        _gui(signals=["tb.dut.fifo.full"], time="100c:200c")


def test_document_closed_mid_update_is_an_error(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    def close_tab(model, state):
        model.docs.clear()

    viewer.after_apply = close_tab

    with pytest.raises(SystemExit, match="is not open in the Waveform Viewer"):
        _gui(signals=["tb.dut.fifo.full"])


def test_wrong_vaporview_version_refuses_before_mutation(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch, version="1.5.9")

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.full"])

    message = str(exc.value)
    assert "runs VaporView 1.5.9" in message
    assert "--install-extension lramseyer.vaporview@" in message
    assert viewer.calls == []


def test_window_without_vaporview_refuses_with_install_guidance(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch, version=None)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.full"])

    message = str(exc.value)
    assert "no usable VaporView extension" in message
    assert "is not installed" in message
    assert "Install from VSIX" in message
    assert viewer.calls == []


def test_group_rejects_a_missing_name_glob_separator(tmp_path, capsys):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(groups=["Status"])

    assert exc.value.code == 2
    assert "NAME=GLOB" in capsys.readouterr().err


def test_group_rejects_html_control_characters_in_the_viewer_label(tmp_path, capsys):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(groups=["<script>=tb.dut.fifo.full"])

    assert exc.value.code == 2
    assert "cannot contain" in capsys.readouterr().err


def test_multibit_vector_reaches_the_viewer(tmp_path, monkeypatch, fake_rust, capsys):
    """Regression: a vector must be sent as bare path + bit range.

    `bwave list` names a 4-bit register `count[3:0]`; VaporView's netlist keys
    it as `count` with msb/lsb, so the bracketed path would resolve to nothing.
    """
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch)

    _gui(signals=["tb.dut.fifo.*"])

    assert _row("tb.dut.fifo.count", msb=3, lsb=0) in viewer.applied()[0]["displayedSignals"]
    # The viewer really resolved the vector — not just an ack that says so.
    assert viewer.rows[-1]["netlistId"] == viewer.ids["tb.dut.fifo.count"]
    captured = capsys.readouterr()
    # Reported under the name the user typed and `bwave list` prints.
    assert "tb.dut.fifo.count[3:0]" in captured.out
    assert "WARNING" not in captured.err


# ── clock-first / markers ───────────────────────────────────────────────────


def test_new_view_gets_the_clock_as_row_one(tmp_path, monkeypatch, fake_rust, capsys):
    """A waveform without its clock is unreadable — you cannot count cycles."""
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    _gui(signals=["tb.dut.fifo.full"])

    assert viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]
    # It rides on the `list` call gui already makes — never a `stats` walk,
    # which on a large store runs for minutes and blows the query timeout.
    assert [c[1] for c in fake_rust["calls"]] == ["list"]
    assert "tb.clk" in capsys.readouterr().out


def test_no_clock_opts_out(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    _gui(signals=["tb.dut.fifo.full"], no_clock=True)

    assert viewer.shown() == ["tb.dut.fifo.full"]


def test_append_does_not_re_add_the_clock(tmp_path, monkeypatch, fake_rust):
    """--append builds on a view that already has its clock row."""
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.clk")])

    _gui(signals=["tb.dut.fifo.full"], append=True)

    assert viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]


def test_append_does_not_duplicate_an_already_displayed_signal(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.clk"), _row("tb.dut.fifo.full")])

    _gui(signals=["tb.dut.fifo.full"], append=True)

    assert viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]


def test_clock_is_not_duplicated_when_the_glob_already_matched_it(
    tmp_path, monkeypatch, fake_rust
):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.clk", 1), ("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    _gui(signals=["tb.*"])

    assert viewer.names(viewer.applied()[0]["displayedSignals"]) == ["tb.clk", "tb.dut.fifo.full"]


def test_async_trace_without_a_clock_still_opens(tmp_path, monkeypatch, fake_rust):
    """No clock detected (async trace) → no row 1, no crash."""
    trace = _seed_default(tmp_path)
    fake_rust["clock"] = None
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    _gui(signals=["tb.dut.fifo.full"])

    assert viewer.shown() == ["tb.dut.fifo.full"]


def test_gui_never_walks_the_trace_with_stats(tmp_path, monkeypatch, fake_rust):
    """`stats` is minutes on a large store — gui must not touch it, ever.

    An open-ended range resolves its end through `list`, which reads the
    hierarchy only; `stats` walks every transition of every signal.
    """
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch)

    _gui(signals=["tb.dut.fifo.*"], time="100c:")

    assert "stats" not in [c[1] for c in fake_rust["calls"]]
    assert viewer.calls[-1][1]["end"] == fake_rust["total_ticks"]


def test_cursor_moves_start_marker_and_end_stays(tmp_path, monkeypatch, fake_rust):
    """--cursor relocates START (main) inside the range; END (alt) holds."""
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch)

    _gui(time="100c:200c", cursor="120c", append=True)

    state = viewer.applied()[0]
    assert (state["markerTime"], state["altMarkerTime"]) == (1200, 2000)
    assert viewer.calls[-1][0] == "viewport"


def test_cursor_without_a_range_is_a_lone_main_marker_scrolled_into_view(
    tmp_path, monkeypatch, fake_rust
):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.clk")])

    _gui(cursor="120c", append=True)

    state = viewer.applied()[0]
    assert state["markerTime"] == 1200
    assert "altMarkerTime" not in state  # the other marker stays where it was
    assert viewer.session["altMarkerTime"] == 22
    assert viewer.shown() == ["tb.clk"]  # append keeps every row
    assert viewer.calls[-1] == ("reveal", {"path": str(trace.resolve()), "time": 1200})


def test_signals_the_viewer_drops_are_reported_and_not_left_on_screen(
    tmp_path, monkeypatch, fake_rust, capsys
):
    """A signal the viewer cannot resolve gets a warning, never a placeholder row."""
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1), ("tb.dut.ghost[7:0]", 8)]
    # `ghost` is in the trace but absent from the viewer's netlist.
    viewer = _viewer_for(trace, monkeypatch)

    _gui(signals=["tb.dut.*"])

    assert viewer.shown() == ["tb.clk", "tb.dut.fifo.full"]
    captured = capsys.readouterr()
    assert "tb.dut.fifo.full" in captured.out
    assert "tb.dut.ghost[7:0]" not in captured.out  # never claim what did not land
    assert "WARNING" in captured.err and "tb.dut.ghost[7:0]" in captured.err


def test_dropped_group_member_leaves_no_empty_group(tmp_path, monkeypatch, fake_rust, capsys):
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.ghost", 1)]
    viewer = _viewer_for(trace, monkeypatch)

    _gui(groups=["Ghosts=tb.dut.ghost"])

    assert viewer.rows == [_row("tb.clk", numberFormat="binary", colorIndex=1, netlistId=4)]
    assert "Ghosts" not in capsys.readouterr().out


def test_append_keeps_a_users_unresolved_row(tmp_path, monkeypatch, fake_rust):
    """Only rows this run added are judged; the user's own rows are preserved."""
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.old.gone")])

    _gui(signals=["tb.dut.fifo.full"], append=True)

    assert viewer.shown() == ["tb.old.gone", "tb.dut.fifo.full"]


def test_bracketed_identifier_falls_back_to_the_raw_name(tmp_path, monkeypatch, fake_rust, capsys):
    """A 1-bit `mem[0]` may be an array element, not a bit-select.

    The name alone cannot settle it, so the bare-path attempt is retried under
    the raw name when the viewer does not resolve it.
    """
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.mem[0]", 1)]
    viewer = _viewer_for(trace, monkeypatch, netlist=["tb.dut.mem[0]"])

    _gui(signals=["tb.dut.mem*"], no_clock=True)

    first, retry = viewer.applied()
    assert first["displayedSignals"] == [_row("tb.dut.mem", msb=0, lsb=0)]  # bit-select
    assert retry["displayedSignals"] == [_row("tb.dut.mem[0]")]  # …unresolved: identifier
    assert viewer.shown() == ["tb.dut.mem[0]"]
    captured = capsys.readouterr()
    assert "tb.dut.mem[0]" in captured.out
    assert "WARNING" not in captured.err


def test_wide_bracketed_identifier_is_never_split(tmp_path, monkeypatch, fake_rust):
    """A wider-than-1-bit `mem[0]` can only be an identifier — no retry needed."""
    trace = _seed_default(tmp_path)
    fake_rust["signals"] = [("tb.dut.mem[0]", 8)]
    viewer = _viewer_for(trace, monkeypatch, netlist=["tb.dut.mem[0]"])

    _gui(signals=["tb.dut.mem*"], no_clock=True)

    assert [s["displayedSignals"] for s in viewer.applied()] == [[_row("tb.dut.mem[0]")]]


def test_append_adds_without_clearing(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.clk")])

    _gui(signals=["tb.dut.fifo.*"], append=True)

    assert viewer.shown() == [
        "tb.clk",  # kept
        "tb.dut.fifo.full",
        "tb.dut.fifo.empty",
        "tb.dut.fifo.count",
    ]


def test_scoped_replace_on_already_open_document(tmp_path, monkeypatch, fake_rust, capsys):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.clk"), _row("tb.rst_n")])

    _gui(signals=["tb.dut.fifo.*"])

    assert viewer.ops() == ["open", "apply"]  # open only reveals the tab
    assert viewer.shown() == [
        "tb.clk",
        "tb.dut.fifo.full",
        "tb.dut.fifo.empty",
        "tb.dut.fifo.count",
    ]
    assert "Waveform Viewer scoped to" in capsys.readouterr().out


def test_time_only_moves_viewport_without_changing_rows(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch, rows=[_row("tb.clk")])

    _gui(time="100c:200c", append=True)

    assert viewer.shown() == ["tb.clk"]
    assert viewer.calls[-1] == (
        "viewport",
        {"path": str(trace.resolve()), "start": 1000, "end": 2000},
    )


def test_tick_range_is_exact_on_clockless_trace(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["clock"] = None
    viewer = _viewer_for(trace, monkeypatch)

    _gui(time="10000t:20000t", append=True, no_clock=True)

    value_calls = [call for call in fake_rust["calls"] if call[1] == "value"]
    assert all("--async" in call for call in value_calls)
    assert viewer.calls[-1][1] == {"path": str(trace.resolve()), "start": 10000, "end": 20000}


def test_physical_range_works_on_clockless_trace(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    fake_rust["clock"] = None
    viewer = _viewer_for(trace, monkeypatch)

    _gui(time="100ns:200ns", append=True, no_clock=True)

    value_calls = [call for call in fake_rust["calls"] if call[1] == "value"]
    assert all("--async" in call for call in value_calls)
    assert viewer.calls[-1][1] == {"path": str(trace.resolve()), "start": 100000, "end": 200000}


def test_physical_time_range_is_not_shifted_by_sync_origin(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch)

    _gui(time="50ns:150ns", append=True)

    value_calls = [call for call in fake_rust["calls"] if call[1] == "value"]
    assert all("--async" in call for call in value_calls)
    assert viewer.calls[-1][1] == {"path": str(trace.resolve()), "start": 50000, "end": 150000}


def test_scoped_open_without_a_bridge_hard_errors(tmp_path, monkeypatch, fake_rust, launched):
    # The autouse _no_bridge fixture leaves no attached window.
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.*"])

    msg = str(exc.value)
    assert "no window answered" in msg
    assert 'run "Developer: Reload Window"' in msg
    assert "refresh the Sandbox" in msg
    assert exc.value.code
    assert launched == []  # never degrade a scoped request to a bare launch


def test_scoped_open_missing_extension_leads_with_install(
    tmp_path, monkeypatch, fake_rust, launched
):
    from booley.bwave import cli as bwave
    from booley.runtime.vaporview import ExtensionState

    _seed_default(tmp_path)
    monkeypatch.setattr(bwave, "_viewer_extension_state", lambda: ExtensionState.MISSING)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.*"])

    msg = str(exc.value)
    assert "VaporView extension is not installed" in msg
    assert "--install-extension" in msg
    assert "Install from VSIX" in msg
    assert 'Reload Window" there' not in msg
    assert launched == []


def test_scoped_open_empty_remote_registry_beats_unavailable_editor_probe(
    tmp_path, monkeypatch, fake_rust
):
    from booley.bwave import cli as bwave

    _seed_default(tmp_path)
    home = tmp_path / "home"
    (home / ".vscode-server" / "extensions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(bwave.shutil, "which", lambda name: None)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.*"])

    msg = str(exc.value)
    assert "VaporView extension is not installed" in msg
    assert "no window answered" not in msg


def test_scoped_open_cursor_only_names_cursor_installer(tmp_path, monkeypatch, fake_rust):
    from booley.bwave import cli as bwave
    from booley.runtime.vaporview import ExtensionState

    _seed_default(tmp_path)
    monkeypatch.setattr(bwave.shutil, "which", lambda name: "cursor" if name == "cursor" else None)
    monkeypatch.setattr(bwave, "_viewer_extension_state", lambda: ExtensionState.MISSING)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.*"])

    assert "cursor --install-extension lramseyer.vaporview" in str(exc.value)


def test_scoped_open_unknown_extension_state_does_not_claim_it_is_missing(
    tmp_path, monkeypatch, fake_rust, launched
):
    from booley.bwave import cli as bwave
    from booley.runtime.vaporview import ExtensionState

    _seed_default(tmp_path)
    monkeypatch.setattr(bwave, "_viewer_extension_state", lambda: ExtensionState.UNKNOWN)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.*"])

    msg = str(exc.value)
    assert "could not determine whether the VaporView extension is installed" in msg
    assert "VaporView extension is not installed" not in msg
    assert launched == []


def test_scoped_protocol_failure_names_the_probe_failure(
    tmp_path, monkeypatch, fake_rust, launched
):
    _seed_default(tmp_path)

    def broken(home=None):
        raise bwave_bridge.BridgeProtocolError("ping: unsupported bridge reply")

    monkeypatch.setattr(bwave_bridge, "discover", broken)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.*"])

    msg = str(exc.value)
    assert 'run "Developer: Reload Window"' in msg
    assert "bridge probe: ping: unsupported bridge reply" in msg
    assert launched == []


def test_scoped_open_with_ambiguous_windows_says_how_to_pick(tmp_path, monkeypatch, fake_rust):
    _seed_default(tmp_path)
    windows = [
        bwave_bridge.BridgeInfo(Path(f"/fake/window-{i}.sock"), "1.6.0", False, 0.0, ())
        for i in (1, 2)
    ]
    monkeypatch.setattr(bwave_bridge, "discover", lambda home=None: windows)
    monkeypatch.delenv(bwave_bridge.SOCKET_ENV, raising=False)

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.fifo.*"])

    msg = str(exc.value)
    assert "2 VS Code windows are attached" in msg
    assert bwave_bridge.SOCKET_ENV in msg


def test_glob_without_match_errors_before_the_viewer(tmp_path, monkeypatch, fake_rust, capsys):
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


def test_signal_cap_counts_flat_and_grouped_selections_together(
    tmp_path, monkeypatch, fake_rust, capsys
):
    _seed_default(tmp_path)
    fake_rust["signals"] = [f"tb.dut.bus.bit{i}" for i in range(10)]

    with pytest.raises(SystemExit) as exc:
        _gui(
            signals=["tb.dut.bus.bit0"],
            groups=["Bus=tb.dut.bus.bit[1-9]"],
            max_signals=9,
        )

    assert exc.value.code == 2
    msg = capsys.readouterr().err
    assert "10 signals" in msg and "--max-signals" in msg


def test_max_signals_raises_the_cap(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    bus = [f"tb.dut.bus.bit{i}" for i in range(65)]
    fake_rust["signals"] = bus
    viewer = _viewer_for(trace, monkeypatch, netlist=bus)

    _gui(signals=["tb.dut.bus.*"], max_signals=100, no_clock=True)

    assert viewer.shown() == bus


def test_marker_names_resolve_in_time_range(tmp_path, monkeypatch, fake_rust):
    trace = _make_trace(tmp_path)
    _write_sessions({"dut": _make_entry(str(trace), markers={"overflow": 1200})})
    viewer = _viewer_for(trace, monkeypatch)

    _gui("@dut", time="overflow:1400c")

    # The marker resolves to its cycle with an explicit 'c' suffix before
    # hitting the Rust converter.
    value_tokens = [c[c.index("--at") + 1] for c in fake_rust["calls"] if c[1] == "value"]
    assert value_tokens == ["1200c", "1400c"]
    assert viewer.calls[-1][1] == {
        "path": str(trace.resolve()),
        "start": 1200 * _TICKS_PER_CYCLE,
        "end": 1400 * _TICKS_PER_CYCLE,
    }


def test_gui_ended_range_uses_list_total_ticks(tmp_path, monkeypatch, fake_rust):
    trace = _seed_default(tmp_path)
    viewer = _viewer_for(trace, monkeypatch)

    _gui(time="100c:", append=True)

    assert viewer.calls[-1][1]["end"] == fake_rust["total_ticks"]


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
    assert "Sandbox" in capsys.readouterr().err


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
    env["HOME"] = str(tmp_path / "home")  # no bridge sockets: editor-CLI fallback

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


@pytest.mark.parametrize("command", ["find", "sample", "distance"])
@pytest.mark.parametrize(
    "trigger", ["change", "ChAnGe", "RISING", "FaLlInG", "'h10", "'d16", "'b10000"]
)
def test_trigger_arguments_forward_unchanged(command, trigger):
    from booley.bwave import cli as bwave

    args = [command, "scalar", trigger]
    if command == "sample":
        args += ["-s", "bus"]
    if command == "distance":
        args += ["--to", "bus", trigger]
    assert bwave._passthrough_subcommand(args, "trace.fst") == [
        command,
        "trace.fst",
        *args[1:],
    ]


@pytest.mark.parametrize(
    ("extra", "expected_limit", "capped"),
    [
        (["value", "--at", "0"], "2000", False),
        (["list"], "400", False),
        (["value", "--at", "0", "--limit=10000"], "10000", False),
        (["value", "--at", "0", "--limit", "10001"], "10000", True),
        (["value", "--at", "0", "--limit=2000000"], "10000", True),
    ],
)
def test_issue_1108_query_limit_dispatch(monkeypatch, capsys, extra, expected_limit, capped):
    from booley.bwave import cli as bwave

    captured = []
    monkeypatch.setattr(bwave, "_resolve_trace", lambda args: "/tmp/trace.fst")
    monkeypatch.setattr(bwave, "_resolve_markers_in_args", lambda *args: None)
    monkeypatch.setattr(bwave, "_inject_marker_flags", lambda *args: None)
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave"])
    monkeypatch.setattr(bwave, "_run", lambda args: captured.extend(args) or 0)
    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=extra))
    assert exc.value.code == 0
    limits = [captured[i + 1] for i, arg in enumerate(captured[:-1]) if arg == "--limit"]
    limits += [arg.partition("=")[2] for arg in captured if arg.startswith("--limit=")]
    assert limits == [expected_limit]
    assert ("--limit capped at 10000" in capsys.readouterr().err) == capped


@pytest.mark.parametrize(
    ("extra", "subcommand", "remaining"),
    [
        (["--distance", "a", "change", "--stats"], "distance", "--stats"),
        (["--stats"], "stats", None),
        (["--grep", "*", "--format", "json", "--limit=10001"], "list", "--format"),
        (["--grep", "*", "--async", "--unsupported"], "list", "--unsupported"),
    ],
)
def test_issue_1108_legacy_and_grep_dispatch(monkeypatch, extra, subcommand, remaining):
    from booley.bwave import cli as bwave

    captured = []
    monkeypatch.setattr(bwave, "_resolve_trace", lambda args: "/tmp/trace.fst")
    monkeypatch.setattr(bwave, "_resolve_markers_in_args", lambda *args: None)
    monkeypatch.setattr(bwave, "_inject_marker_flags", lambda *args: None)
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave"])
    monkeypatch.setattr(bwave, "_run", lambda args: captured.extend(args) or 0)
    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=extra))
    assert exc.value.code == 0
    assert captured[1] == subcommand
    if remaining:
        assert remaining in captured


@pytest.mark.native_bwave
@pytest.mark.parametrize("limit", [["--limit", "10001"], ["--limit=2000000"]])
def test_issue_1108_real_json_cap_preserves_native_warnings(monkeypatch, tmp_path, capfd, limit):
    from booley.bwave import cli as bwave
    from booley.dev_support.test_profiles import native_test_binary

    binary = native_test_binary(BOOLEY_ROOT)
    assert binary.is_file(), "build the native bwave binary before this integration test"
    vcd = tmp_path / "input.vcd"
    fst = tmp_path / "input.fst"
    vcd.write_text(
        "$timescale 1ps $end\n$scope module tb $end\n"
        "$var wire 1 ! a $end\n$var wire 1 ! alias $end\n"
        "$upscope $end\n$enddefinitions $end\n#0\n0!\n#1000\n1!\n#2000\n"
    )
    subprocess.run(
        [str(binary), "build", str(vcd), "-o", str(fst)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    monkeypatch.setattr(bwave, "_resolve_trace", lambda args: str(fst))
    monkeypatch.setattr(bwave, "_resolve_markers_in_args", lambda *args: None)
    monkeypatch.setattr(bwave, "_inject_marker_flags", lambda *args: None)
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: [str(binary)])
    extra = ["value", "--at", "1000t", "--async", "-s", "tb.*", "--format", "json", *limit]
    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=extra))
    assert exc.value.code == 0
    output = capfd.readouterr()
    envelope = json.loads(output.out)
    assert envelope["warnings"].count("--limit capped at 10000") == 1
    assert any("shown once" in warning for warning in envelope["warnings"])
    assert output.err.count("--limit capped at 10000") == 1


@pytest.mark.native_bwave
@pytest.mark.parametrize(
    "extra",
    [
        ["value", "--at", "0", "--limit", "0"],
        ["value", "--at", "0", "--limit=bad"],
        ["value", "--at", "0", "--limit=1_000_000"],
        ["value", "--at", "0", "--limit= 10001"],
        ["value", "--at", "0", "--limit", "2", "--limit=3"],
        ["value", "--at", "0", "--limit", "-1"],
        ["--distance", "a", "change", "-s", "a"],
        ["--grep", "*", "--unsupported"],
    ],
)
def test_issue_1108_wrapper_preserves_native_input_errors(monkeypatch, extra):
    from booley.bwave import cli as bwave
    from booley.dev_support.test_profiles import native_test_binary

    binary = native_test_binary(BOOLEY_ROOT)
    assert binary.is_file()
    monkeypatch.setattr(bwave, "_resolve_trace", lambda args: "/nonexistent/trace.fst")
    monkeypatch.setattr(bwave, "_resolve_markers_in_args", lambda *args: None)
    monkeypatch.setattr(bwave, "_inject_marker_flags", lambda *args: None)
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: [str(binary)])
    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=extra))
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "extra",
    [
        ["value", "--at", "0", "--limit", "2", "--limit=3"],
        ["value", "--at", "0", "--limit"],
        ["value", "--at", "0", "--limit=bad"],
    ],
)
def test_issue_1108_invalid_limits_reach_native_dispatch(monkeypatch, capsys, extra):
    from booley.bwave import cli as bwave

    captured = []
    monkeypatch.setattr(bwave, "_resolve_trace", lambda args: "/tmp/trace.fst")
    monkeypatch.setattr(bwave, "_resolve_markers_in_args", lambda *args: None)
    monkeypatch.setattr(bwave, "_inject_marker_flags", lambda *args: None)
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave"])
    monkeypatch.setattr(bwave, "_run", lambda args: captured.extend(args) or 2)
    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=extra.copy()))
    assert exc.value.code == 2
    assert captured == ["bwave", "value", "/tmp/trace.fst", *extra[1:]]
    assert "--wrapper-warning" not in captured
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("extra", "diagnostic"),
    [
        (["--wave", "--stats"], "multiple query modes are mutually exclusive"),
        (["--distance", "a"], "--distance requires PATTERN VALUE"),
    ],
)
def test_issue_1108_invalid_legacy_modes_fail_before_dispatch(
    monkeypatch, capsys, extra, diagnostic
):
    from booley.bwave import cli as bwave

    captured = []
    monkeypatch.setattr(bwave, "_resolve_trace", lambda args: "/tmp/trace.fst")
    monkeypatch.setattr(bwave, "_resolve_markers_in_args", lambda *args: None)
    monkeypatch.setattr(bwave, "_inject_marker_flags", lambda *args: None)
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave"])
    monkeypatch.setattr(bwave, "_run", lambda args: captured.extend(args) or 0)
    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=extra))
    assert exc.value.code == 2
    assert diagnostic in capsys.readouterr().err
    assert captured == []


@pytest.mark.parametrize(
    "args",
    [
        ["overview", "--format", "json", "--clock", "clk"],
        ["value", "--at", "2", "--sample-mode", "pre-edge"],
        ["find", "valid", "'h1", "--include-stopped"],
        ["sample", "valid", "'h1", "--include-stopped", "--sample-mode", "pre-edge"],
        ["signal", "--format", "json"],
        ["wave", "--format", "json"],
        ["distance", "valid", "change", "--format", "json"],
    ],
)
def test_clock_grid_options_forward_to_registered_store(args):
    from booley.bwave import cli as bwave

    assert bwave._translate_to_subcommand(args, "trace.fst") == [
        args[0],
        "trace.fst",
        *args[1:],
    ]
