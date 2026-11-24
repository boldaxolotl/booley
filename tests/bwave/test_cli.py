"""Tests for `bwave gui`: target resolution, selection checks, and the no-viewer exit.

Booley has no waveform viewer for now, so
`gui` resolves its target and selection, then exits non-zero. The selection is
checked through `_build_gui_plan`; the Rust binary is faked with canned JSON
envelopes.
"""

from __future__ import annotations

import argparse
import fnmatch
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.core.host_shell import quote_arg
from tests.bwave.session_store_support import isolate_temporary_store
from tests.conftest import MINIMAL_FST_BYTES
from tests.subprocess_budget import run_bounded_child

BOOLEY_ROOT = Path(__file__).resolve().parent.parent.parent

# `gui` never reads the trace contents, but discovery does
# check the store is real — header plus at least one value-change block, so a
# header-only stub does not read as a queryable trace.
_FAKE_TRACE_BYTES = MINIMAL_FST_BYTES


@pytest.fixture(autouse=True)
def _inside_session_runtime(monkeypatch):
    """bwave is container-only (ADR 0028); simulate the Sandbox."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


@pytest.fixture(autouse=True)
def _clean_session(monkeypatch, tmp_path):
    """Start every test with an empty temporary-directory session store of its own."""
    isolate_temporary_store(monkeypatch, tmp_path)


def _write_sessions(entries: dict) -> None:
    from booley.bwave import sessions

    session_file = sessions._temporary_store().path
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(json.dumps(entries, indent=2) + "\n")


def _make_entry(trace: str, markers: dict | None = None) -> dict:
    entry = {"trace": trace, "registered_at": datetime.now(UTC).isoformat()}
    if markers:
        entry["markers"] = markers
    return entry


def _make_trace(tmp_path: Path, name: str = "trace.fst") -> Path:
    trace = tmp_path / name
    trace.write_bytes(_FAKE_TRACE_BYTES)
    return trace


_NO_VIEWER = (
    "ERROR: Booley has no waveform viewer until the web Dashboard's waveform "
    "view ships. The text queries still work: see `bwave --help`."
)


@pytest.fixture()
def no_viewer_host(monkeypatch, tmp_path):
    """A host with no editor CLI and an empty home, so no viewer can answer."""
    real_which = shutil.which
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **kw: (
            None
            if name in ("code", "code-insiders", "cursor", "codium")
            else real_which(name, *a, **kw)
        ),
    )


def _args(target: str | None = None, **kwargs) -> argparse.Namespace:
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
    assert not kwargs, f"unknown gui args: {kwargs}"
    return ns


def _gui(target: str | None = None, **kwargs) -> None:
    from booley.bwave import cli as bwave

    bwave.cmd_gui(_args(target, **kwargs))


def _plan(trace: Path, **kwargs):
    """The resolved selection `gui` checks, for a trace registered as the default."""
    from booley.bwave import cli as bwave

    return bwave._build_gui_plan(str(trace.resolve()), "_last", _args(**kwargs))


def _names(signals) -> list[str]:
    return [signal.bwave_name for signal in signals]


def _style(signals) -> list[tuple[str, str | None, str | None]]:
    return [(signal.bwave_name, signal.number_format, signal.color) for signal in signals]


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


# ── resolution (reuses _resolve_trace; keep lean) ─────────────────────────


def test_gui_unknown_alias_errors(tmp_path):
    _write_sessions({"dut": _make_entry(str(_make_trace(tmp_path)))})

    with pytest.raises(SystemExit) as exc:
        _gui("@ghost")

    assert "No registered alias" in str(exc.value)


def test_gui_no_session_errors():
    with pytest.raises(SystemExit) as exc:
        _gui()

    assert "register" in str(exc.value)


def test_gui_nonexistent_path_errors_not_falls_back(tmp_path):
    # `query` treats a nonexistent token as query args and uses the session
    # trace; `open` must refuse rather than silently open a different trace.
    _write_sessions({"_last": _make_entry(str(_make_trace(tmp_path)))})

    with pytest.raises(SystemExit) as exc:
        _gui(str(tmp_path / "missing.fst"))

    assert "not found" in str(exc.value)


def test_gui_empty_alias_errors_not_default_session(tmp_path):
    # `bwave open @` must not silently resolve `_last` — an empty alias is
    # a typo. (Guard lives in shared _resolve_trace, so `query` gets it too.)
    _write_sessions({"_last": _make_entry(str(_make_trace(tmp_path)))})

    with pytest.raises(SystemExit) as exc:
        _gui("@")

    assert "Empty alias" in str(exc.value)


def test_gui_directory_without_trace_errors(tmp_path):
    sim_dir = tmp_path / "empty_sim"
    sim_dir.mkdir()
    (sim_dir / "sim.log").write_text("no dump\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        _gui(str(sim_dir))

    assert str(exc.value) == f"ERROR: No trace file found in {sim_dir}"


def test_gui_directory_names_stray_vcd_dump(tmp_path):
    sim_dir = tmp_path / "it's work"
    sim_dir.mkdir()
    dump = sim_dir / "sim.vcd"
    dump.write_bytes(b"")

    with pytest.raises(SystemExit) as exc:
        _gui(str(sim_dir))

    message = str(exc.value)
    assert "not named trace.vcd: sim.vcd" in message
    assert "[flows.sim].trace_files" in message
    assert f"bwave register {quote_arg(str(dump))} --as ALIAS --build" in message
    assert "bwave gui" not in message


# ── format gate ────────────────────────────────────────────────────────────


def test_gui_legacy_bwave_rejected(tmp_path, capsys):
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


def test_gui_legacy_bwave_gate_is_case_insensitive(tmp_path, capsys):
    # A case-insensitive host FS resolves TRACE.BWAVE to a real .bwave file;
    # the gate must not be fooled by suffix casing.
    legacy = tmp_path / "TRACE.BWAVE"
    legacy.write_bytes(_FAKE_TRACE_BYTES)
    _write_sessions({"dut": _make_entry(str(legacy))})

    with pytest.raises(SystemExit) as exc:
        _gui("@dut")

    assert exc.value.code == 2
    assert "legacy .bwave" in capsys.readouterr().err


def _seed_default(tmp_path) -> Path:
    trace = _make_trace(tmp_path)
    _write_sessions({"_last": _make_entry(str(trace))})
    return trace


# ── selection (--signals / --group / --time / --cursor / --append) ──────────
#
# The Rust binary is faked at the subprocess seam with canned JSON envelopes:
# list → signal paths, value --at TOKEN → target_tick, stats → total_ticks.

# What `bwave list` prints: (name, width). The vector is the crux of this
# fixture — its name carries the bit range, which the plan splits off.
_CANNED_SIGNALS = [
    ("tb.dut.fifo.full", 1),
    ("tb.dut.fifo.empty", 1),
    ("tb.dut.fifo.count[3:0]", 4),
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
    if token.endswith("c"):
        return int(token[:-1]) * _TICKS_PER_CYCLE
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


@pytest.fixture()
def fake_rust(monkeypatch):
    """Fake the Rust bwave binary; returns the recorded query argv list."""
    from booley.bwave import cli as bwave

    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-rust"])
    state = {"signals": list(_CANNED_SIGNALS), "total_ticks": 1_000_000, "clock": "tb.clk"}
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
            if target_tick > state["total_ticks"]:
                # Like the binary: a time past the trace end is a usage error.
                return subprocess.CompletedProcess(
                    cmd,
                    2,
                    stdout="",
                    stderr=f"ERROR: value time {token} is beyond simulation range\n",
                )
            data = {
                "scope_prefix": "",
                "mode": "async" if async_mode else "sync",
                "at": target_tick if async_mode else cycles,
                "at_unit": "tick" if async_mode else "cycle",
                "target_tick": target_tick,
                "time_label": token,
                "signals": [],
            }
        elif sub == "find":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
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
        envelope = {"command": sub, "data": data, "warnings": state.get("warnings", [])}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr(bwave.subprocess, "run", fake_run)
    calls_and_state = state
    calls_and_state["calls"] = calls
    return calls_and_state


# ── no viewer: the criterion tests ──────────────────────────────────────────


def test_valid_selection_reports_no_viewer(tmp_path, fake_rust, no_viewer_host, capsys):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(
            signals=["tb.dut.fifo.*%h@red", "tb.nothing"],
            groups=["Ctl=tb.dut.fifo.empty"],
            time="100c:200c",
            cursor="150c",
        )

    assert exc.value.code == _NO_VIEWER
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no signals match --signals 'tb.nothing'" in captured.err
    assert {call[1] for call in fake_rust["calls"]} == {"list", "value"}


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("trace.fst", "@dut"),
        ("trace.fst", "{trace}"),
        ("trace.fst", "{directory}"),
        ("trace.vcd", "{trace}"),
    ],
    ids=["alias", "explicit-path", "sim-dir", "vcd-file"],
)
def test_gui_without_selection_reports_no_viewer(tmp_path, no_viewer_host, name, target):
    trace = _make_trace(tmp_path, name)
    _write_sessions({"dut": _make_entry(str(trace))})

    with pytest.raises(SystemExit) as exc:
        _gui(target.format(trace=trace, directory=tmp_path))

    assert exc.value.code == _NO_VIEWER


# ── selection resolution (checked through the plan `gui` builds) ───────────


def test_selection_resolves_clock_vector_and_range(tmp_path, fake_rust):
    plan = _plan(_seed_default(tmp_path), signals=["tb.dut.fifo.*"], time="100c:200c")

    assert _names(plan.signals) == [
        "tb.clk",
        "tb.dut.fifo.full",
        "tb.dut.fifo.empty",
        "tb.dut.fifo.count[3:0]",
    ]
    count = plan.signals[-1]
    assert (count.instance_path, count.msb, count.lsb) == ("tb.dut.fifo.count", 3, 0)
    assert plan.viewport == (1000, 2000)
    assert plan.markers == [(1000, "markerTime", "START"), (2000, "altMarkerTime", "END")]


@pytest.mark.parametrize(
    ("kwargs", "viewport", "markers"),
    [
        (
            {"time": "100c:200c", "cursor": "120c"},
            (1000, 2000),
            [(1200, "markerTime", "START"), (2000, "altMarkerTime", "END")],
        ),
        ({"cursor": "120c"}, None, [(1200, "markerTime", "cursor")]),
        (
            {"time": "100c:"},
            (1000, 1_000_000),
            [(1000, "markerTime", "START"), (1_000_000, "altMarkerTime", "END")],
        ),
    ],
    ids=["cursor-inside-range", "lone-cursor", "open-ended-range"],
)
def test_time_and_cursor_resolve_to_viewport_and_markers(
    tmp_path, fake_rust, kwargs, viewport, markers
):
    plan = _plan(_seed_default(tmp_path), append=True, **kwargs)

    assert (plan.viewport, plan.markers) == (viewport, markers)
    # `stats` walks every transition and times out on a large store.
    assert "stats" not in [call[1] for call in fake_rust["calls"]]


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        (
            ["Status=tb.dut.fifo.full", "Status=tb.dut.fifo.empty"],
            [("Status", ["tb.dut.fifo.full", "tb.dut.fifo.empty"])],
        ),
        (
            ["Backpressure=tb.dut.fifo.full", "Availability=tb.dut.fifo.empty"],
            [("Backpressure", ["tb.dut.fifo.full"]), ("Availability", ["tb.dut.fifo.empty"])],
        ),
    ],
    ids=["same-name-merges", "distinct-names-keep-order"],
)
def test_named_groups_merge_by_name_and_keep_requested_order(
    tmp_path, fake_rust, groups, expected
):
    fake_rust["signals"] = [("tb.dut.fifo.full", 1), ("tb.dut.fifo.empty", 1)]

    plan = _plan(_seed_default(tmp_path), groups=groups)

    assert [(group.name, _names(group.signals)) for group in plan.groups] == expected


def test_radix_and_color_suffixes_apply_to_flat_grouped_and_clock_rows(tmp_path, fake_rust):
    fake_rust["signals"] = [("tb.clk", 1), ("tb.dut.fifo.full", 1), ("tb.dut.fifo.count[3:0]", 4)]

    plan = _plan(
        _seed_default(tmp_path),
        signals=["tb.clk%b@red", "tb.dut.fifo.full%b@green"],
        groups=["State=tb.dut.fifo.count*%d@blue"],
    )

    # Annotations are wrapper syntax; only clean signal globs reach B-Wave.
    list_calls = [call for call in fake_rust["calls"] if call[1] == "list"]
    assert all("%" not in token and "@" not in token for call in list_calls for token in call)
    assert _style(plan.signals) == [
        ("tb.clk", "binary", "red"),
        ("tb.dut.fifo.full", "binary", "green"),
        ("tb.dut.fifo.count[3:0]", "decimal", "blue"),
    ]
    assert _style(plan.groups[0].signals) == [("tb.dut.fifo.count[3:0]", "decimal", "blue")]


@pytest.mark.parametrize(
    ("selector", "signal", "expected"),
    [
        ("words[56]%h@blue", ("tb.dut.words[56]", 1), ("tb.dut.words", 56, "hexadecimal", "blue")),
        ("tb.dut.mem*", ("tb.dut.mem[0]", 8), ("tb.dut.mem[0]", None, None, None)),
    ],
    ids=["exact-index-keeps-style", "wide-bracketed-identifier-is-not-split"],
)
def test_bracketed_names_split_only_when_they_are_a_bit_select(
    tmp_path, fake_rust, selector, signal, expected
):
    fake_rust["signals"] = [signal]

    (resolved,) = _plan(_seed_default(tmp_path), signals=[selector], no_clock=True).signals

    path, msb, number_format, color = expected
    assert (resolved.instance_path, resolved.msb, resolved.number_format, resolved.color) == (
        path,
        msb,
        number_format,
        color,
    )


@pytest.mark.parametrize(
    ("kwargs", "clock", "signals", "expected"),
    [
        ({"signals": ["tb.dut.fifo.full"]}, "tb.clk", [], ["tb.clk", "tb.dut.fifo.full"]),
        ({"signals": ["tb.dut.fifo.full"], "no_clock": True}, "tb.clk", [], ["tb.dut.fifo.full"]),
        ({"signals": ["tb.dut.fifo.full"], "append": True}, "tb.clk", [], ["tb.dut.fifo.full"]),
        ({"signals": ["tb.*"]}, "tb.clk", [("tb.clk", 1)], ["tb.clk", "tb.dut.fifo.full"]),
        ({"signals": ["tb.dut.fifo.full"]}, None, [], ["tb.dut.fifo.full"]),
    ],
    ids=["new-view", "no-clock", "append", "glob-matched-clock", "async-trace"],
)
def test_clock_is_row_one_unless_the_selection_opts_out(
    tmp_path, fake_rust, kwargs, clock, signals, expected
):
    fake_rust["clock"] = clock
    fake_rust["signals"] = [*signals, ("tb.dut.fifo.full", 1)]

    plan = _plan(_seed_default(tmp_path), **kwargs)

    assert _names(plan.signals) == expected
    # The clock rides on the `list` call gui already makes.
    assert [call[1] for call in fake_rust["calls"]] == ["list"]


def test_unmatched_signal_glob_warns_and_keeps_the_rest(tmp_path, fake_rust, capsys):
    fake_rust["signals"] = [("tb.dut.fifo.full", 1)]

    plan = _plan(_seed_default(tmp_path), signals=["tb.dut.fifo.full", "tb.nope"])

    assert _names(plan.signals) == ["tb.clk", "tb.dut.fifo.full"]
    err = capsys.readouterr().err
    assert "no signals match --signals 'tb.nope'" in err
    assert "tb.dut.fifo.full'" not in err


def test_max_signals_raises_the_cap(tmp_path, fake_rust):
    fake_rust["signals"] = [f"tb.dut.bus.bit{i}" for i in range(65)]

    plan = _plan(_seed_default(tmp_path), signals=["tb.dut.bus.*"], max_signals=100, no_clock=True)

    assert _names(plan.signals) == fake_rust["signals"]


# ── time tokens ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("time", "viewport"),
    [("10000t:20000t", (10000, 20000)), ("100ns:200ns", (100000, 200000))],
)
def test_absolute_time_tokens_resolve_exactly_on_a_clockless_trace(
    tmp_path, fake_rust, time, viewport
):
    fake_rust["clock"] = None

    plan = _plan(_seed_default(tmp_path), time=time, append=True, no_clock=True)

    assert plan.viewport == viewport
    assert all("--async" in call for call in fake_rust["calls"] if call[1] == "value")


def test_physical_time_range_is_not_shifted_by_sync_origin(tmp_path, fake_rust):
    plan = _plan(_seed_default(tmp_path), time="50ns:150ns", append=True)

    assert plan.viewport == (50000, 150000)
    assert all("--async" in call for call in fake_rust["calls"] if call[1] == "value")


def test_marker_names_resolve_in_time_range(tmp_path, fake_rust, capsys):
    trace = _make_trace(tmp_path)
    _write_sessions({"dut": _make_entry(str(trace), markers={"overflow": 1200, "1400c": 7})})
    from booley.bwave import cli as bwave

    plan = bwave._build_gui_plan(str(trace.resolve()), "dut", _args(time="overflow:1400c"))

    # The marker resolves to its cycle with an explicit 'c' suffix before
    # hitting the Rust converter.
    value_tokens = [c[c.index("--at") + 1] for c in fake_rust["calls"] if c[1] == "value"]
    assert value_tokens == ["1200c", "1400c"]
    assert plan.viewport == (1200 * _TICKS_PER_CYCLE, 1400 * _TICKS_PER_CYCLE)
    assert "marker '1400c' on @dut does not apply" in capsys.readouterr().err


def test_time_end_past_the_trace_is_clamped(tmp_path, fake_rust, capsys):
    end = fake_rust["total_ticks"]

    plan = _plan(_seed_default(tmp_path), time="100c:999999c", append=True)

    assert plan.viewport == (1000, end)
    assert f"--time end 999999c is past the end of the trace; showing up to tick {end}" in (
        capsys.readouterr().err
    )


# ── selector errors are still reported ──────────────────────────────────────


def test_invalid_display_suffix_is_a_usage_error(tmp_path, fake_rust, capsys):
    _seed_default(tmp_path)

    for selector, message in [
        ("tb.dut.fifo.full%u", "unknown radix suffix '%u'"),
        ("tb.dut.fifo.full@orange", "unknown waveform color '@orange'"),
        ("tb.dut.fifo.full@red%h", "radix must precede color"),
    ]:
        with pytest.raises(SystemExit) as exc:
            _gui(signals=[selector])

        assert exc.value.code == 2
        assert message in capsys.readouterr().err
    assert fake_rust["calls"] == []


def test_group_rejects_a_missing_name_glob_separator(tmp_path, capsys):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(groups=["Status"])

    assert exc.value.code == 2
    assert "NAME=GLOB" in capsys.readouterr().err


def test_group_rejects_html_control_characters_in_the_group_name(tmp_path, capsys):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(groups=["<script>=tb.dut.fifo.full"])

    assert exc.value.code == 2
    assert "cannot contain" in capsys.readouterr().err


def test_glob_without_match_errors_before_the_viewer(tmp_path, fake_rust, capsys):
    _seed_default(tmp_path)
    fake_rust["signals"] = []

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.nope.*"])

    # Caller-input class: exit 2, message on stderr — same contract as the
    # Rust binary's total-miss (see _bwave_contract).
    assert exc.value.code == 2
    assert "no signals match" in capsys.readouterr().err


def test_glob_total_miss_relays_the_binary_suggestion(tmp_path, fake_rust, capsys):
    """The anchored form the binary suggests rides on gui's own total-miss error."""
    _seed_default(tmp_path)
    fake_rust["signals"] = []
    fake_rust["warnings"] = ["did you mean 'tb.dut.fifo.*'?", "unrelated warning"]

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["fifo.*"])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no signals match --signals 'fifo.*'" in err
    assert "; did you mean 'tb.dut.fifo.*'?" in err
    assert "unrelated warning" not in err


def test_signal_cap_is_a_hard_error(tmp_path, fake_rust, capsys):
    _seed_default(tmp_path)
    fake_rust["signals"] = [f"tb.dut.bus.bit{i}" for i in range(65)]

    with pytest.raises(SystemExit) as exc:
        _gui(signals=["tb.dut.bus.*"])

    assert exc.value.code == 2
    msg = capsys.readouterr().err
    assert "65 signals" in msg and "--max-signals" in msg


def test_signal_cap_counts_flat_and_grouped_selections_together(tmp_path, fake_rust, capsys):
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


def test_time_starting_past_the_trace_fails_before_the_viewer(tmp_path, fake_rust):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(time="200000c:300000c")

    assert "--time start 200000c is past the end of the trace" in str(exc.value)
    assert "--at-time" not in str(exc.value)  # no hint aimed at `bwave value`


def test_cursor_past_the_trace_fails_before_the_viewer(tmp_path, fake_rust):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(time="100c:200c", cursor="999999c")

    assert "--cursor 999999c is past the end of the trace" in str(exc.value)


def test_empty_time_range_rejected(tmp_path, fake_rust):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(time="200c:100c", append=True)

    assert "empty --time range" in str(exc.value)


def test_time_without_colon_rejected(tmp_path, fake_rust):
    _seed_default(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _gui(time="150c", append=True)

    assert "START:END" in str(exc.value)


def test_append_with_nothing_to_do_errors(tmp_path, fake_rust):
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
    failing_rust["stderr"] = "ERROR: could not open store: bad magic\n"

    with pytest.raises(SystemExit) as exc:
        _gui(time="100c:200c", append=True)

    assert str(exc.value) == "ERROR: could not open store: bad magic"


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
    retry_args = " ".join(quote_arg(arg) for arg in ["@dut", "-s", "tb.dut.fifo.*"])
    assert f"bwave gui {retry_args}" in msg


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
    """`python -m booley.bwave.cli gui @dut` end-to-end: routed, then no viewer.

    Covers the argv-routing seam (_KNOWN_COMMANDS, dispatch) that in-process
    Namespace tests bypass. A stub `code` on PATH proves no editor is started.
    """
    _write_sessions({"dut": _make_entry(str(_make_trace(tmp_path)))})
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    argv_log = tmp_path / "code_argv.txt"
    stub = stub_dir / ("code.cmd" if sys.platform == "win32" else "code")
    if sys.platform == "win32":
        stub.write_bytes(f'@echo off\r\necho %* > "{argv_log}"\r\n'.encode())
    else:
        stub.write_bytes(f'#!/bin/sh\necho "$@" > "{argv_log}"\n'.encode())
        stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = str(stub_dir) + os.pathsep + env["PATH"]
    env["PYTHONPATH"] = str(BOOLEY_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["BOOLEY_CONTAINER"] = "1"
    env["HOME"] = str(tmp_path / "home")

    r = run_bounded_child([sys.executable, "-m", "booley.bwave.cli", "gui", "@dut"], env=env)

    assert r.returncode == 1
    assert _NO_VIEWER in r.stderr
    assert not argv_log.exists(), "gui started the stub `code` CLI"


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
    run_bounded_child([str(binary), "build", str(vcd), "-o", str(fst)], check=True)
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


def test_list_envelope_refuses_native_usage_error(monkeypatch, capsys):
    """#4898: a native exit 2 (a bit-slice selector) makes ``gui`` exit 2."""
    from booley.bwave import cli as bwave

    failed = subprocess.CompletedProcess([], 2, "", "bit range [9:8] exceeds width 8")
    monkeypatch.setattr(bwave, "_run_query", lambda _args: failed)

    with pytest.raises(SystemExit) as raised:
        bwave._list_envelope("trace.fst", ["data[9:8]"])

    assert raised.value.code == 2
    assert "exceeds width 8" in capsys.readouterr().err


@pytest.mark.parametrize(("cycle", "bound"), [(25, "250"), (200_000, "1000001")])
def test_async_marker_bound_is_its_cycles_tick(fake_rust, tmp_path, cycle, bound):
    """Async `--before` takes a tick: a Marker is passed as the tick of its cycle."""
    from booley.bwave import cli as bwave

    _write_sessions({"dut": _make_entry(str(_make_trace(tmp_path)), {"m": cycle})})

    argv = ["@dut", "find", "sig", "1", "--before", "m", "--async", "--with-reset"]
    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=argv))

    assert exc.value.code == 0
    *probes, final = fake_rust["calls"]
    assert final[1] == "find"
    assert ("--before", bound) in itertools.pairwise(final)
    value_probe = next(call for call in probes if call[1] == "value")
    assert "--async" in value_probe
    assert "--with-reset" in value_probe
