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

from booley.bwave.cli import SESSION_FILE

BOOLEY_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = BOOLEY_ROOT / "crates" / "bwave" / "tests" / "fixtures"
BWAVE = FIXTURE_DIR / "test_distance.test.fst"


@pytest.fixture(autouse=True)
def _inside_session_runtime(monkeypatch):
    """bwave is container-only (ADR 0028); simulate the Session Runtime.

    Covers both in-process bwave.main() calls and the _query() subprocess
    (which inherits os.environ), so the venue guard passes on a host machine.
    """
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


def _native_bwave_binary() -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    release = BOOLEY_ROOT / "crates" / "bwave" / "target" / "release" / f"bwave{suffix}"
    debug = BOOLEY_ROOT / "crates" / "bwave" / "target" / "debug" / f"bwave{suffix}"
    if release.exists():
        return release
    if debug.exists():
        return debug
    pytest.skip(
        "native bwave binary not built; run cargo build --manifest-path crates/bwave/Cargo.toml"
    )


def _ensure_bwave_fixture() -> None:
    native_bwave = _native_bwave_binary()
    if BWAVE.exists():
        return
    src = FIXTURE_DIR / "test_distance.vcd"
    if not src.exists():
        pytest.skip("test_distance.vcd fixture missing")
    result = subprocess.run(
        [str(native_bwave), "build", str(src), "-o", str(BWAVE)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"failed to build {BWAVE}: {result.stderr}")


def _write_sessions(entries: dict) -> None:
    from booley.bwave import cli as bwave

    bwave.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    bwave.SESSION_FILE.write_text(json.dumps(entries, indent=2) + "\n")


def _make_entry(trace: str, **overrides) -> dict:
    return {
        "trace": trace,
        "registered_at": datetime.now(UTC).isoformat(),
        **overrides,
    }


def _query(*extra_args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    # bwave is container-only (ADR 0028); mark the venue so the subprocess
    # exercises the query surface rather than dying on the host guard.
    env = {
        **os.environ,
        "BOOLEY_CONTAINER": "1",
        "PYTHONPATH": str(BOOLEY_ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    return subprocess.run(
        [sys.executable, "-m", "booley.bwave.cli", "query", *extra_args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def test_top_level_help_lists_real_query_surface(monkeypatch, capsys):
    from booley.bwave import cli as bwave

    monkeypatch.setattr(sys, "argv", ["bwave", "--help"])

    with pytest.raises(SystemExit) as exc:
        bwave.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "RTL debug helper for simulation traces." in out
    # QA-8: help no longer over-promises blanket auto-conversion; it states a
    # directly-passed .vcd must be built with `bwave build` before querying.
    assert "auto-builds an .fst store" in out
    assert "bwave build" in out
    assert "bwave [@ALIAS | TRACE_PATH] COMMAND [ARGS...] [OPTIONS...]" in out
    assert "bwave markers [@ALIAS]" not in out
    assert "bwave query [@ALIAS | TRACE_PATH]" not in out
    assert "Trace commands:" in out
    assert "Query subcommands:" not in out
    assert "Common options:" in out
    assert "Common query options:" not in out
    assert "value --at T" in out
    assert "bwave <subcommand> --help" in out
    assert "{register,markers,query}" not in out


def test_query_help_points_to_subcommand_help(capsys):
    from booley.bwave import cli as bwave

    with pytest.raises(SystemExit) as exc:
        bwave._show_query_help()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: bwave [@ALIAS | TRACE_PATH] <subcommand>" in out
    assert "usage: bwave query" not in out
    assert "bwave --help` (Rust binary)" not in out
    assert "bwave <subcommand> --help" in out


def test_direct_subcommand_help_forwards_to_native_bwave(monkeypatch):
    from booley.bwave import cli as bwave

    calls = []
    monkeypatch.setattr(sys, "argv", ["bwave", "value", "--help"])
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-bin"])
    monkeypatch.setattr(bwave, "_run", lambda cmd: calls.append(cmd) or 0)

    with pytest.raises(SystemExit) as exc:
        bwave.main()

    assert exc.value.code == 0
    assert calls == [["bwave-bin", "value", "--help"]]


@pytest.fixture(autouse=True)
def _clean_session():
    """Remove session file before and after each test."""
    SESSION_FILE.unlink(missing_ok=True)
    yield
    SESSION_FILE.unlink(missing_ok=True)


# ── save / load round-trip ───────────────────────────────────────────


def test_save_load_sessions():
    from booley.bwave.cli import _load_sessions, _save_sessions

    data = {"baseline": {"trace": "/tmp/test.fst", "registered_at": "2026-05-14T12:00:00+00:00"}}
    _save_sessions(data)
    loaded = _load_sessions()
    assert loaded == data


def test_load_missing_sessions():
    from booley.bwave.cli import _load_sessions

    assert _load_sessions() == {}


def test_load_corrupt_sessions():
    from booley.bwave.cli import SESSION_FILE as SF
    from booley.bwave.cli import _load_sessions

    SF.parent.mkdir(parents=True, exist_ok=True)
    SF.write_text("not json {{{")
    assert _load_sessions() == {}


def test_register_trace_sets_last():
    from booley.bwave.cli import _load_sessions, _register_trace

    trace = Path("/tmp/fake.fst")
    _register_trace(trace, alias="foo")
    sessions = _load_sessions()
    assert "foo" in sessions
    assert "_last" in sessions
    assert sessions["foo"]["trace"] == sessions["_last"]["trace"]


def test_register_multiple_aliases():
    from booley.bwave.cli import _load_sessions, _register_trace

    _register_trace(Path("/tmp/a.fst"), alias="a")
    _register_trace(Path("/tmp/b.fst"), alias="b")
    sessions = _load_sessions()
    assert sessions["a"]["trace"].endswith("a.fst")
    assert sessions["b"]["trace"].endswith("b.fst")
    assert sessions["_last"]["trace"] == sessions["b"]["trace"]


# ── query via session ────────────────────────────────────────────────


@pytest.mark.native_bwave
def test_query_uses_default_session():
    _ensure_bwave_fixture()
    _write_sessions({"_last": _make_entry(str(BWAVE.resolve()))})
    r = _query("--wave", "-t", "1:10", "-s", "*req*")
    assert r.returncode == 0
    assert "req" in r.stdout


@pytest.mark.native_bwave
def test_query_uses_named_alias():
    _ensure_bwave_fixture()
    _write_sessions(
        {
            "baseline": _make_entry(str(BWAVE.resolve())),
            "_last": _make_entry("/nonexistent/path.fst"),
        }
    )
    r = _query("@baseline", "--wave", "-t", "1:10", "-s", "*req*")
    assert r.returncode == 0
    assert "req" in r.stdout


@pytest.mark.native_bwave
def test_query_explicit_overrides_session():
    _ensure_bwave_fixture()
    _write_sessions({"_last": _make_entry("/nonexistent/path.fst")})
    r = _query(str(BWAVE), "--wave", "-t", "1:5", "-s", "*ack*")
    assert r.returncode == 0
    assert "ack" in r.stdout


@pytest.mark.native_bwave
def test_stale_session_warning(tmp_path):
    """Staleness keys on the trace file's mtime, not the registration time."""
    import shutil

    _ensure_bwave_fixture()
    old_trace = tmp_path / "old.fst"
    shutil.copy(BWAVE, old_trace)
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(old_trace, (two_days_ago, two_days_ago))
    # registered_at is NOW — registering an ancient file five minutes ago
    # does not make its data younger, so the warning must still fire.
    _write_sessions({"_last": _make_entry(str(old_trace.resolve()))})
    r = _query("--wave", "-t", "1:5", "-s", "*req*")
    assert r.returncode == 0
    assert "older than 24h" in r.stderr


@pytest.mark.native_bwave
def test_fresh_trace_with_old_registration_does_not_warn():
    """Re-simulating in place refreshes the data — no false staleness warning.

    The old check keyed on registered_at, so a re-sim into the same path kept
    warning (or worse: an alias registered long ago over fresh data warned
    while truly stale data registered recently did not).
    """
    _ensure_bwave_fixture()
    os.utime(BWAVE)  # "re-sim just happened": data is fresh regardless of build date
    _write_sessions(
        {
            "_last": _make_entry(
                str(BWAVE.resolve()),
                registered_at="2026-05-12T00:00:00+00:00",
            )
        }
    )
    r = _query("--wave", "-t", "1:5", "-s", "*req*")
    assert r.returncode == 0
    assert "older than 24h" not in r.stderr


def test_missing_trace_error():
    _write_sessions(
        {
            "_last": _make_entry(
                str(FIXTURE_DIR / "nonexistent_xyz.fst"),
            )
        }
    )
    r = _query("--wave", "-t", "1:5", "-s", "*req*")
    assert r.returncode != 0
    assert "no longer exists" in r.stderr.lower()


def test_register_missing_trace_includes_incident(tmp_path):
    from booley.bwave import cli as bwave

    (tmp_path / "trace_status.json").write_text(
        '{"current_status": "failed", "failure_reason": "no artifact"}\n',
        encoding="utf-8",
    )
    (tmp_path / "trace_incident.txt").write_text(
        "reason: trace requested but no queryable .fst store or .vcd was produced\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        bwave.cmd_register(argparse.Namespace(sim_dir=str(tmp_path), alias="dut"))
    assert "No trace file found" in str(exc.value)
    assert "trace_status.json" in str(exc.value)
    assert "failure_reason" in str(exc.value)


def test_register_raw_vcd_refuses_and_registers_nothing(tmp_path, monkeypatch, capsys):
    """A raw .vcd is refused non-zero — never bound as an unqueryable alias.

    The query engine needs a built store, so a VCD alias can only ever answer
    "requires a built waveform store". Registering it rc=0 handed scripted
    callers a green light for a command that could not work (F-32c).
    """
    import argparse

    from booley.bwave import cli as bwave

    sessions = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", sessions)

    vcd = tmp_path / "waveform.vcd"
    vcd.write_text("$enddefinitions $end\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_register(argparse.Namespace(sim_dir=str(vcd), alias="dut", build=False))
    assert exc.value.code == 2, "caller-input refusal must use the usage exit code"
    msg = capsys.readouterr().err
    assert "raw VCD" in msg
    assert "bwave build" in msg
    assert "waveform.fst" in msg
    assert "--build" in msg
    assert not sessions.exists(), "a refused registration must not write a session"


def test_register_raw_vcd_with_build_converts_and_registers(tmp_path, monkeypatch, capsys):
    """`--build` opts into the conversion and registers the built store."""
    import argparse

    from booley.bwave import cli as bwave
    from tests.conftest import MINIMAL_FST_BYTES

    monkeypatch.setattr(bwave, "SESSION_FILE", tmp_path / "sessions.json")

    vcd = tmp_path / "waveform.vcd"
    vcd.write_text("$enddefinitions $end\n", encoding="utf-8")
    built = tmp_path / "waveform.fst"

    def _fake_build(vcd_path, out_path, scope):
        out_path.write_bytes(MINIMAL_FST_BYTES)
        return True

    monkeypatch.setattr("booley.sim.bwave_fifo.postprocess_vcd_to_bwave", _fake_build)
    monkeypatch.setattr("booley.bwave.sessions._trace_identity", lambda _t: "top")

    bwave.cmd_register(argparse.Namespace(sim_dir=str(vcd), alias="dut", build=True))
    assert bwave._load_sessions()["dut"]["trace"] == str(built.resolve())
    assert "waveform.fst" in capsys.readouterr().out


def test_register_raw_vcd_build_failure_exits_nonzero(tmp_path, monkeypatch):
    """A failed conversion must not fall back to registering the VCD."""
    import argparse

    from booley.bwave import cli as bwave

    sessions = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", sessions)

    vcd = tmp_path / "waveform.vcd"
    vcd.write_text("$enddefinitions $end\n", encoding="utf-8")
    monkeypatch.setattr(
        "booley.sim.bwave_fifo.postprocess_vcd_to_bwave",
        lambda vcd_path, out_path, scope: False,
    )

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_register(argparse.Namespace(sim_dir=str(vcd), alias="dut", build=True))
    assert "could not build a queryable store" in str(exc.value)
    assert not sessions.exists()


def test_register_build_refuses_to_clobber_an_existing_store(tmp_path, monkeypatch, capsys):
    """`--build` writes <name>.fst unscoped; an existing one may be scoped.

    Nothing on disk distinguishes a store someone built with `bwave build
    --scope tb.dut` from one register would write, so overwriting it silently
    is a data loss the user never asked for by typing `register`.
    """
    import argparse

    from booley.bwave import cli as bwave

    sessions = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", sessions)

    vcd = tmp_path / "waveform.vcd"
    vcd.write_text("$enddefinitions $end\n", encoding="utf-8")
    existing = tmp_path / "waveform.fst"
    existing.write_bytes(b"precious-scoped-store")

    called = []
    monkeypatch.setattr(
        "booley.sim.bwave_fifo.postprocess_vcd_to_bwave",
        lambda *a: called.append(a) or True,
    )

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_register(argparse.Namespace(sim_dir=str(vcd), alias="dut", build=True))

    assert exc.value.code == 2, "caller-input refusal must use the usage exit code"
    msg = capsys.readouterr().err
    assert "refusing to overwrite" in msg
    assert "--scope" in msg
    assert f"bwave register {existing}" in msg  # the register-it-as-is way out
    assert not called, "build ran despite the refusal"
    assert existing.read_bytes() == b"precious-scoped-store"
    assert not sessions.exists()


def test_register_sim_dir_falling_back_to_vcd_shows_why_conversion_failed(
    tmp_path, monkeypatch, capsys
):
    """A sim dir only resolves to a raw VCD when its own VCD→FST already failed.

    Refusing is right (the alias would be unqueryable either way), but pointing
    at `bwave build` without a word about the failure sends the user at the
    command that just lost. The trace manifest carries the reason — show it.
    """
    import argparse

    from booley.bwave import cli as bwave

    sessions = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", sessions)

    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$enddefinitions $end\n", encoding="utf-8")
    (tmp_path / "trace_status.json").write_text(
        '{"failure_reason": "bwave binary not found"}', encoding="utf-8"
    )
    # The one way find() hands back a raw .vcd: its own conversion lost.
    monkeypatch.setattr(
        "booley.sim.trace_session.TraceSession._convert_vcd", lambda self, path: None
    )

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_register(argparse.Namespace(sim_dir=str(tmp_path), alias="dut", build=False))

    assert exc.value.code == 2, "caller-input refusal must use the usage exit code"
    msg = capsys.readouterr().err
    assert "raw VCD" in msg
    assert "bwave binary not found" in msg
    assert not sessions.exists()


def test_register_bwave_file_no_build_hint(tmp_path, monkeypatch, capsys):
    """A directly-registered .fst needs no build step — no hint printed.

    Uses a minimal-but-valid store: register now validates direct .fst files
    (see test_register_rejects_header_only_fst), so the old 9-garbage-bytes
    fixture would trip the validation gate before the hint logic runs.
    """
    import argparse

    from booley.bwave import cli as bwave
    from tests.conftest import MINIMAL_FST_BYTES

    monkeypatch.setattr(bwave, "SESSION_FILE", tmp_path / "sessions.json")

    trace = tmp_path / "trace.fst"
    trace.write_bytes(MINIMAL_FST_BYTES)

    bwave.cmd_register(argparse.Namespace(sim_dir=str(trace), alias="dut"))
    err = capsys.readouterr().err
    assert "bwave build" not in err


def test_register_rejects_header_only_fst(tmp_path, monkeypatch, capsys):
    """Direct-file register must refuse a header-only (zero-signal) store.

    The suffix check alone let a Verilator auto---main artifact — valid FST
    header, no signal data — get registered, after which every query
    "succeeded" with empty output. Registration is the cheap moment to
    catch it, with an error that names the file and the cause.
    """
    import argparse

    from booley.bwave import cli as bwave
    from tests.conftest import FST_HEADER_BYTES

    monkeypatch.setattr(bwave, "SESSION_FILE", tmp_path / "sessions.json")

    trace = tmp_path / "trace.fst"
    trace.write_bytes(FST_HEADER_BYTES)

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_register(argparse.Namespace(sim_dir=str(trace), alias="dut"))
    assert exc.value.code == 2, "caller-input refusal must use the usage exit code"
    msg = capsys.readouterr().err
    assert "trace.fst" in msg, msg
    assert "no signal data" in msg, msg
    assert "header-only" in msg, msg
    # Nothing may be registered on failure.
    assert not (tmp_path / "sessions.json").exists()


def test_register_dir_with_header_only_store_names_the_real_problem(tmp_path, capsys):
    """A sim dir whose only .fst is header-only must not just say "no trace".

    find_trace filters stores through _bwave_valid, so the header-only file
    was invisible: the search fell to "No trace file found" (or blamed a raw
    VCD) while the actual problem — an unqueryable store sitting right there
    — went unmentioned.
    """
    import argparse

    from booley.bwave import cli as bwave
    from tests.conftest import FST_HEADER_BYTES

    (tmp_path / "trace.fst").write_bytes(FST_HEADER_BYTES)

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_register(argparse.Namespace(sim_dir=str(tmp_path), alias="dut"))
    msg = str(exc.value)
    assert "No trace file found" in msg, msg
    assert "trace.fst" in msg, msg
    assert "header-only" in msg, msg
    capsys.readouterr()  # drain


def test_grep_propagates_inner_exit_code(monkeypatch, tmp_path):
    """--grep must not swallow the inner `list` exit code (bad glob = rc 2)."""
    from booley.bwave import cli as bwave

    trace = tmp_path / "trace.fst"
    trace.write_bytes(b"x")
    monkeypatch.setattr(bwave, "_run", lambda cmd: 2)
    with pytest.raises(SystemExit) as exc:
        bwave._handle_grep(["bwave-bin"], str(trace), ["--grep", "[oops"])
    assert exc.value.code == 2


def test_register_missing_identity_warns_loudly(tmp_path, monkeypatch, capsys):
    """A store whose top scope cannot be read must say so, not skip silently.

    `_trace_identity` returning "" used to drop the identity block entirely —
    which hid exactly the stores (no scopes, unreadable) one should not
    blindly trust.
    """
    import argparse

    from booley.bwave import cli as bwave
    from booley.bwave import sessions as bwave_sessions
    from tests.conftest import MINIMAL_FST_BYTES

    monkeypatch.setattr(bwave, "SESSION_FILE", tmp_path / "sessions.json")
    # The minimal fixture passes _bwave_valid but is not a real FST the native
    # binary can list scopes from; force the identity probe's empty answer.
    monkeypatch.setattr(bwave_sessions, "_trace_identity", lambda _trace: "")

    trace = tmp_path / "trace.fst"
    trace.write_bytes(MINIMAL_FST_BYTES)

    bwave.cmd_register(argparse.Namespace(sim_dir=str(trace), alias="dut"))
    err = capsys.readouterr().err
    assert "could not read a top scope" in err, err
    assert "header-only" in err, err


def test_unknown_alias_error():
    _write_sessions({"baseline": _make_entry(str(BWAVE.resolve()))})
    r = _query("@nonexistent", "--wave", "-t", "1:5")
    assert r.returncode != 0
    assert "@nonexistent" in r.stderr.lower() or "no registered alias" in r.stderr.lower()


def test_no_session_error():
    r = _query("--wave", "-t", "1:5", "-s", "*req*")
    assert r.returncode != 0
    assert "no registered session" in r.stderr.lower()


def test_build_subcommand_does_not_require_registered_session(monkeypatch):
    from booley.bwave import cli as bwave

    calls = []
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-bin"])
    monkeypatch.setattr(bwave, "_run", lambda cmd: calls.append(cmd) or 0)

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=["build", "input.vcd", "-o", "out.fst"]))

    assert exc.value.code == 0
    assert calls == [["bwave-bin", "build", "input.vcd", "-o", "out.fst"]]


def test_v02_subcommand_accepts_trace_after_command(monkeypatch, tmp_path):
    from booley.bwave import cli as bwave

    trace = tmp_path / "trace.fst"
    trace.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x01I" + b"\0" * 16)
    calls = []
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-bin"])
    monkeypatch.setattr(bwave, "_run", lambda cmd: calls.append(cmd) or 0)

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=["list", str(trace)]))

    assert exc.value.code == 0
    # `list` carries its own (smaller) default so the tree fits the caller's
    # output window — see test_list_gets_a_window_sized_limit_default.
    assert calls == [["bwave-bin", "list", str(trace), "--limit", bwave._LIST_LIMIT_DEFAULT]]


def test_register_trace_preserves_alias_and_last_markers(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    session_file = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", session_file)
    a = tmp_path / "a.fst"
    b = tmp_path / "b.fst"
    a.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x01I" + b"\0" * 16)
    b.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x01I" + b"\0" * 16)

    bwave._register_trace(a, alias="dut")
    sessions = bwave._load_sessions()
    sessions["dut"]["markers"] = {"start": 10}
    sessions["_last"]["markers"] = {"last_only": 99}
    bwave._save_sessions(sessions)

    bwave._register_trace(b, alias="dut")
    sessions = bwave._load_sessions()
    assert sessions["dut"]["trace"] == str(b.resolve())
    assert sessions["dut"]["markers"] == {"start": 10}
    assert sessions["_last"]["trace"] == str(b.resolve())
    assert sessions["_last"]["markers"] == {"last_only": 99}


def test_marker_name_validation():
    from booley.bwave import cli as bwave

    for bad in ["", "123", "-bad", "has space", "wave", "register"]:
        assert not bwave._validate_marker_name(bad), bad
    for good in ["error_start", "t123"]:
        assert bwave._validate_marker_name(good), good


def test_markers_set_list_delete(tmp_path, monkeypatch, capsys):
    from booley.bwave import cli as bwave

    session_file = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", session_file)
    trace = tmp_path / "trace.fst"
    trace.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x01I" + b"\0" * 16)
    bwave._register_trace(trace, alias="dut")

    bwave.cmd_markers(argparse.Namespace(extra=["@dut", "set", "done", "20"]))
    bwave.cmd_markers(argparse.Namespace(extra=["@dut", "set", "start", "10"]))
    bwave.cmd_markers(argparse.Namespace(extra=["@dut", "list"]))
    out = capsys.readouterr().out
    assert "start" in out and "done" in out
    assert out.rfind("start") < out.rfind("done")

    bwave.cmd_markers(argparse.Namespace(extra=["@dut", "delete", "start"]))
    assert "start" not in bwave._load_sessions()["dut"]["markers"]


def test_resolve_markers_in_legacy_and_v02_args(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    session_file = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", session_file)
    _write_sessions({"dut": _make_entry("/tmp/t.fst", markers={"start": 10, "done": 20})})

    cases = [
        (["-t", "start:done"], ["-t", "10:20"]),
        (["--before", "done"], ["--before", "20"]),
        (["--after", "start"], ["--after", "10"]),
        (["--diff", "start", "done"], ["--diff", "10", "20"]),
        (["diff", "start", "done"], ["diff", "10", "20"]),
        (["value", "--at", "start"], ["value", "--at", "10"]),
        (["-t", "unknown:done"], ["-t", "unknown:20"]),
    ]
    for args, expected in cases:
        bwave._resolve_markers_in_args(args, "dut")
        assert args == expected


def test_inject_marker_flags_only_for_wave(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    session_file = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", session_file)
    _write_sessions({"dut": _make_entry("/tmp/t.fst", markers={"start": 10, "done": 20})})

    legacy = ["--wave"]
    bwave._inject_marker_flags(legacy, "dut")
    assert legacy[-6:] == ["--marker", "start", "10", "--marker", "done", "20"]

    v02 = ["wave"]
    bwave._inject_marker_flags(v02, "dut")
    assert v02[-6:] == ["--marker", "start", "10", "--marker", "done", "20"]

    non_wave = ["find"]
    bwave._inject_marker_flags(non_wave, "dut")
    assert non_wave == ["find"]


def test_query_resolves_markers_and_injects_wave_labels(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    session_file = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", session_file)
    trace = tmp_path / "trace.fst"
    trace.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x01I" + b"\0" * 16)
    _write_sessions(
        {
            "dut": _make_entry(str(trace), markers={"start": 10, "done": 20}),
        }
    )
    calls = []
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-bin"])
    monkeypatch.setattr(bwave, "_run", lambda cmd: calls.append(cmd) or 0)

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(
            argparse.Namespace(
                extra=["@dut", "wave", "-s", "top.dut.state", "-t", "start:done"],
            )
        )

    assert exc.value.code == 0
    assert calls == [
        [
            "bwave-bin",
            "wave",
            str(trace),
            "-s",
            "top.dut.state",
            "-t",
            "10:20",
            "--marker",
            "start",
            "10",
            "--marker",
            "done",
            "20",
            "--limit",
            "5000",
        ]
    ]


def test_query_resolves_v02_diff_markers(tmp_path, monkeypatch):
    from booley.bwave import cli as bwave

    session_file = tmp_path / "sessions.json"
    monkeypatch.setattr(bwave, "SESSION_FILE", session_file)
    trace = tmp_path / "trace.fst"
    trace.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x01I" + b"\0" * 16)
    _write_sessions(
        {
            "dut": _make_entry(str(trace), markers={"start": 10, "done": 20}),
        }
    )
    calls = []
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-bin"])
    monkeypatch.setattr(bwave, "_run", lambda cmd: calls.append(cmd) or 0)

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=["@dut", "diff", "start", "done"]))

    assert exc.value.code == 0
    assert calls == [["bwave-bin", "diff", str(trace), "10", "20", "--limit", "5000"]]


def test_meta_query_bypasses_alias_resolution(monkeypatch):
    from booley.bwave import cli as bwave

    calls = []
    monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave-bin"])
    monkeypatch.setattr(bwave, "_run", lambda cmd: calls.append(cmd) or 0)

    with pytest.raises(SystemExit) as exc:
        bwave.cmd_query(argparse.Namespace(extra=["@missing", "schema"]))

    assert exc.value.code == 0
    assert calls == [["bwave-bin", "schema"]]


# ── malformed session-file boundary (untrusted JSON) ─────────────────────
def test_resolve_trace_missing_trace_key_exits(monkeypatch, tmp_path):
    """A session object lacking the "trace" key must exit cleanly, not KeyError."""
    from booley.bwave import cli as bwave

    monkeypatch.setattr(bwave, "SESSION_FILE", tmp_path / "sessions.json")
    _write_sessions({"dut": {"registered_at": datetime.now(UTC).isoformat()}})

    with pytest.raises(SystemExit) as exc:
        bwave._resolve_trace_from_session("dut")
    assert "malformed" in str(exc.value)


def test_resolve_trace_non_object_session_exits(monkeypatch, tmp_path):
    """A non-object session entry (corrupted JSON) must not crash on .get()."""
    from booley.bwave import cli as bwave

    monkeypatch.setattr(bwave, "SESSION_FILE", tmp_path / "sessions.json")
    _write_sessions({"dut": "not-an-object"})

    with pytest.raises(SystemExit) as exc:
        bwave._resolve_trace_from_session("dut")
    assert "malformed" in str(exc.value)


def test_resolve_trace_bad_timestamp_still_resolves(monkeypatch, tmp_path):
    """A garbage registered_at must skip the staleness warning, not crash."""
    from booley.bwave import cli as bwave

    monkeypatch.setattr(bwave, "SESSION_FILE", tmp_path / "sessions.json")
    trace = tmp_path / "trace.fst"
    trace.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x01I" + b"\0" * 16)
    _write_sessions({"dut": {"trace": str(trace), "registered_at": "yesterday-ish"}})

    # Must return the trace path without raising on fromisoformat.
    assert bwave._resolve_trace_from_session("dut") == str(trace)


def test_warn_if_trace_stale_bad_inputs_are_silent(capsys):
    """Missing/non-string/unparsable inputs produce no warning and no crash."""
    from booley.bwave import cli as bwave

    bwave._warn_if_trace_stale(None)
    bwave._warn_if_trace_stale(12345)
    bwave._warn_if_trace_stale("no/such/trace.fst", None)
    bwave._warn_if_trace_stale("no/such/trace.fst", "not-a-timestamp")
    assert capsys.readouterr().err == ""


def test_warn_if_trace_stale_old_mtime_warns(tmp_path, capsys):
    from booley.bwave import cli as bwave

    trace = tmp_path / "trace.fst"
    trace.write_bytes(b"x")
    three_days_ago = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    os.utime(trace, (three_days_ago, three_days_ago))
    bwave._warn_if_trace_stale(trace)
    assert "older than 24h" in capsys.readouterr().err


def test_warn_if_trace_stale_fresh_mtime_beats_old_registration(tmp_path, capsys):
    """A re-sim in place (fresh mtime) silences the warning even for an old alias."""
    from booley.bwave import cli as bwave

    trace = tmp_path / "trace.fst"
    trace.write_bytes(b"x")  # mtime = now
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    bwave._warn_if_trace_stale(trace, old)
    assert capsys.readouterr().err == ""


def test_warn_if_trace_stale_falls_back_to_registered_at(capsys):
    """When the file cannot be stat'ed, the registration time is the best signal."""
    from booley.bwave import cli as bwave

    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    bwave._warn_if_trace_stale("no/such/trace.fst", old)
    assert "older than 24h" in capsys.readouterr().err


@pytest.mark.native_bwave
def test_register_reports_trace_identity_and_age(tmp_path, monkeypatch, capsys):
    """`register` must say which design it just bound, and how old it is.

    A shared-container cache collision once bound an AXI-bridge query to an
    unrelated serial-comm testbench's waveform; nothing in the register
    output hinted at the mismatch, so the agent debugged a design it was not
    working on. The top scope is the design's identity — print it.
    """
    from booley.bwave import cli as bwave

    _ensure_bwave_fixture()
    monkeypatch.setattr(bwave, "SESSION_FILE", tmp_path / "sessions.json")

    bwave.cmd_register(argparse.Namespace(sim_dir=str(BWAVE), alias="dut"))
    err = capsys.readouterr().err
    assert "top scope:" in err, err
    assert "written" in err, err
    assert "confirm this is the design under test" in err, err


def test_list_gets_a_window_sized_limit_default():
    """`list` defaults to a limit that fits the MCP stdout window.

    An unbounded list of a wide scope overran the window, which keeps the
    *tail* — so the scope header and the top of the tree were dropped and
    the agent saw a fragment with no explanation.
    """
    from booley.bwave import cli as bwave

    list_args = ["list", "/tmp/x.fst", "-s", "*"]
    bwave._apply_limit_default(list_args)
    assert list_args[-2:] == ["--limit", bwave._LIST_LIMIT_DEFAULT]

    query_args = ["signal", "/tmp/x.fst", "-s", "*"]
    bwave._apply_limit_default(query_args)
    assert query_args[-2:] == ["--limit", bwave._QUERY_LIMIT_DEFAULT]


def test_version_flag_answers_instead_of_forwarding_a_query(monkeypatch, capsys):
    """`bwave --version` used to fall through to the query translator (F-29c).

    The first arg was not a wrapper command, so it was forwarded to the Rust
    binary as a *query* and came back as the session usage block — the one
    question you ask when checking whether the Python wrapper is shadowed.
    """
    from booley import __version__
    from booley.bwave import cli as bwave

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr(sys, "argv", ["bwave", "--version"])

    with pytest.raises(SystemExit) as exc:
        bwave.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "bwave" in out
    assert "usage" not in out.lower()


def test_short_version_flag_works_too(monkeypatch, capsys):
    from booley.bwave import cli as bwave

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr(sys, "argv", ["bwave", "-V"])
    with pytest.raises(SystemExit) as exc:
        bwave.main()
    assert exc.value.code == 0
    assert "bwave (booley " in capsys.readouterr().out


def test_parser_declares_version_so_help_lists_it():
    from booley.bwave import cli as bwave

    assert "--version" in bwave._build_parser().format_help()
    assert "bwave --version" in bwave._TOP_HELP_TEXT
