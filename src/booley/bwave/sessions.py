#!/usr/bin/env python3
"""
bwave trace-session + marker persistence.

Owns the "persist/lookup registered traces and named markers on disk"
responsibility, split out of ``cli.py`` (which keeps trace *discovery* and
the CLI/flag-translation layer). Concretely this module holds:

  - The on-disk session store (``SessionStore``) and its load/save primitives.
  - Trace registration under named aliases (``register`` subcommand).
  - Named markers CRUD for an alias (``markers`` subcommand).

Consumers:
  - ``booley.bwave.cli`` re-exports these symbols for backward compatibility
    and calls a few of them from its CLI dispatch / marker-resolution code.
  - ``booley.flows.sim.bwave_registration`` calls ``register_trace`` to register
    a traced simulation run's waveforms into the run's own checkout store.
  - ``tests/bwave/test_sessions.py`` imports _load_sessions, _save_sessions,
    register_trace via the ``bwave`` facade.

Cross-module coupling is one-directional at import time: ``bwave`` imports this
module eagerly; this module imports ``bwave`` only *lazily* (inside functions)
to reach names that stay in ``bwave`` — the CLI subcommand tables
(``_V02_SUBCOMMANDS`` / ``_KNOWN_COMMANDS``) and the trace-discovery helpers
(``find_trace`` / ``_trace_diagnostics``). That keeps both import orders clean.

Store location (issue #3483): every checkout keeps its own sessions, so
``@<target>`` and the default session are that checkout's latest traced run.
The store is ``<Booley Directory>/.runtime/bwave/bwave_sessions.json`` of the
checkout that registers or reads. Outside any Project (no Git checkout or
initialized directory above the working directory, or a checkout with no
Booley Directory of its own) it is ``<temporary directory>/bwave/``, resolved
at call time and never creating a Booley Directory. A trace inside the checkout
is recorded relative to it, so the checkout can move; any other trace is
recorded as an absolute path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from booley.bwave.contract import decode_list_metadata
from booley.bwave.contract import exit_usage as _exit_usage
from booley.bwave.waveform_store import ExistingStorePolicy, convert_vcd, inspect_store
from booley.core.booley_dir import resolve_authoritative_booley_dir
from booley.core.booley_dir_pin import BooleyDirPinError
from booley.core.booley_layout import BooleyLayoutError
from booley.core.boundary import BoundaryError
from booley.core.checkout_role import SourceCheckoutProjectError
from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.project_discovery import session_store_checkout
from booley.runtime.timefmt import format_age, utc_now_rfc3339

# ---------------------------------------------------------------------------
# Session file (named aliases)
# ---------------------------------------------------------------------------
_DEFAULT_ALIAS = "_last"
# Bounded wait for the registration lock: a held lock is another registration.
_LOCK_TIMEOUT_S = 5
# Failures that mean "this directory has no Booley Directory to keep sessions in".
_NO_STORE_ERRORS = (SourceCheckoutProjectError, BooleyLayoutError, BooleyDirPinError, OSError)


@dataclass(frozen=True)
class SessionStore:
    """One bwave session file and the checkout its in-checkout traces are relative to."""

    path: Path  # The session JSON; its lock is the sibling ``<name>.lock``.
    checkout: Path | None  # Resolved checkout root; ``None`` for the temporary-directory store.

    def record(self, trace: Path) -> str:
        """The text to store for *trace*: relative to the checkout when inside it."""
        resolved = trace.resolve()
        if self.checkout is not None and resolved.is_relative_to(self.checkout):
            return resolved.relative_to(self.checkout).as_posix()
        return str(resolved)

    def trace_path(self, recorded: str) -> Path:
        """The absolute trace path for a stored entry (inverse of ``record``)."""
        path = Path(recorded)
        if path.is_absolute() or self.checkout is None:
            return path
        return self.checkout / path


def _temporary_store() -> SessionStore:
    """The store outside any Project, in the temporary directory current at call time."""
    return SessionStore(Path(tempfile.gettempdir()) / "bwave" / "bwave_sessions.json", None)


def session_store(checkout: Path | None) -> SessionStore:
    """The store in *checkout*'s own Booley Directory, else the temporary-directory store.

    Never creates a Booley Directory: a checkout without one of its own (a
    hand-made worktree, or a Project reached only through ``BOOLEY_DIR``) shares
    nothing with the Main Booley Directory and uses the temporary store.
    """
    if checkout is None:
        return _temporary_store()
    try:
        data = resolve_authoritative_booley_dir(checkout)
        if not data.is_dir():
            return _temporary_store()
        root = checkout.resolve()
    except _NO_STORE_ERRORS:
        return _temporary_store()
    return SessionStore(data / ".runtime" / "bwave" / "bwave_sessions.json", root)


def current_session_store() -> SessionStore:
    """The store of the Project checkout containing the working directory."""
    try:
        checkout = session_store_checkout(Path.cwd())
    except _NO_STORE_ERRORS:
        checkout = None
    return session_store(checkout)


def _load_sessions(store: SessionStore | None = None) -> dict:
    """Load all sessions of *store* (default: the current checkout's), alias -> entry."""
    session_file = (store or current_session_store()).path
    if not session_file.exists():
        return {}
    try:
        return json.loads(session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sessions(data: dict, store: SessionStore | None = None) -> bool:
    """Publish the sessions file atomically (creates the directory if needed).

    The document is written to a sibling temp file and renamed over the store,
    so a concurrent reader sees the old or the new sessions, never a torn file
    that ``_load_sessions`` would read as empty.

    Retries on PermissionError — Windows can transiently lock files when
    another process or sandbox holds a handle. Returns False (after a warning
    on stderr) when every attempt was refused; other OSErrors propagate.
    """
    import time as _time

    session_file = (store or current_session_store()).path
    session_file.parent.mkdir(parents=True, exist_ok=True)
    pending = session_file.with_name(f"{session_file.name}.{os.getpid()}.tmp")
    try:
        for attempt in range(3):
            try:
                pending.write_text(json.dumps(data, indent=2) + "\n")
                pending.replace(session_file)
                return True
            except PermissionError:
                if attempt < 2:
                    _time.sleep(0.1 * (attempt + 1))
    finally:
        pending.unlink(missing_ok=True)
    # Non-critical for markers — warn and continue
    print(
        f"[bwave] WARNING: could not write session file {session_file} (PermissionError)",
        file=sys.stderr,
    )
    return False


def register_trace(
    trace: Path, alias: str | None = None, *, store: SessionStore | None = None
) -> str:
    """Register a trace under an alias and make it the default session.

    Used by ``bwave register`` and by a traced ``booley flow sim`` run. The
    load-modify-save runs under an exclusive lock on a sidecar file, so two
    registrations finishing together cannot drop each other's aliases.

    Returns the alias used. Raises ``OSError`` when the store cannot be locked
    or written; the caller decides whether that matters. *store* defaults to the
    current checkout's.
    """
    store = store or current_session_store()
    session_file = store.path
    session_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = session_file.with_name(f"{session_file.name}.lock")
    with lock_path.open("a+") as lock:
        wait_for_file_lock(lock, timeout_s=_LOCK_TIMEOUT_S)
        try:
            return _register_trace_locked(trace, alias, store)
        finally:
            release_file_lock(lock)


def _register_trace_locked(trace: Path, alias: str | None, store: SessionStore) -> str:
    """Load-modify-save one registration; the caller holds the store lock."""
    sessions = _load_sessions(store)
    alias = alias or _DEFAULT_ALIAS
    entry = {
        "trace": store.record(trace),
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
    if not _save_sessions(sessions, store):
        raise OSError(f"could not write session file {store.path} (PermissionError)")
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
        # Surface why the probe failed (e.g. a truncated store's cause plus
        # rebuild hint) instead of leaving only the generic warning below.
        cause = result.stderr.strip().splitlines()[-1:] if result.stderr else []
        if cause:
            print(f"[bwave] WARNING: {cause[0]}", file=sys.stderr)
        return ""
    try:
        metadata = decode_list_metadata(result.stdout)
        return metadata.display_scope if metadata.signal_count > 0 else ""
    except (BoundaryError, json.JSONDecodeError):
        return ""


def _headeronly_store_note(directory: Path) -> str:
    """Name .fst files find_trace silently skipped as unqueryable, or "".

    ``find_trace`` filters stores through structural inspection, so a header-only
    trace.fst is invisible to the directory search: the lookup falls back to
    the raw VCD (or to "no trace found") and the resulting message blames the
    wrong thing. This note restores the real diagnosis — the direct-file
    registration path already explains header-only stores; the directory path
    must not hide the same story.
    """
    try:
        skipped = sorted(
            f
            for f in directory.glob("*.fst")
            if not inspect_store(f, probe_reader=False).structurally_usable
        )
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
    print(f"[bwave] building {out.name} from {trace.name} (--build) ...", file=sys.stderr)
    result = convert_vcd(trace, out, existing_store=ExistingStorePolicy.REFUSE)
    for event in result.events:
        print(f"[bwave] {event}")
    if not result.success:
        if result.detail:
            (trace.parent / "trace.fst.stderr").write_text(result.detail + "\n", encoding="utf-8")
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
        # (find_trace filters through structural inspection).
        if (
            target.suffix == ".fst"
            and not inspect_store(target, probe_reader=False).structurally_usable
        ):
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

    try:
        alias = register_trace(trace, args.alias)
    except OSError as exc:
        # Keep the historical contract: a store that cannot be written warns
        # and the command still reports the trace it found.
        print(f"[bwave] WARNING: {exc}", file=sys.stderr)
        alias = args.alias or _DEFAULT_ALIAS
    print(f"@{alias} -> {trace}")
    # Identity + age, so binding the wrong design (or a trace from a sim you
    # already superseded) is visible in the register output instead of being
    # inferred later from nonsense signal names.
    if trace.suffix == ".fst":
        identity = _trace_identity(trace)
        try:
            age = f"(written {format_age(trace.stat().st_mtime)})"
        except OSError:
            age = ""
        identity_label = "top scopes" if ", " in identity else "top scope"
        detail = " ".join(
            part for part in (f"{identity_label}: {identity}" if identity else "", age) if part
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


def _marker_set(sessions: dict, alias: str, extra: list[str], store: SessionStore) -> None:
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
    _save_sessions(sessions, store)
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


def _marker_delete(sessions: dict, alias: str, extra: list[str], store: SessionStore) -> None:
    if len(extra) < 2:
        sys.exit("ERROR: markers delete requires NAME")
    name = extra[1]
    markers = sessions[alias].get("markers", {})
    if name not in markers:
        sys.exit(f"ERROR: marker '{name}' not found in @{alias}")
    del markers[name]
    _save_sessions(sessions, store)
    print(f"@{alias}: deleted marker '{name}'")


def cmd_markers(args: argparse.Namespace) -> None:
    """Manage named markers for a trace alias."""
    store = current_session_store()
    sessions = _load_sessions(store)

    alias = _DEFAULT_ALIAS
    extra = list(args.extra)
    if extra and extra[0].startswith("@"):
        alias = extra.pop(0)[1:]
    if alias not in sessions:
        # `--as` (not `-as`) is the flag; show it correctly so copy/paste works.
        sys.exit(
            f"ERROR: No registered alias '@{alias}'.\n"
            f"Register a trace first: bwave register SIM_DIR --as {alias}\n"
            f"Or list known aliases by reading: {store.path}"
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
        "set": lambda: _marker_set(sessions, alias, extra, store),
        "list": lambda: _marker_list(sessions, alias),
        "delete": lambda: _marker_delete(sessions, alias, extra, store),
    }
    if action not in dispatch:
        sys.exit(f"ERROR: unknown markers action '{action}'. Use: set, list, delete")
    dispatch[action]()
