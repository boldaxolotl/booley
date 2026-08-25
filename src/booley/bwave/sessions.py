#!/usr/bin/env python3
"""
bwave trace-session + marker persistence.

Owns the "persist/lookup registered traces and named markers on disk"
responsibility, split out of ``cli.py`` (which keeps trace *discovery* and
the CLI/flag-translation layer). Concretely this module holds:

  - The on-disk session store (``SESSION_FILE``) and its load/save primitives.
  - Trace registration under named aliases (``register`` subcommand).
  - Named markers CRUD for an alias (``markers`` subcommand).

Consumers:
  - ``booley.bwave.cli`` re-exports these symbols for backward compatibility
    and calls a few of them from its CLI dispatch / marker-resolution code.
  - ``tests/bwave/test_sessions.py`` imports SESSION_FILE, _load_sessions,
    _save_sessions, _register_trace via the ``bwave`` facade.

Cross-module coupling is one-directional at import time: ``bwave`` imports this
module eagerly; this module imports ``bwave`` only *lazily* (inside functions)
to reach names that stay in ``bwave`` — the CLI subcommand tables
(``_V02_SUBCOMMANDS`` / ``_KNOWN_COMMANDS``) and the trace-discovery helpers
(``find_trace`` / ``_trace_diagnostics``). That keeps both import orders clean.

Session-file resolution note: tests exercise this store by patching
``bwave.SESSION_FILE`` (the historical, public seam). To honor that seam after
the move, every on-disk access resolves the live path via ``_session_file()``,
which reads ``bwave.SESSION_FILE`` at call time rather than closing over this
module's own global. In production the two are the same object (re-export).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from booley.bwave.contract import exit_usage as _exit_usage
from booley.runtime.timefmt import utc_now_rfc3339


def _bwave_cache_root() -> Path:
    """Platform-safe temp directory for bwave cache files."""
    import tempfile

    return Path(tempfile.gettempdir()) / "bwave"


# ---------------------------------------------------------------------------
# Session file (named aliases)
# ---------------------------------------------------------------------------
SESSION_FILE = _bwave_cache_root() / "bwave_sessions.json"
_DEFAULT_ALIAS = "_last"


def _session_file() -> Path:
    """Resolve the live session-file path, honoring a test monkeypatch.

    Tests patch ``bwave.SESSION_FILE`` (the public seam), so read it back
    through the ``bwave`` facade at call time. In production this is the same
    object as this module's ``SESSION_FILE`` (re-exported), so behavior is
    identical; the indirection exists purely to keep the patch seam working.
    """
    from booley.bwave import cli as bwave  # lazy: avoid an import cycle at load time

    return bwave.SESSION_FILE


def _load_sessions() -> dict:
    """Load all sessions, return dict of alias -> entry."""
    session_file = _session_file()
    if not session_file.exists():
        return {}
    try:
        return json.loads(session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sessions(data: dict) -> None:
    """Write sessions file (creates tmp/ dir if needed).

    Retries on PermissionError — Windows can transiently lock files when
    another process or sandbox holds a handle.
    """
    import time as _time

    session_file = _session_file()
    session_file.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            session_file.write_text(json.dumps(data, indent=2) + "\n")
            return
        except PermissionError:
            if attempt < 2:
                _time.sleep(0.1 * (attempt + 1))
    # Non-critical — log and continue
    print(
        f"[bwave] WARNING: could not write session file {session_file} (PermissionError)",
        file=sys.stderr,
    )


def _register_trace(trace: Path, alias: str | None = None) -> str:
    """Register a trace under an alias. Returns the alias used."""
    sessions = _load_sessions()
    alias = alias or _DEFAULT_ALIAS
    entry = {
        "trace": str(trace.resolve()),
        "registered_at": utc_now_rfc3339(),
    }
    # Preserve existing markers
    if alias in sessions and "markers" in sessions[alias]:
        entry["markers"] = sessions[alias]["markers"]
    sessions[alias] = entry
    if alias != _DEFAULT_ALIAS:
        if _DEFAULT_ALIAS not in sessions:
            sessions[_DEFAULT_ALIAS] = entry
        else:
            # Update _last trace but keep its own markers
            sessions[_DEFAULT_ALIAS]["trace"] = entry["trace"]
            sessions[_DEFAULT_ALIAS]["registered_at"] = entry["registered_at"]
    _save_sessions(sessions)
    return alias


# ---------------------------------------------------------------------------
# Subcommand: register
# ---------------------------------------------------------------------------
def _trace_identity(trace: Path) -> str:
    """Top-level scope(s) recorded in *trace*, or "" when unreadable/empty.

    Registration is the only moment where a wrong trace is cheap to notice,
    so print what design the file actually contains. A common prefix is useful
    when one exists; otherwise retain every first-level root (Cocotb/Verilator
    normally exposes ``$rootio`` beside the DUT).
    """
    import subprocess

    from booley.bwave import cli as bwave  # lazy: binary resolution lives in bwave

    try:
        result = subprocess.run(
            [
                *bwave._bwave_cmd(),
                "list",
                str(trace),
                "--format",
                "json",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, SystemExit):
        return ""
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout)["data"]
        common = str(data.get("scope_prefix") or "").strip()
        if common:
            return common
        roots = [str(scope).strip() for scope in data.get("root_scopes", [])]
        identity = ", ".join(scope for scope in roots if scope)
        return identity or (
            "<top-level signals>" if int(data.get("signal_count", 0) or 0) > 0 else ""
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ""


def _trace_age_note(trace: Path) -> str:
    """`(written Nm ago)` for a trace file, or "" if the mtime is unreadable."""
    import time

    try:
        age_s = max(0.0, time.time() - trace.stat().st_mtime)
    except OSError:
        return ""
    if age_s < 90:
        return f"(written {age_s:.0f}s ago)"
    if age_s < 5400:
        return f"(written {age_s / 60:.0f}m ago)"
    return f"(written {age_s / 3600:.1f}h ago)"


def _headeronly_store_note(directory: Path) -> str:
    """Name .fst files find_trace silently skipped as unqueryable, or "".

    ``find_trace`` filters stores through ``_bwave_valid``, so a header-only
    trace.fst is invisible to the directory search: the lookup falls back to
    the raw VCD (or to "no trace found") and the resulting message blames the
    wrong thing. This note restores the real diagnosis — the direct-file
    registration path already explains header-only stores; the directory path
    must not hide the same story.
    """
    from booley.sim.trace_session import _bwave_valid  # lazy: sim dep

    try:
        skipped = sorted(f for f in directory.glob("*.fst") if not _bwave_valid(f))
    except OSError:
        return ""
    if not skipped:
        return ""
    names = ", ".join(f.name for f in skipped[:3])
    return (
        f"\nNOTE: {names} exists here but is not a queryable store (FST header "
        "missing or no signal data). A header-only trace.fst is what a "
        "Verilator sim traced via the auto-generated --main writes — re-run "
        "the sim with a custom C++ --exe main that drives tracing."
    )


def _resolve_raw_vcd(trace: Path, build: bool, diagnostics: str = "") -> Path:
    """Turn a raw ``.vcd`` into a registerable store, or exit non-zero.

    Registering a raw VCD used to succeed (rc=0) with a NOTE on stderr, leaving
    a booby-trapped alias: every later query against it dies with "requires a
    built waveform store". A scripted caller that checks exit codes — which is
    the point of exit codes — sees a green register and a red query it did not
    cause. So register now guarantees its own contract: the alias it binds is
    queryable, or the command fails.

    The conversion is opt-in (``--build``) rather than automatic because
    ``bwave build`` on a multi-GB VCD is minutes of CPU and a new multi-GB file
    written next to the user's trace — too big a side effect to perform because
    someone typed ``register``.

    *diagnostics* is appended to the refusal. A sim directory only ever resolves
    to a raw VCD when :meth:`TraceSession.find` already TRIED the conversion and
    it failed, so telling that caller to "build one first" without saying what
    went wrong sends them at a command that just failed.
    """
    out = trace.with_suffix(".fst")
    if not build:
        _exit_usage(
            f"ERROR: {trace.name} is a raw VCD; the query engine needs a built "
            f".fst store, so registering it would bind an unqueryable alias.\n"
            f"Build one first, then register it:\n"
            f"  bwave build {trace} -o {out}\n"
            f"  bwave register {out} --as ALIAS\n"
            f"Or let register do both: bwave register {trace} --as ALIAS --build" + diagnostics
        )
    if out.exists():
        # --build writes unscoped (scope=None), so silently overwriting would
        # destroy a store someone deliberately built with `bwave build --scope`
        # — and there is no way to tell the two apart from here. Refuse: the
        # user either wants that store (register it) or wants it gone (say so).
        _exit_usage(
            f"ERROR: refusing to overwrite the existing {out} with --build "
            f"(it may have been built with a different --scope).\n"
            f"Register the store that is already there:\n"
            f"  bwave register {out} --as ALIAS\n"
            f"Or rebuild it deliberately, then register:\n"
            f"  bwave build {trace} -o {out}"
        )
    from booley.sim.bwave_fifo import postprocess_vcd_to_bwave  # lazy: sim dep

    print(f"[bwave] building {out.name} from {trace.name} (--build) ...", file=sys.stderr)
    if not postprocess_vcd_to_bwave(trace, out, None):
        sys.exit(
            f"ERROR: could not build a queryable store from {trace}.\n"
            f"Run `bwave build {trace} -o {out}` directly to see the failure." + diagnostics
        )
    return out


def cmd_register(args: argparse.Namespace) -> None:
    """Find trace in a sim directory (or register a file directly) under an alias."""
    from booley.bwave import cli as bwave  # lazy: trace-discovery helpers live in bwave

    target = Path(args.sim_dir)
    if target.is_file() and target.suffix in (".fst", ".vcd"):
        # Direct-file registration used to accept anything with the right
        # suffix; a header-only .fst then answered every query with silence.
        # Gate on the same store validation the directory search applies
        # (find_trace filters through _bwave_valid).
        if target.suffix == ".fst":
            from booley.sim.trace_session import _bwave_valid  # lazy: sim dep

            if not _bwave_valid(target):
                _exit_usage(
                    f"ERROR: {target} is not a queryable waveform store: the "
                    "FST header is missing or it contains no signal data.\n"
                    "A header-only trace.fst is what a Verilator sim traced "
                    "via the auto-generated --main writes — re-run the sim "
                    "with a custom C++ --exe main that drives tracing."
                    + bwave._trace_diagnostics(target.parent)
                )
        trace = target
        diagnostics = ""
    elif target.is_dir():
        trace = bwave.find_trace(target)
        if not trace:
            msg = f"ERROR: No trace file found in {target}"
            msg += _headeronly_store_note(target)
            msg += bwave._trace_diagnostics(target)
            sys.exit(msg)
        # Only read if the search lands on a raw VCD (below) — that means the
        # directory's own VCD→FST conversion failed, and the manifest says why.
        # A header-only .fst find_trace skipped is part of that story too:
        # without it, the refusal diagnoses "raw VCD" when the actual problem
        # is an unqueryable store sitting right next to it.
        diagnostics = (
            _headeronly_store_note(target) + bwave._trace_diagnostics(target)
            if trace.suffix == ".vcd"
            else ""
        )
    else:
        sys.exit(f"ERROR: Not a trace file or directory: {target}")

    if trace.suffix == ".vcd":
        # Reached both ways: a .vcd passed directly, and a sim directory whose
        # VCD→FST conversion did not produce a store (find_trace falls back to
        # the raw VCD). Either way the alias must not be registered as-is.
        trace = _resolve_raw_vcd(trace, getattr(args, "build", False), diagnostics)

    alias = _register_trace(trace, args.alias)
    print(f"@{alias} -> {trace}")
    # Identity + age, so binding the wrong design (or a trace from a sim you
    # already superseded) is visible in the register output instead of being
    # inferred later from nonsense signal names.
    if trace.suffix == ".fst":
        identity = _trace_identity(trace)
        age = _trace_age_note(trace)
        identity_label = "top scopes" if ", " in identity else "top scope"
        detail = " ".join(
            part
            for part in (f"{identity_label}: {identity}" if identity else "", age)
            if part
        )
        if detail:
            print(f"[bwave] {detail}", file=sys.stderr)
            print(
                "[bwave] confirm this is the design under test before trusting queries.",
                file=sys.stderr,
            )
        if not identity:
            # A queryable store with signals always yields a common scope,
            # root scopes, or the explicit top-level-signals marker. Reaching
            # here therefore means the identity probe itself failed or could
            # not enumerate signals.
            print(
                "[bwave] WARNING: could not read a top scope from this store — "
                "it may have no signals (header-only trace?). Run "
                "`bwave list` on it before trusting queries.",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Subcommand: markers
# ---------------------------------------------------------------------------
def _validate_marker_name(name: str) -> bool:
    """Marker names must contain at least one non-digit character, no
    whitespace, no leading dash (else they'd parse as a CLI flag downstream),
    and must not collide with a known subcommand name."""
    from booley.bwave import cli as bwave  # lazy: CLI subcommand tables live in bwave

    if not name or name.isdigit():
        return False
    if name.startswith("-") or any(c.isspace() for c in name):
        return False
    return name not in bwave._V02_SUBCOMMANDS and name not in bwave._KNOWN_COMMANDS


# Sanity ceiling for a marker cycle — anything past this is almost certainly
# a typo (no real-world Verilog sim runs ~10**10 cycles). Used only for a
# warning; the value is still stored.
_MARKER_CYCLE_SOFT_MAX = 10**10


def _get_markers(sessions: dict, alias: str) -> dict:
    """Get markers dict for an alias (empty if none)."""
    return sessions.get(alias, {}).get("markers", {})


def _marker_set(sessions: dict, alias: str, extra: list[str]) -> None:
    if len(extra) < 3:
        sys.exit(
            "ERROR: markers set requires NAME and CYCLE\n  bwave markers [@alias] set NAME CYCLE"
        )
    name = extra[1]
    if not _validate_marker_name(name):
        hint = ""
        if name.isdigit():
            # Pure-digit name was the common mistake — suggest a labelled form.
            hint = f" (try 't{name}', 'cyc_{name}', or any non-digit prefix)"
        sys.exit(
            f"ERROR: invalid marker name '{name}'. Names must contain a "
            "non-digit char, have no whitespace, not start with '-', and "
            "not collide with a bwave subcommand (e.g. 'wave', 'find', ...)." + hint
        )
    try:
        cycle = int(extra[2])
    except ValueError:
        sys.exit(f"ERROR: marker cycle must be an integer, got '{extra[2]}'")
    if cycle < 0:
        sys.exit(f"ERROR: marker cycle must be non-negative, got {cycle}")
    if cycle > _MARKER_CYCLE_SOFT_MAX:
        print(
            f"WARNING: marker cycle {cycle} is implausibly large "
            f"(>{_MARKER_CYCLE_SOFT_MAX:.0e}); did you confuse cycles with ticks?",
            file=sys.stderr,
        )
    if "markers" not in sessions[alias]:
        sessions[alias]["markers"] = {}
    sessions[alias]["markers"][name] = cycle
    _save_sessions(sessions)
    print(f"@{alias}: {name} = {cycle}")


def _marker_list(sessions: dict, alias: str) -> None:
    markers = _get_markers(sessions, alias)
    if not markers:
        print(f"@{alias}: no markers")
        return
    sorted_markers = sorted(markers.items(), key=lambda x: x[1])
    print(f"@{alias} markers:")
    for name, cycle in sorted_markers:
        print(f"  {name:<20} {cycle}")


def _marker_delete(sessions: dict, alias: str, extra: list[str]) -> None:
    if len(extra) < 2:
        sys.exit("ERROR: markers delete requires NAME")
    name = extra[1]
    markers = sessions[alias].get("markers", {})
    if name not in markers:
        sys.exit(f"ERROR: marker '{name}' not found in @{alias}")
    del markers[name]
    _save_sessions(sessions)
    print(f"@{alias}: deleted marker '{name}'")


def cmd_markers(args: argparse.Namespace) -> None:
    """Manage named markers for a trace alias."""
    sessions = _load_sessions()

    alias = _DEFAULT_ALIAS
    extra = list(args.extra)
    if extra and extra[0].startswith("@"):
        alias = extra.pop(0)[1:]
    if alias not in sessions:
        # `--as` (not `-as`) is the flag; show it correctly so copy/paste works.
        sys.exit(
            f"ERROR: No registered alias '@{alias}'.\n"
            f"Register a trace first: bwave register SIM_DIR --as {alias}\n"
            f"Or list known aliases by reading: {_session_file()}"
        )

    if not extra:
        sys.exit(
            "ERROR: markers requires an action: set, list, or delete\n"
            "  bwave markers [@alias] set NAME CYCLE\n"
            "  bwave markers [@alias] list\n"
            "  bwave markers [@alias] delete NAME"
        )

    action = extra[0].lower()
    dispatch = {
        "set": lambda: _marker_set(sessions, alias, extra),
        "list": lambda: _marker_list(sessions, alias),
        "delete": lambda: _marker_delete(sessions, alias, extra),
    }
    if action not in dispatch:
        sys.exit(f"ERROR: unknown markers action '{action}'. Use: set, list, delete")
    dispatch[action]()
