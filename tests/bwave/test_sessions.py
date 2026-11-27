"""Tests for the B-Wave session (register --as / query @alias) workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import booley
from booley.bwave import cli, sessions
from booley.core.host_shell import quote_arg
from booley.dev_support.test_profiles import native_test_binary
from tests.bwave.native_binary import fake_native_binary
from tests.bwave.native_binary import require_native_binary as _native_bwave_binary
from tests.bwave.session_store_support import (
    isolate_temporary_store,
    stored_sessions,
    write_sessions,
)
from tests.conftest import FST_HEADER_BYTES, MINIMAL_FST_BYTES
from tests.path_tool_fake import PathToolFake
from tests.sharing_error_support import hold_replaces
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import hang_guard_s

BOOLEY_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = BOOLEY_ROOT / "crates" / "bwave" / "tests" / "fixtures"
BWAVE = FIXTURE_DIR / "test_distance.test.fst"


@pytest.fixture(autouse=True)
def _inside_session_runtime(monkeypatch):
    """bwave is container-only (ADR 0028); simulate the Sandbox.

    Covers both in-process cli.main() calls and the _query() subprocess
    (which inherits os.environ), so the venue guard passes on a host machine.
    """
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


@pytest.fixture(autouse=True)
def session_file(monkeypatch, tmp_path) -> Path:
    """An empty temporary-directory session store of the test's own."""
    return isolate_temporary_store(monkeypatch, tmp_path)


@pytest.fixture
def fake_native(monkeypatch, tmp_path) -> PathToolFake:
    return fake_native_binary(tmp_path / "native", monkeypatch)


def _ensure_bwave_fixture() -> None:
    native_bwave = _native_bwave_binary()
    if BWAVE.exists():
        return
    src = FIXTURE_DIR / "test_distance.vcd"
    if not src.exists():
        pytest.skip("test_distance.vcd fixture missing")
    result = run_bounded_child(
        [str(native_bwave), "build", str(src), "-o", str(BWAVE)], timeout=2 * hang_guard_s()
    )
    if result.returncode != 0:
        pytest.fail(f"failed to build {BWAVE}: {result.stderr}")


def _make_entry(trace: str, **overrides) -> dict:
    return {
        "trace": trace,
        "registered_at": datetime.now(UTC).isoformat(),
        **overrides,
    }


def _registered(tmp_path: Path, markers: dict | None = None, alias: str = "dut") -> Path:
    """A registered trace file under *alias*, with *markers* when given."""
    trace = tmp_path / "trace.fst"
    trace.write_bytes(MINIMAL_FST_BYTES)
    entry = _make_entry(str(trace))
    if markers is not None:
        entry["markers"] = markers
    write_sessions({alias: entry})
    return trace


def _forwarded(fake_native: PathToolFake, *extra: str) -> list[str]:
    """Run ``bwave`` query arguments; return the arguments the native binary got last."""
    fake_native.answer()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_query(argparse.Namespace(extra=list(extra)))
    assert exc.value.code == 0
    return fake_native.calls()[-1].args


def _list_answer(*, roots: list[str], signals: int) -> str:
    """The ``bwave list --format json`` envelope registration reads identity from."""
    data = {"scope_prefix": "", "root_scopes": roots, "signal_count": signals, "total_ticks": 5}
    return json.dumps({"data": data})


def _main(monkeypatch, capsys, *argv: str) -> str:
    """Run ``bwave`` *argv*, expect exit 0 and return stdout."""
    monkeypatch.setattr(sys, "argv", ["bwave", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    return capsys.readouterr().out


def _query(*extra_args: str) -> subprocess.CompletedProcess[str]:
    # bwave is container-only (ADR 0028); mark the venue so the subprocess
    # exercises the query surface rather than dying on the host guard.
    env = {
        **os.environ,
        "BOOLEY_CONTAINER": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(BOOLEY_ROOT), str(BOOLEY_ROOT / "src"), os.environ.get("PYTHONPATH", "")]
        ),
    }
    return run_bounded_child(
        [
            sys.executable,
            "-m",
            "tests.bwave.native_binary",
            str(native_test_binary(BOOLEY_ROOT)),
            "query",
            *extra_args,
        ],
        env=env,
    )


@pytest.mark.parametrize(
    ("argv", "present", "absent"),
    [
        (
            ["--help"],
            [
                "RTL debug helper for simulation traces.",
                # QA-8: a directly passed .vcd must be built before querying.
                "auto-builds an .fst store",
                "bwave build",
                "bwave [@ALIAS | TRACE_PATH] COMMAND [ARGS...] [OPTIONS...]",
                "Trace commands:",
                "Common options:",
                "value --at T",
                "bwave <subcommand> --help",
                "bwave --version",
            ],
            [
                "bwave markers [@ALIAS]",
                "bwave query [@ALIAS | TRACE_PATH]",
                "Query subcommands:",
                "Common query options:",
                "{register,markers,query}",
            ],
        ),
        (
            ["query", "--help"],
            ["usage: bwave [@ALIAS | TRACE_PATH] <subcommand>", "bwave <subcommand> --help"],
            ["usage: bwave query", "bwave --help` (Rust binary)"],
        ),
    ],
    ids=["top", "query"],
)
def test_help_surfaces(monkeypatch, capsys, argv, present, absent):
    out = _main(monkeypatch, capsys, *argv)

    assert [text for text in present if text not in out] == []
    assert [text for text in absent if text in out] == []


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flags_answer(monkeypatch, capsys, flag):
    """The version flags used to fall through to the query translator (F-29c)."""
    out = _main(monkeypatch, capsys, flag)

    assert booley.__version__ in out
    assert "bwave (booley " in out
    assert "usage" not in out.lower()


def test_direct_subcommand_help_forwards_to_native_bwave(monkeypatch, capsys, fake_native):
    fake_native.answer()

    _main(monkeypatch, capsys, "value", "--help")

    assert [call.args for call in fake_native.calls()] == [["value", "--help"]]


def test_register_keeps_an_unreadable_store_aside(capsys, session_file):
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("not json {{{")

    sessions.register_trace(Path("/tmp/a.fst"), alias="a")

    kept = list(session_file.parent.glob("bwave_sessions.json.unreadable-*"))
    assert [path.read_text() for path in kept] == ["not json {{{"]
    assert "a" in stored_sessions()
    assert str(kept[0]) in capsys.readouterr().err


def test_register_leaves_a_store_it_cannot_move_aside(monkeypatch, session_file):
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("not json {{{")

    def refuse(path):
        raise PermissionError("denied")

    # No public entry point reaches this: the move must fail while the store stays readable,
    # and its timestamped target name cannot be held in advance.
    monkeypatch.setattr(sessions, "keep_unreadable", refuse)
    with pytest.raises(OSError, match="denied"):
        sessions.register_trace(Path("/tmp/a.fst"), alias="a")

    assert session_file.read_text() == "not json {{{"


def test_register_does_not_replace_a_store_it_cannot_read(session_file):
    session_file.mkdir(parents=True)

    with pytest.raises(OSError):
        sessions.register_trace(Path("/tmp/a.fst"), alias="a")

    assert session_file.is_dir()


# ── query via session ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("extra", "forwarded"),
    [
        (["build", "input.vcd", "-o", "out.fst"], ["build", "input.vcd", "-o", "out.fst"]),
        (["@missing", "schema"], ["schema"]),
        # `list` has a smaller default: its tree must fit the caller's output window.
        (["list", "{trace}"], ["list", "{trace}", "--limit", "400"]),
        (["signal", "{trace}", "-s", "*"], ["signal", "{trace}", "-s", "*", "--limit", "2000"]),
    ],
    ids=["build", "meta", "list", "signal"],
)
def test_query_forwards_to_the_native_binary(tmp_path, fake_native, extra, forwarded):
    trace = tmp_path / "trace.fst"
    trace.write_bytes(MINIMAL_FST_BYTES)

    def filled(args: list[str]) -> list[str]:
        return [str(trace) if arg == "{trace}" else arg for arg in args]

    assert _forwarded(fake_native, *filled(extra)) == filled(forwarded)


def test_grep_propagates_inner_exit_code(tmp_path, fake_native):
    """--grep must not swallow the inner `list` exit code (bad glob = rc 2)."""
    _registered(tmp_path)
    fake_native.answer("list", returncode=2)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_query(argparse.Namespace(extra=["@dut", "--grep", "[oops"]))

    assert exc.value.code == 2


_NO_SESSION_HELP = [
    "No trace path given and no registered session",
    "bwave register SIM_DIR --as ALIAS",
    "booley flow sim",
    "--trace",
    "trace: true",
]


@pytest.mark.parametrize(
    ("store", "extra", "messages"),
    [
        (None, ["wave", "-t", "1:5"], _NO_SESSION_HELP),
        ("not json {{{", ["wave", "-t", "1:5"], _NO_SESSION_HELP),
        (
            {"baseline": {"trace": "x.fst"}},
            ["@nonexistent", "wave"],
            ["No registered alias '@nonexistent'", "@baseline"],
        ),
        ({"dut": {"registered_at": "2026-05-14T12:00:00+00:00"}}, ["@dut", "list"], ["malformed"]),
        ({"dut": "not-an-object"}, ["@dut", "list"], ["malformed"]),
        ({"dut": {"trace": "/nonexistent/x.fst"}}, ["@dut", "list"], ["no longer exists"]),
    ],
    ids=["no-store", "unreadable", "unknown-alias", "no-trace-key", "non-object", "missing-trace"],
)
def test_query_refuses_a_session_it_cannot_resolve(
    session_file, fake_native, store, extra, messages
):
    if isinstance(store, str):
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(store, encoding="utf-8")
    elif store is not None:
        write_sessions(store)
    fake_native.answer()

    with pytest.raises(SystemExit) as exc:
        cli.cmd_query(argparse.Namespace(extra=list(extra)))

    message = str(exc.value)
    assert [text for text in messages if text not in message] == []
    assert fake_native.calls() == []


@pytest.mark.parametrize(
    ("age_days", "registered_at", "warns"),
    [
        # Registering an old file now does not make its data younger.
        (2, None, True),
        # Re-simulating in place refreshes the data, whatever the registration says.
        (0, "2026-05-12T00:00:00+00:00", False),
        (0, "yesterday-ish", False),
    ],
    ids=["old-data", "old-registration", "bad-timestamp"],
)
def test_query_warns_on_stale_trace_data(
    tmp_path, capsys, fake_native, age_days, registered_at, warns
):
    trace = tmp_path / "trace.fst"
    trace.write_bytes(MINIMAL_FST_BYTES)
    written = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
    os.utime(trace, (written, written))
    overrides = {} if registered_at is None else {"registered_at": registered_at}
    write_sessions({"dut": _make_entry(str(trace), **overrides)})

    assert _forwarded(fake_native, "@dut", "list")[:2] == ["list", str(trace)]

    assert ("older than 24h" in capsys.readouterr().err) is warns


@pytest.mark.parametrize(
    ("trace", "registered_at", "warns"),
    [
        (None, None, False),
        (12345, None, False),
        ("no/such/trace.fst", None, False),
        ("no/such/trace.fst", "not-a-timestamp", False),
        ("no/such/trace.fst", "2000-01-01T00:00:00+00:00", True),
    ],
)
def test_stale_check_falls_back_to_the_registration_time(capsys, trace, registered_at, warns):
    # Private: a query stats the trace right after `exists()` succeeds, so the registration
    # fallback and its bad-input guards run only when the file vanishes in between.
    cli._warn_if_trace_stale(trace, registered_at)

    assert ("older than 24h" in capsys.readouterr().err) is warns


@pytest.mark.native_bwave
@pytest.mark.parametrize(
    ("stored", "args", "signal"),
    [
        ({"_last": "{trace}"}, ["--wave", "-t", "1:10", "-s", "*req*"], "req"),
        (
            {"baseline": "{trace}", "_last": "/nonexistent/path.fst"},
            ["@baseline", "--wave", "-t", "1:10", "-s", "*req*"],
            "req",
        ),
        (
            {"_last": "/nonexistent/path.fst"},
            ["{trace}", "--wave", "-t", "1:5", "-s", "*ack*"],
            "ack",
        ),
    ],
    ids=["default", "alias", "explicit"],
)
def test_query_resolves_named_and_explicit_traces(stored, args, signal):
    _ensure_bwave_fixture()
    trace = str(BWAVE.resolve())
    write_sessions(
        {alias: _make_entry(path.replace("{trace}", trace)) for alias, path in stored.items()}
    )

    r = _query(*(arg.replace("{trace}", trace) for arg in args))

    assert r.returncode == 0, r.stderr
    assert signal in r.stdout


def test_register_dir_names_stray_vcd_dump(tmp_path):
    dump = tmp_path / "sim.vcd"
    dump.write_bytes(b"")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(argparse.Namespace(sim_dir=str(tmp_path), alias="dut"))
    assert str(exc.value) == (
        f"ERROR: No trace file found in {tmp_path}\n"
        "VCD dumps here that are not named trace.vcd: sim.vcd\n"
        "Declare the dump in [flows.sim].trace_files and re-run the sim, "
        "or pass the file directly (a raw VCD needs --build to register):\n"
        f"  bwave register {quote_arg(str(dump))} --as ALIAS --build"
    )


def test_register_lists_five_stray_vcds_and_counts_the_rest(tmp_path):
    for name in "gfedcba":
        (tmp_path / f"{name}.vcd").write_bytes(b"")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(argparse.Namespace(sim_dir=str(tmp_path), alias="dut"))
    assert "a.vcd, b.vcd, c.vcd, d.vcd, e.vcd and 2 more" in str(exc.value)
    assert "f.vcd" not in str(exc.value)


def test_register_missing_trace_includes_incident(tmp_path):
    (tmp_path / "trace_status.json").write_text(
        '{"current_status": "failed", "failure_reason": "no artifact"}\n',
        encoding="utf-8",
    )
    (tmp_path / "trace_incident.txt").write_text(
        "reason: trace requested but no queryable .fst store or .vcd was produced\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(argparse.Namespace(sim_dir=str(tmp_path), alias="dut"))
    assert "No trace file found" in str(exc.value)
    assert "trace_status.json" in str(exc.value)
    assert "failure_reason" in str(exc.value)


def test_register_raw_vcd_refuses_and_registers_nothing(tmp_path, capsys, session_file):
    """A raw .vcd is refused non-zero — never bound as an unqueryable alias.

    The query engine needs a built store, so a VCD alias can only ever answer
    "requires a built waveform store". Registering it rc=0 handed scripted
    callers a green light for a command that could not work (F-32c).
    """
    vcd = tmp_path / "it's waveform.vcd"
    vcd.write_text("$enddefinitions $end\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(argparse.Namespace(sim_dir=str(vcd), alias="dut", build=False))
    assert exc.value.code == 2, "caller-input refusal must use the usage exit code"
    msg = capsys.readouterr().err
    assert "raw VCD" in msg
    assert "bwave build" in msg
    assert "waveform.fst" in msg
    assert f"bwave register {quote_arg(str(vcd))} --as ALIAS --build" in msg
    assert not session_file.exists(), "a refused registration must not write a session"


@pytest.mark.parametrize("outcome", ["built", "failed"])
def test_register_raw_vcd_build(tmp_path, capsys, session_file, fake_native, outcome):
    """`--build` converts the VCD and registers the store, or registers nothing."""
    name = "waveform.vcd" if outcome == "built" else "it's waveform.vcd"
    vcd = tmp_path / name
    vcd.write_text("$enddefinitions $end\n", encoding="utf-8", newline="\n")
    store = vcd.with_suffix(".fst")
    args = argparse.Namespace(sim_dir=str(vcd), alias="dut", build=True)
    if outcome == "built":
        fake_native.answer("build", output=MINIMAL_FST_BYTES)
        fake_native.answer("list", stdout=_list_answer(roots=["tb"], signals=1))

        cli.cmd_register(args)

        assert stored_sessions()["dut"]["trace"] == str(store.resolve())
        assert "wrote waveform.fst" in capsys.readouterr().out
        return
    fake_native.answer("build", returncode=1, stderr="bad conversion")

    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(args)

    assert "could not build a queryable store" in str(exc.value)
    assert f"`bwave build {quote_arg(str(vcd))} -o {quote_arg(str(store))}`" in str(exc.value)
    log = tmp_path / "trace.fst.stderr"
    assert "bad conversion" in log.read_text(encoding="utf-8")
    assert not session_file.exists()


def test_register_build_refuses_to_clobber_an_existing_store(
    tmp_path, capsys, session_file, fake_native
):
    """`--build` writes <name>.fst unscoped; an existing one may be scoped.

    Nothing on disk distinguishes a store someone built with `bwave build
    --scope tb.dut` from one register would write, so overwriting it silently
    is a data loss the user never asked for by typing `register`.
    """
    vcd = tmp_path / "it's waveform.vcd"
    vcd.write_text("$enddefinitions $end\n", encoding="utf-8")
    existing = vcd.with_suffix(".fst")
    existing.write_bytes(b"precious-scoped-store")

    fake_native.answer()

    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(argparse.Namespace(sim_dir=str(vcd), alias="dut", build=True))

    assert exc.value.code == 2, "caller-input refusal must use the usage exit code"
    msg = capsys.readouterr().err
    assert "refusing to overwrite" in msg
    assert "--scope" in msg
    assert f"bwave register {quote_arg(str(existing))}" in msg  # the register-it-as-is way out
    assert f"bwave build {quote_arg(str(vcd))} -o {quote_arg(str(existing))}" in msg
    assert fake_native.calls() == [], "build ran despite the refusal"
    assert existing.read_bytes() == b"precious-scoped-store"
    assert not session_file.exists()


def test_register_sim_dir_falling_back_to_vcd_shows_why_conversion_failed(
    tmp_path, capsys, session_file, fake_native
):
    """A sim dir only resolves to a raw VCD when its own VCD→FST already failed.

    Refusing is right (the alias would be unqueryable either way), but pointing
    at `bwave build` without a word about the failure sends the user at the
    command that just lost. The trace manifest carries the reason — show it.
    """
    (tmp_path / "trace.vcd").write_text("$enddefinitions $end\n", encoding="utf-8")
    (tmp_path / "trace_status.json").write_text(
        '{"failure_reason": "bwave binary not found"}', encoding="utf-8"
    )
    fake_native.answer("build", returncode=1, stderr="bad conversion")

    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(argparse.Namespace(sim_dir=str(tmp_path), alias="dut", build=False))

    assert exc.value.code == 2, "caller-input refusal must use the usage exit code"
    msg = capsys.readouterr().err
    assert "raw VCD" in msg
    assert "bwave binary not found" in msg
    assert not session_file.exists()


def test_register_rejects_header_only_fst(tmp_path, capsys, session_file):
    """Direct-file register must refuse a header-only (zero-signal) store.

    The suffix check alone let a Verilator auto---main artifact — valid FST
    header, no signal data — get registered, after which every query
    "succeeded" with empty output. Registration is the cheap moment to
    catch it, with an error that names the file and the cause.
    """
    trace = tmp_path / "trace.fst"
    trace.write_bytes(FST_HEADER_BYTES)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(argparse.Namespace(sim_dir=str(trace), alias="dut"))
    assert exc.value.code == 2, "caller-input refusal must use the usage exit code"
    msg = capsys.readouterr().err
    assert "trace.fst" in msg, msg
    assert "no signal data" in msg, msg
    assert "header-only" in msg, msg
    # Nothing may be registered on failure.
    assert not session_file.exists()


def test_register_dir_with_header_only_store_names_the_real_problem(tmp_path, capsys):
    """A sim dir whose only .fst is header-only must not just say "no trace".

    find_trace filters stores through structural inspection, so the header-only file
    was invisible: the search fell to "No trace file found" (or blamed a raw
    VCD) while the actual problem — an unqueryable store sitting right there
    — went unmentioned.
    """
    (tmp_path / "trace.fst").write_bytes(FST_HEADER_BYTES)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_register(argparse.Namespace(sim_dir=str(tmp_path), alias="dut"))
    msg = str(exc.value)
    assert "No trace file found" in msg, msg
    assert "trace.fst" in msg, msg
    assert "header-only" in msg, msg
    capsys.readouterr()  # drain


_STORE_LOAD_CAUSE = (
    "ERROR: cannot load waveform store 'cut.fst': store is truncated; "
    "re-run the simulation or rebuild it with `bwave build <trace.vcd> -o cut.fst`"
)
_UNREADABLE = ["could not read a top scope", "header-only"]


@pytest.mark.parametrize(
    ("answer", "present", "absent"),
    [
        # A multi-root Cocotb trace is not header-only.
        (
            {"stdout": _list_answer(roots=["$rootio", "uart16550"], signals=148)},
            ["top scopes: $rootio, uart16550"],
            # A directly registered store needs no build step, so no build hint either.
            ["header-only", "bwave build"],
        ),
        # #3648: bwave's cause line is printed, not only the guess.
        (
            {"returncode": 1, "stderr": f"noise\n{_STORE_LOAD_CAUSE}\n"},
            [f"[bwave] WARNING: {_STORE_LOAD_CAUSE}", *_UNREADABLE],
            ["top scope:"],
        ),
        (
            {"stdout": _list_answer(roots=[], signals=0)},
            _UNREADABLE,
            ["top scope:", "bwave build"],
        ),
    ],
    ids=["two-roots", "cause", "zero-signals"],
)
def test_register_reports_what_the_store_holds(
    tmp_path, capsys, fake_native, answer, present, absent
):
    trace = tmp_path / "trace.fst"
    trace.write_bytes(MINIMAL_FST_BYTES)
    written = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(trace, (written, written))
    fake_native.answer("list", **answer)

    cli.cmd_register(argparse.Namespace(sim_dir=str(trace), alias="dut"))

    err = capsys.readouterr().err
    assert [text for text in [*present, "(written 2d ago)"] if text not in err] == []
    assert [text for text in absent if text in err] == []


def test_register_omits_the_age_of_a_trace_that_vanished(
    tmp_path, monkeypatch, capsys, fake_native
):
    trace = tmp_path / "trace.fst"
    trace.write_bytes(MINIMAL_FST_BYTES)
    fake_native.answer("list", stdout=_list_answer(roots=["tb"], signals=1))

    def remove_trace(path, alias):
        path.unlink()
        return alias

    # The trace must vanish between registration and the age stat; no real process
    # can be timed into that window.
    monkeypatch.setattr(sessions, "register_trace", remove_trace)
    cli.cmd_register(argparse.Namespace(sim_dir=str(trace), alias="dut"))

    err = capsys.readouterr().err
    assert "top scope: tb" in err
    assert "written" not in err


def test_register_trace_raises_when_the_store_cannot_be_written(
    tmp_path, monkeypatch, capsys, session_file, fake_native
):
    trace = tmp_path / "trace.fst"
    trace.write_bytes(MINIMAL_FST_BYTES)
    fake_native.answer("list", stdout=_list_answer(roots=["tb"], signals=1))
    hold_replaces(monkeypatch, {session_file: 6})  # three attempts per registration

    with pytest.raises(OSError, match="could not write session file"):
        sessions.register_trace(trace, alias="dut")

    # `bwave register` keeps its old behavior: warn, still report the trace.
    capsys.readouterr()
    cli.cmd_register(argparse.Namespace(sim_dir=str(trace), alias="dut"))
    captured = capsys.readouterr()
    assert f"@dut -> {trace}" in captured.out
    assert "could not write session file" in captured.err
    assert not session_file.exists()


def test_register_trace_updates_last_and_keeps_markers(tmp_path):
    a, b, c = (tmp_path / f"{name}.fst" for name in "abc")
    for trace in (a, b, c):
        trace.write_bytes(MINIMAL_FST_BYTES)
    sessions.register_trace(a, alias="a")
    sessions.register_trace(b, alias="b")
    stored = stored_sessions()
    assert stored["a"]["trace"] == str(a.resolve())
    assert stored["b"]["trace"] == stored["_last"]["trace"] == str(b.resolve())
    cli.cmd_markers(argparse.Namespace(extra=["@a", "set", "start", "10"]))
    cli.cmd_markers(argparse.Namespace(extra=["set", "last_only", "99"]))

    sessions.register_trace(c, alias="a")

    stored = stored_sessions()
    assert stored["a"]["trace"] == stored["_last"]["trace"] == str(c.resolve())
    assert stored["a"]["markers"] == {"start": 10}
    assert stored["_last"]["markers"] == {"last_only": 99}
    assert stored["b"]["trace"] == str(b.resolve())


@pytest.mark.native_bwave
def test_register_reports_trace_identity_and_age(capsys):
    """`register` must say which design it just bound, and how old it is.

    A shared-container cache collision once bound an AXI-bridge query to an
    unrelated serial-comm testbench's waveform; nothing in the register
    output hinted at the mismatch, so the agent debugged a design it was not
    working on. The top scope is the design's identity — print it.
    """
    _ensure_bwave_fixture()

    cli.cmd_register(argparse.Namespace(sim_dir=str(BWAVE), alias="dut"))
    err = capsys.readouterr().err
    assert "top scope:" in err, err
    assert "written" in err, err
    assert "confirm this is the design under test" in err, err


@pytest.mark.parametrize(
    ("name", "hint"),
    [
        ("123", "t123"),
        ("5c", "at_5c"),
        ("10ns", "at_10ns"),
        ("7t", "at_7t"),
        ("3us", "at_3us"),
        ("+5c", "at_+5c"),
        ("a:b", "a_b"),
        (":x", "_x"),
        ("\uff11\uff12\uff13", None),
        ("", None),
        ("-bad", None),
        ("has space", None),
        ("wave", None),
    ],
)
def test_markers_set_refuses_names_typed_arguments_claim(tmp_path, name, hint):
    _registered(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_markers(argparse.Namespace(extra=["@dut", "set", name, "10"]))

    message = str(exc.value)
    assert "invalid marker name" in message
    if hint is not None:
        assert f"'{hint}'" in message
    assert "markers" not in stored_sessions()["dut"]


@pytest.mark.parametrize("name", ["t12", "irq_start", "cyc_5", "5C"])
def test_markers_set_accepts_names_with_a_label(tmp_path, name):
    _registered(tmp_path)

    cli.cmd_markers(argparse.Namespace(extra=["@dut", "set", name, "10"]))

    assert stored_sessions()["dut"]["markers"] == {name: 10}


def test_markers_set_list_delete(tmp_path, capsys):
    # A session written before `set` refused time-token names holds one (#4735).
    _registered(tmp_path, {"5c": 30})

    for name, cycle in (("done", "20"), ("start", "10")):
        cli.cmd_markers(argparse.Namespace(extra=["@dut", "set", name, cycle]))
    capsys.readouterr()
    cli.cmd_markers(argparse.Namespace(extra=["@dut", "list"]))
    out = capsys.readouterr().out
    assert out.index("start") < out.index("done") < out.index("5c")

    for name in ("start", "5c"):
        cli.cmd_markers(argparse.Namespace(extra=["@dut", "delete", name]))
    assert stored_sessions()["dut"]["markers"] == {"done": 20}


# (subcommand, typed arguments, forwarded arguments, name warned about)
_MARKER_ROWS: list[tuple[list[str], list[str], list[str], str | None]] = [
    (["find", "sig", "1"], ["-t", "start:done"], ["-t", "10c:20c"], None),
    (["find", "sig", "1"], ["--before", "done"], ["--before", "20"], None),
    (["find", "sig", "1"], ["--after", "start"], ["--after", "10"], None),
    (["find", "sig", "1"], ["--before", "done", "--count"], ["--before", "20", "--count"], None),
    (["find", "sig", "1"], ["--before=done"], ["--before=20"], None),
    (["find", "sig", "1"], ["--time=start:done"], ["--time=10c:20c"], None),
    (["find", "sig", "1"], ["-tstart:done"], ["-t10c:20c"], None),
    (["find", "sig", "1"], ["-t=start:"], ["-t=10c:"], None),
    (["value"], ["--at=start"], ["--at=10c"], None),
    (
        ["find", "sig", "1"],
        ["--before", "start", "--before", "done"],
        ["--before", "10", "--before", "20"],
        None,
    ),
    (["find", "sig", "1"], ["--before"], ["--before"], None),
    ([], ["--diff", "start", "done"], ["10c", "20c"], None),
    (["diff"], ["start", "done"], ["10c", "20c"], None),
    (["diff"], ["-s", "start", "start", "done"], ["-s", "start", "10c", "20c"], None),
    (["diff"], ["--async", "start", "done"], ["--async", "10c", "20c"], None),
    (["value"], ["--at", "start"], ["--at", "10c"], None),
    (["find", "sig", "1"], ["-t", "unknown:done"], ["-t", "unknown:20c"], None),
    (["find", "sig", "1"], ["-t", "start:10ns"], ["-t", "10c:10ns"], "10ns"),
    (["diff"], ["5c", "done"], ["5c", "20c"], "5c"),
    ([], ["--diff", "10ns", "start"], ["10ns", "10c"], "10ns"),
    (["find", "sig", "1"], ["--before", "5c"], ["--before", "5c"], "5c"),
    (["find", "sig", "1"], ["--after=10ns"], ["--after=10ns"], "10ns"),
    (["find", "sig", "1"], ["--before", "5c", "--async"], ["--before", "5c", "--async"], "5c"),
    (["find", "sig", "1"], ["--at-cycle", "5c"], ["--at-cycle", "5c"], "5c"),
    (["find", "sig", "1"], ["--at-time", "10ns"], ["--at-time", "10ns"], "10ns"),
    (["find", "sig", "1"], ["--at", "5c"], ["--at", "5c"], "5c"),
]


@pytest.mark.parametrize(("command", "typed", "resolved", "warned"), _MARKER_ROWS)
def test_query_resolves_markers(tmp_path, capsys, fake_native, command, typed, resolved, warned):
    markers = {"start": 10, "done": 20, "5c": 30, "10ns": 40, "10c": 50}
    trace = str(_registered(tmp_path, markers))

    forwarded = _forwarded(fake_native, "@dut", *command, *typed)

    # The wrapper puts the trace after the subcommand; `--diff` becomes `diff`.
    subcommand, positionals = (command[0], command[1:]) if command else ("diff", [])
    expected = [subcommand, trace, *positionals, *resolved]
    assert forwarded[: len(expected)] == expected
    assert "--marker" not in forwarded
    err = capsys.readouterr().err
    if warned is None:
        assert err == ""
    else:
        assert f"marker '{warned}' on @dut does not apply" in err


@pytest.mark.parametrize("wave", ["wave", "--wave"])
def test_query_injects_marker_labels_into_wave(tmp_path, fake_native, wave):
    trace = _registered(tmp_path, {"start": 10, "done": 20})

    forwarded = _forwarded(fake_native, "@dut", wave, "-s", "top.dut.state", "-t", "start:done")

    assert forwarded == [
        "wave",
        str(trace),
        "-s",
        "top.dut.state",
        "-t",
        "10c:20c",
        "--marker",
        "start",
        "10c",
        "--marker",
        "done",
        "20c",
        "--limit",
        "2000",
    ]


@pytest.mark.parametrize(
    ("alias", "argv", "flag", "typed", "name", "target"),
    [
        (
            "dut",
            ["@dut", "wave", "-s", "top.dut.state", "-t", "5c:12c"],
            "-t",
            "5c:12c",
            "5c",
            "@dut",
        ),
        (
            "my dut",
            ["@my dut", "value", "--at", "10ns", "-s", "top.dut.state"],
            "--at",
            "10ns",
            "10ns",
            "@my dut",
        ),
    ],
    ids=["wave-range", "value-at-spaced-alias"],
)
def test_query_keeps_typed_time_over_a_conflicting_marker(
    tmp_path, capsys, fake_native, alias, argv, flag, typed, name, target
):
    _registered(tmp_path, {"5c": 10, "10ns": 3}, alias)

    native = _forwarded(fake_native, *argv)

    assert len(fake_native.calls()) == 1
    assert native[native.index(flag) + 1] == typed
    warnings = [x for x in capsys.readouterr().err.splitlines() if x.startswith("WARNING: marker")]
    assert len(warnings) == 1
    assert f"marker '{name}' on @{alias} does not apply" in warnings[0]
    assert f"bwave markers {quote_arg(target)} delete {quote_arg(name)}" in warnings[0]


def _help_section_lines(text: str, headings: tuple[str, ...]) -> list[str]:
    """Indented lines under each heading line, up to the next blank line."""
    lines = text.splitlines()
    collected: list[str] = []
    for index, line in enumerate(lines):
        if not any(line.startswith(heading) for heading in headings):
            continue
        for body in lines[index + 1 :]:
            if not body.strip():
                break
            collected.append(body)
    return collected


def _help_entry_names(entries: list[str]) -> set[str]:
    """First token of each `name  description` entry; `a / b` names both."""
    names: set[str] = set()
    for entry in entries:
        head = entry.strip().split("  ")[0]
        names.update(part.split()[0] for part in head.split(" / "))
    return names


def test_wrapper_help_command_lists_match_accepted_commands(monkeypatch, capsys):
    """Every wrapper help surface names exactly the commands it accepts (#3648)."""
    top_help = _main(monkeypatch, capsys, "--help")
    query_text = _main(monkeypatch, capsys, "query", "--help")
    wrapper_only = {"register", "markers", "gui"}
    # Private: a generator-sync check. The accepted-command tables and the parser's
    # declared help have no public reader.
    subcommands = cli._V02_SUBCOMMANDS

    # (a) argparse epilog (the parser's declared help): wrapper + query commands.
    epilog = cli._build_parser().format_help()
    wrapper_names = _help_entry_names(_help_section_lines(epilog, ("Wrapper commands:",)))
    query_lines = _help_section_lines(epilog, ("Query subcommands",))
    query_names: set[str] = set()
    for line in query_lines:
        # Drop the "Trace-consuming:" / "Meta (no trace):" label, keep the names.
        body = line.split(":", 1)[1] if ":" in line else line
        query_names.update(body.split())
    assert wrapper_names | query_names == subcommands | wrapper_only | {"query"}

    # (b) `bwave --help` text.
    top_lines = _help_section_lines(
        top_help, ("Setup:", "Viewing", "Trace commands:", "Meta/direct subcommands:")
    )
    assert _help_entry_names(top_lines) == subcommands | wrapper_only

    # (c) `bwave query --help` text.
    query_help = _help_entry_names(_help_section_lines(query_text, ("Subcommands",)))
    # Private: same tables, split into the two kinds the query help lists.
    assert query_help == cli._V02_CONSUMER_SUBCOMMANDS | cli._V02_META_SUBCOMMANDS


_MARKER_CYCLE_PAIRS = [
    ("find *req* 1 --before hs --count", "find *req* 1 --before 25 --count", 0),
    ("find *req* 1 --after hs --first", "find *req* 1 --after 25 --first", 2),
    ("find *req* 1 --before hs", "find *req* 1 --before 25", 0),
    ("find *req* 1 --after hs", "find *req* 1 --after 25", 0),
    (
        "sample *req* 1 -s *data* --before hs --count",
        "sample *req* 1 -s *data* --before 25 --count",
        0,
    ),
    (
        "sample *req* 1 -s *data* --after hs --first",
        "sample *req* 1 -s *data* --after 25 --first",
        2,
    ),
    ("sample *req* 1 -s *data* --after hs", "sample *req* 1 -s *data* --after 25", 0),
    ("find *req* 1 --before=hs --count", "find *req* 1 --before=25 --count", 0),
    ("find *req* 1 --time=hs:he", "find *req* 1 --time=25:35", 0),
    ("find *req* 1 -ths:he", "find *req* 1 -t25:35", 0),
    ("value --at=hs -s *req*", "value --at=25 -s *req*", 0),
    ("diff -s *data* hs he", "diff -s *data* 25 35", 0),
    ("find *req* 1 --before hs --async --count", "find *req* 1 --before 265 --async --count", 0),
]


@pytest.mark.native_bwave
@pytest.mark.parametrize(("marker_form", "cycle_form", "returncode"), _MARKER_CYCLE_PAIRS)
def test_marker_matches_its_cycle_form(marker_form, cycle_form, returncode):
    """A Marker behaves like its cycle typed in its place: same output, refusal, exit code."""
    _ensure_bwave_fixture()
    write_sessions({"dut": _make_entry(str(BWAVE.resolve()), markers={"hs": 25, "he": 35})})

    by_marker = _query("@dut", *marker_form.split())
    by_cycle = _query("@dut", *cycle_form.split())

    assert by_marker.returncode == returncode, by_marker.stderr
    assert (by_marker.returncode, by_marker.stdout, by_marker.stderr) == (
        by_cycle.returncode,
        by_cycle.stdout,
        by_cycle.stderr,
    )


@pytest.mark.native_bwave
def test_async_marker_bound_on_a_clockless_trace_is_refused(tmp_path):
    """The async probe lets the native cycle resolver refuse a trace with no clock."""
    native = _native_bwave_binary()
    store = tmp_path / "no_clock.fst"
    built = run_bounded_child(
        [str(native), "build", str(FIXTURE_DIR / "test_no_clock.vcd"), "-o", str(store)],
        timeout=2 * hang_guard_s(),
    )
    assert built.returncode == 0, built.stderr
    write_sessions({"nc": _make_entry(str(store.resolve()), markers={"m": 2})})

    result = _query("@nc", "find", "*enable*", "1", "--before", "m", "--async")

    assert result.returncode == 2
    assert "no clock detected" in result.stderr


@pytest.mark.parametrize(
    "grid",
    [
        ["--clock", "clk", "--reset", "rst"],
        ["--clock=--clk", "--reset=--rst", "--with-reset"],
        [],
    ],
    ids=["spaced", "attached", "none"],
)
def test_async_marker_probe_keeps_cycle_grid_options(tmp_path, fake_native, grid):
    """The async probe gets the options that map a cycle to a tick, spelled as typed."""
    trace = str(_registered(tmp_path, {"m": 2}))
    fake_native.answer("value", "--async", stdout=json.dumps({"data": {"target_tick": 25}}))

    forwarded = _forwarded(
        fake_native, "@dut", "find", "s", "1", "--before", "m", "--async", *grid
    )

    probe = ["value", trace, "--async", *grid, "--at", "2c", "--format", "json", "--limit", "1"]
    assert fake_native.calls()[0].args == probe
    assert forwarded[: forwarded.index("--async")] == ["find", trace, "s", "1", "--before", "25"]
