#!/usr/bin/env python3
"""
RTL Debug Helper — trace location and query wrapper.

Absorbs the mechanical parts of waveform-based debugging:
  - Trace file discovery in sim directories (.fst preferred, .vcd fallback)
  - Named trace aliases for multi-trace workflows
  - Translation layer from legacy flag-soup form to the bwave v0.2
    subcommand surface (build, signal, wave, find, sample, …)

Usage:
  bwave register SIM_DIR --as ALIAS
  bwave query [@ALIAS | TRACE_PATH] [legacy bwave args...]

The wrapper still accepts the v0.1 flag form (--wave, --find PAT VAL, etc.)
and rewrites it into v0.2 subcommand form internally so existing callers
(skills, prompts, muscle-memory) keep working. New callers can pass the
subcommand form directly — the translator passes them through.

This script is just plumbing — it captures and formats waveform data; the
debug *strategy* (what to investigate, in what order) lives with the caller.

This wrapper is the only `bwave` on PATH in the sandbox image because it is the
superset: it owns gui/register/markers and forwards every query subcommand to
the native Rust binary, which the image installs off PATH so it cannot shadow
the wrapper. The platform-neutral Python wheel does not bundle that binary.
Internal callers that need the binary resolve it with
`booley.runtime.paths.native_bwave_binary()`, never by bare name — going through this
wrapper would apply its query defaults (`--limit`, marker flags) to them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, NoReturn

from booley.bwave.contract import (
    BEYOND_RANGE_MARKER,
    DID_YOU_MEAN_MARKER,
    EXIT_USAGE,
    NO_MATCH_MARKER,
)
from booley.bwave.contract import exit_usage as _exit_usage
from booley.bwave.waveform_store import (
    conversion_failure_messages,
    discover_waveform,
    waveform_cache_dir,
)
from booley.core.host_shell import quote_arg
from booley.runtime import runtime_context

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
from booley.runtime.paths import native_bwave_binary
from booley.runtime.platform_paths import cargo_bin
from booley.runtime.timefmt import parse_timestamp

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
BWAVE_CARGO_TOML = _REPO_ROOT / "crates" / "bwave" / "Cargo.toml"


def _bwave_cmd() -> list[str]:
    """Return the command prefix for the native bwave binary.

    Resolution lives in booley.runtime.paths so this wrapper and the FIFO streamer
    find the same binary. Never resolve it by bare name:
    on PATH, `bwave` is *this* wrapper.
    """
    found = native_bwave_binary()
    if found:
        return [str(found)]
    # Dev host with a checkout but no build yet — build it once. (The sandbox
    # image ships the binary pre-built, so this path never runs in-container,
    # where the network is off and the bwave sources aren't present.)
    cargo = cargo_bin()
    print(f"bwave not found -- building from {BWAVE_CARGO_TOML} ...", file=sys.stderr)
    result = subprocess.run(
        [cargo, "build", "--release", "--manifest-path", str(BWAVE_CARGO_TOML)],
        capture_output=True,
        text=True,
        check=False,
    )
    built = native_bwave_binary()
    if result.returncode == 0 and built:
        print("bwave built successfully.", file=sys.stderr)
        return [str(built)]
    sys.exit(f"ERROR: bwave build failed (rc={result.returncode})\n{result.stderr[-500:]}")


def find_trace(work_dir: Path) -> Path | None:
    """Find and, when needed, convert a waveform for CLI registration."""
    result = discover_waveform(work_dir, cache_dir=waveform_cache_dir(work_dir))
    if result.failure_kind == "ambiguous":
        sys.exit(result.detail)
    if result.conversion is not None:
        for event in result.conversion.events:
            print(f"[bwave] {event}")
        for message in conversion_failure_messages(
            result.conversion, warning="VCD conversion failed"
        ):
            print(message, file=sys.stderr)
    if result.conversion is not None and result.conversion.success and result.selected:
        return _publish_discovered_store(work_dir, result.selected)
    return result.selected


def _publish_discovered_store(work_dir: Path, selected: Path) -> Path:
    """Materialize a converted result for the CLI's legacy registration path."""
    published = work_dir / "trace.fst"
    try:
        if selected.resolve() != published.resolve():
            shutil.copy2(selected, published)
        return published
    except OSError:
        return selected


def _trace_diagnostics(target: Path) -> str:
    """Return trace manifest/incident diagnostics for a failed registration."""
    manifest = target / "trace_status.json"
    if manifest.exists():
        try:
            detail = manifest.read_text(errors="replace").strip()
            if detail:
                return f"\n\n--- trace_status.json ---\n{detail[-4000:]}"
        except OSError:
            pass
    incident = target / "trace_incident.txt"
    if incident.exists():
        try:
            detail = incident.read_text(errors="replace").strip()
            if detail:
                return f"\n\n--- trace_incident.txt ---\n{detail[-4000:]}"
        except OSError:
            pass
    return ""


_STRAY_VCDS_SHOWN = 5


def _stray_vcd_hint(directory: Path) -> str:
    """Name top-level VCD dumps discovery skipped (it reads only trace.vcd), or ""."""
    stray = sorted(p.name for p in directory.glob("*.vcd") if p.is_file())
    if not stray:
        return ""
    more = len(stray) - _STRAY_VCDS_SHOWN
    names = ", ".join(stray[:_STRAY_VCDS_SHOWN]) + (f" and {more} more" if more > 0 else "")
    example = quote_arg(str(directory / stray[0]))
    return (
        f"\nVCD dumps here that are not named trace.vcd: {names}\n"
        "Declare the dump in [flows.sim].trace_files and re-run the sim, "
        "or pass the file directly (a raw VCD needs --build to register):\n"
        f"  bwave register {example} --as ALIAS --build"
    )


def _no_trace_found_message(directory: str | Path, note: str = "") -> str:
    """The `register DIR` / `gui DIR` error when trace discovery finds nothing."""
    path = Path(directory)
    return (
        f"ERROR: No trace file found in {directory}"
        + _stray_vcd_hint(path)
        + note
        + _trace_diagnostics(path)
    )


# ---------------------------------------------------------------------------
# Trace-session + marker persistence (kept separate from the CLI, principle 8)
# ---------------------------------------------------------------------------
# Names used internally here (dispatch table, marker resolution) have no
# suppression; the rest are re-exported only (F401-suppressed) as part of the
# CLI module's existing Python API.
# Absolute (not relative) import: cli.py can also be run directly as a script
# (`python .../cli.py ...`), where `__package__` is empty and a relative
# import would raise "attempted relative import with no known parent package".
from booley.bwave.sessions import (
    _DEFAULT_ALIAS,
    MarkerLookup,
    _get_markers,
    _load_sessions,
    _save_sessions,  # noqa: F401  # re-exported for backward compatibility
    cmd_markers,
    cmd_register,
    current_session_store,
    register_trace,  # noqa: F401  # re-exported for the tests and the facade
)


def _marker_cycle_token(token: str, markers: MarkerLookup) -> str:
    """Replace a persisted marker name with an explicit cycle token."""
    cycle = markers.cycle(token)
    return token if cycle is None else f"{cycle}c"


def _marker_bare_cycle(token: str, markers: MarkerLookup) -> str:
    """Replace a marker where the native option accepts only a bare integer."""
    cycle = markers.cycle(token)
    return token if cycle is None else str(cycle)


def _resolve_marker_range(value: str, markers: MarkerLookup) -> str:
    """Resolve persisted markers in a typed START:END time range."""
    if ":" not in value:
        return _marker_cycle_token(value, markers)
    start, end = value.split(":", 1)
    return f"{_marker_cycle_token(start, markers)}:{_marker_cycle_token(end, markers)}"


# Value-taking options of `diff` (its own and the global ones) that can precede
# the two time points; their value is never a time point.
_DIFF_VALUE_OPTIONS = frozenset(
    {
        "-s",
        "--signals",
        "--clock",
        "--reset",
        "--sample-mode",
        "--format",
        "--limit",
        "--wrapper-warning",
    }
)


def _split_option(token: str) -> tuple[str, str | None, str]:
    """Split a token into (option, attached value, joiner): `--a=v`, `-t=v`, `-tv`."""
    if token.startswith("--"):
        name, sep, value = token.partition("=")
        return (name, value, sep) if sep else (token, None, "")
    if token.startswith("-t=") or (token.startswith("-t") and len(token) > 2):
        joiner = "=" if token[2] == "=" else ""
        return "-t", token[2 + len(joiner) :], joiner
    return token, None, ""


def _cycle_grid_options(extra: list[str]) -> list[str]:
    """The options that change how a cycle maps to a tick (clock, reset, reset phase)."""
    options: list[str] = []
    for i, token in enumerate(extra):
        name, attached, _ = _split_option(token)
        if name == "--with-reset":
            options.append(name)
        elif name in ("--clock", "--reset"):
            # Keep `--clock=VALUE` joined: a spaced hyphen-prefixed value is refused.
            options.extend([token] if attached is not None else extra[i : i + 2])
    return options


def _async_marker_tick(trace: str, cycle: int, options: list[str]) -> str:
    """The tick of *cycle* in async mode, where --before/--after take ticks.

    The native async cycle resolver decides, so a clockless trace is refused
    instead of reading the cycle as a raw tick. A cycle past the end gives one
    tick past it: `--before` then keeps every match and `--after` none.
    """
    command = ["value", trace, "--async", *options]
    command += ["--at", f"{cycle}c", "--format", "json", "--limit", "1"]
    result = _run_query(command)
    if result.returncode == EXIT_USAGE and BEYOND_RANGE_MARKER in result.stderr.lower():
        return str(_trace_total_ticks(trace) + 1)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return str(_capture_data(result, command)["target_tick"])


def _resolve_diff_points(extra: list[str], markers: MarkerLookup) -> None:
    """Resolve the two time points of `diff [options] T1 T2`, never an option value."""
    points = 0
    i = 1
    while i < len(extra) and points < 2:
        if extra[i].startswith("-"):
            i += 2 if extra[i] in _DIFF_VALUE_OPTIONS else 1
            continue
        extra[i] = _marker_cycle_token(extra[i], markers)
        points += 1
        i += 1


def _resolve_markers_in_args(extra: list[str], alias: str, trace: str) -> None:
    """Resolve persisted marker names in wrapper time arguments, in-place.

    A Marker stands for its cycle: where the native option takes a bare cycle
    (`--before/--after`) it gets the number (the tick of that cycle under
    `--async`), where it takes typed time it gets `Nc`.
    """
    stored = _get_markers(_load_sessions(), alias)
    if not stored:
        return
    markers = MarkerLookup(alias, stored)

    if extra and extra[0] == "diff":
        _resolve_diff_points(extra, markers)

    async_options = _cycle_grid_options(extra) if "--async" in extra else None

    def bound(value: str, known: MarkerLookup) -> str:
        cycle = known.cycle(value)
        if cycle is None:
            return value
        if async_options is None:
            return str(cycle)
        return _async_marker_tick(trace, cycle, async_options)

    resolvers = {
        "-t": _resolve_marker_range,
        "--time": _resolve_marker_range,
        "--at": _marker_cycle_token,
        "--at-time": _marker_bare_cycle,
        "--at-cycle": _marker_bare_cycle,
        "--before": bound,
        "--after": bound,
    }
    i = 0
    while i < len(extra):
        name, attached, joiner = _split_option(extra[i])
        if name == "--diff" and attached is None and i + 2 < len(extra):
            extra[i + 1] = _marker_cycle_token(extra[i + 1], markers)
            extra[i + 2] = _marker_cycle_token(extra[i + 2], markers)
            i += 3
        elif name not in resolvers:
            i += 1
        elif attached is not None:
            extra[i] = name + joiner + resolvers[name](attached, markers)
            i += 1
        elif i + 1 < len(extra):
            extra[i + 1] = resolvers[name](extra[i + 1], markers)
            i += 2
        else:
            i += 1


def _inject_marker_flags(extra: list[str], alias: str) -> None:
    """For wave queries (both legacy --wave and v0.2 `wave` subcommand),
    inject --marker NAME TIME flags for every registered marker so the
    Rust renderer can draw the label row above the cycle header."""
    # Detect either the legacy mode flag or the v0.2 subcommand. The
    # subcommand form is the first token after the optional @alias/path,
    # which `_resolve_trace` has already popped by the time we get here.
    is_wave = "--wave" in extra or (extra and extra[0] == "wave")
    if not is_wave:
        return
    sessions = _load_sessions()
    markers = _get_markers(sessions, alias)
    if not markers:
        return
    for name, cycle in markers.items():
        extra.extend(["--marker", name, f"{cycle}c"])


# ---------------------------------------------------------------------------
# Subcommand: query
# ---------------------------------------------------------------------------
def _resolve_trace_from_session(alias: str | None = None) -> str:
    """Resolve trace path from session alias, or exit with helpful error."""
    store = current_session_store()
    sessions = _load_sessions(store)
    key = alias or _DEFAULT_ALIAS
    session = sessions.get(key)
    if not session:
        if alias:
            sys.exit(
                f"ERROR: No registered alias '@{alias}'.\n"
                f"Available: {', '.join(f'@{k}' for k in sessions if k != _DEFAULT_ALIAS) or '(none)'}"
            )
        sys.exit(
            "ERROR: No trace path given and no registered session.\n"
            "Either pass a trace path or register one:\n"
            "  bwave register SIM_DIR --as ALIAS\n"
            "Or rerun the simulation with tracing; it registers each Target's waveform:\n"
            "  booley flow sim --target TARGET --trace   (MCP: sim with trace: true)"
        )
    # session comes from the on-disk JSON session file (external input): it may
    # be a non-object or lack the "trace" key if hand-edited or corrupted.
    trace = session.get("trace") if isinstance(session, dict) else None
    if not isinstance(trace, str) or not trace:
        sys.exit(
            f"ERROR: Session for @{key} is malformed (no trace path).\n"
            f"Re-register with: bwave register SIM_DIR --as {key}"
        )
    # A trace inside the checkout is stored relative to it, so a moved checkout still resolves.
    trace = str(store.trace_path(trace))
    if not Path(trace).exists():
        sys.exit(
            f"ERROR: Trace for @{key} no longer exists: {trace}\n"
            f"Re-register with: bwave register SIM_DIR --as {key}"
        )
    _warn_if_trace_stale(trace, session.get("registered_at"))
    return trace


def _warn_if_trace_stale(trace: str | Path, registered_at: object = None) -> None:
    """Warn on stderr if the trace *data* is >24h old.

    Staleness is keyed on the trace file's own mtime, not on when the alias
    was registered: re-simulating in place refreshes the data (no warning
    due), while registering an ancient file five minutes ago does not make
    its data any younger. ``registered_at`` (untrusted session-file JSON) is
    only the fallback when the file cannot be stat'ed — and either way a
    broken input silently skips the (non-critical) cosmetic check rather
    than crashing trace resolution.
    """
    age_s: float | None = None
    try:
        age_s = max(0.0, time.time() - Path(trace).stat().st_mtime)
    except (OSError, TypeError):
        if isinstance(registered_at, str):
            try:
                registered = parse_timestamp(registered_at)
            except ValueError:
                return
            age_s = (datetime.now(UTC) - registered).total_seconds()
    if age_s is not None and age_s > 86400:
        print(
            "WARNING: registered trace data is older than 24h -- re-run the sim "
            "(and re-register) before trusting it",
            file=sys.stderr,
        )


def _resolve_trace(extra: list[str]) -> str:
    """Pop trace specifier from extra args and return resolved path."""
    trace_path = None
    if extra and extra[0].startswith("@"):
        alias = extra.pop(0)[1:]
        # A bare `@` would fall through to the default session — almost
        # certainly a typo, so refuse rather than silently resolve `_last`.
        if not alias:
            sys.exit(
                "ERROR: Empty alias '@'. Use @ALIAS, or omit the alias "
                "to use the last registered trace."
            )
        trace_path = _resolve_trace_from_session(alias)
    elif (
        extra
        and extra[0] in _V02_CONSUMER_SUBCOMMANDS
        and len(extra) > 1
        and not extra[1].startswith("-")
        and Path(extra[1]).exists()
    ):
        trace_path = extra.pop(1)
    elif extra and not extra[0].startswith("-") and Path(extra[0]).exists():
        trace_path = extra.pop(0)
    if trace_path is None:
        trace_path = _resolve_trace_from_session()
    if not Path(trace_path).exists():
        sys.exit(f"ERROR: Trace file not found: {trace_path}")
    return trace_path


def cmd_query(args: argparse.Namespace) -> None:
    """Thin wrapper around bwave with sane defaults.

    Accepts both v0.1 legacy flag form and v0.2 subcommand form. Legacy
    form is rewritten into subcommand form before invoking the binary.
    """
    extra = list(args.extra)

    # Meta subcommands (`schema`, `docs`, `skill`) inspect bwave itself
    # rather than a trace — forward them verbatim without resolving an
    # alias or running the limit-default machinery. The MCP tool surface
    # makes callers prepend @ALIAS even for meta commands, so peek past
    # an optional leading alias before deciding.
    meta_idx = 1 if (extra and extra[0].startswith("@")) else 0
    if len(extra) > meta_idx and extra[meta_idx] in _V02_DIRECT_SUBCOMMANDS:
        sys.exit(_run([*_bwave_cmd(), *extra[meta_idx:]]))

    # Determine alias for marker lookup (before _resolve_trace consumes it)
    alias = _DEFAULT_ALIAS
    if extra and extra[0].startswith("@"):
        alias = extra[0][1:]  # peek, don't pop yet

    trace_path = _resolve_trace(extra)
    _resolve_markers_in_args(extra, alias, trace_path)
    _inject_marker_flags(extra, alias)

    extract = _bwave_cmd()

    # --grep shortcut: list filtered by pattern (wrapper-only sugar)
    if "--grep" in extra:
        _handle_grep(extract, trace_path, extra)
        return

    # Translate legacy flag form → v0.2 subcommand form.
    cmd_args = _translate_to_subcommand(extra, trace_path)
    _apply_limit_default(cmd_args)
    # Propagate the binary's exit code so callers / automation can detect
    # soft errors (bad glob, mutex flags, malformed time, etc.) — the
    # wrapper used to swallow these and always return 0.
    sys.exit(_run([*extract, *cmd_args]))


# v0.2 subcommand names — passed verbatim if user already uses them.
# Split into "consumer" subcommands (need a BWAVE_FILE positional arg, the
# wrapper auto-injects the registered trace) and "meta" subcommands (don't
# touch traces, just inspect bwave itself — forwarded as-is).
_V02_CONSUMER_SUBCOMMANDS = frozenset(
    {
        "list",
        "signal",
        "wave",
        "value",
        "find",
        "sample",
        "diff",
        "distance",
        "stats",
        "overview",
        "stuck",
    }
)
_V02_META_SUBCOMMANDS = frozenset({"schema", "docs", "skill"})
_V02_DIRECT_SUBCOMMANDS = frozenset({"build"}) | _V02_META_SUBCOMMANDS
_V02_SUBCOMMANDS = _V02_CONSUMER_SUBCOMMANDS | _V02_DIRECT_SUBCOMMANDS

# Legacy v0.1 mode flags the wrapper used to forward verbatim.
_LEGACY_MODE_FLAGS = {
    "--list",
    "--wave",
    "--find",
    "--sample",
    "--stats",
    "--stuck",
    "--diff",
    "--distance",
    "--at",
    "--at-time",
    "--at-cycle",
    "--log",
    "--tree",
}


def _passthrough_subcommand(extra: list[str], trace_path: str) -> list[str]:
    """Handle the case where `extra` already starts with a v0.2 subcommand.

    Injects `trace_path` after the subcommand name unless the user already
    supplied one, and quietly rewrites the `value` subcommand's `-t T` to
    `--at T`. Returns the finished v0.2 arg list.
    """
    sub = extra[0]
    rest = extra[1:]
    # The `value` subcommand uniquely uses `--at T` for its single time
    # point — every other subcommand takes `-t START:END`. Users hit
    # this paper cut constantly, so quietly rewrite `-t T` to `--at T`
    # here. (If the user actually wrote a range like `-t 100:200`, we
    # let it pass through; clap will reject it with a clearer error
    # than "unexpected argument -t" while we figure out their intent.)
    if sub == "value":
        for i, tok in enumerate(rest):
            if tok == "-t" and i + 1 < len(rest) and ":" not in rest[i + 1]:
                rest = [*rest[:i], "--at", *rest[i + 1 :]]
                break
    # If the user already passed a trace path / store file, trust them.
    # Otherwise inject ours right after the subcommand name.
    if (
        rest
        and not rest[0].startswith("-")
        and (rest[0].endswith(".fst") or rest[0].endswith(".vcd"))
    ):
        return [sub, *rest]
    return [sub, trace_path, *rest]


def _translate_legacy_flags(extra: list[str], trace_path: str) -> list[str]:
    """Translate a legacy mode-flag arg list into v0.2 subcommand form.

    `extra` is mutated in place as flags/positionals are consumed. Assumes
    at least one legacy mode flag is present (caller has already validated).
    """
    # --log was wrapper sugar for "default trace" — drop it.
    while "--log" in extra:
        extra.remove("--log")

    modes = [
        flag for flag in extra if flag in _LEGACY_MODE_FLAGS and flag not in {"--log", "--tree"}
    ]
    if "--distance" in modes:
        modes = [flag for flag in modes if flag != "--stats"]
    if len(modes) > 1:
        _exit_usage("ERROR: multiple query modes are mutually exclusive")

    # Distance owns --stats as its modifier.
    if "--distance" in extra:
        idx = extra.index("--distance")
        if idx + 2 >= len(extra):
            _exit_usage("ERROR: --distance requires PATTERN VALUE")
        pat, val = extra[idx + 1 : idx + 3]
        del extra[idx : idx + 3]
        return ["distance", trace_path, pat, val, *extra]

    # Simple no-positional mode flags.
    for flag, sub in (("--list", "list"), ("--wave", "wave"), ("--stats", "stats")):
        if flag in extra:
            extra.remove(flag)
            return [sub, trace_path, *extra]

    # --tree alone (without --list) implies `list --tree`.
    if "--tree" in extra and "list" not in extra:
        return ["list", trace_path, *extra]

    # --stuck [VALUE]: optional positional follows.
    if "--stuck" in extra:
        idx = extra.index("--stuck")
        # Treat next token as VALUE iff it isn't another flag.
        if idx + 1 < len(extra) and not extra[idx + 1].startswith("-"):
            value = extra[idx + 1]
            del extra[idx : idx + 2]
            return ["stuck", trace_path, value, *extra]
        del extra[idx]
        return ["stuck", trace_path, *extra]

    # --find PAT VAL → find <trace> PAT VAL
    for flag, sub in (("--find", "find"), ("--sample", "sample")):
        if flag in extra:
            idx = extra.index(flag)
            if idx + 2 >= len(extra):
                sys.exit(f"ERROR: {flag} requires two positional args (PATTERN VALUE)")
            pat = extra[idx + 1]
            val = extra[idx + 2]
            del extra[idx : idx + 3]
            return [sub, trace_path, pat, val, *extra]

    # --diff T1 T2
    if "--diff" in extra:
        idx = extra.index("--diff")
        if idx + 2 >= len(extra):
            sys.exit("ERROR: --diff requires T1 T2")
        t1 = extra[idx + 1]
        t2 = extra[idx + 2]
        del extra[idx : idx + 3]
        return ["diff", trace_path, t1, t2, *extra]

    # --at N (and legacy --at-time / --at-cycle aliases)
    for flag in ("--at", "--at-time", "--at-cycle"):
        if flag in extra:
            idx = extra.index(flag)
            if idx + 1 >= len(extra):
                sys.exit(f"ERROR: {flag} requires N")
            n = extra[idx + 1]
            del extra[idx : idx + 2]
            return ["value", trace_path, "--at", n, *extra]

    # Should be unreachable given the mode-flag check above.
    sys.exit("ERROR: internal: failed to translate args to v0.2 subcommand")


def _translate_to_subcommand(extra: list[str], trace_path: str) -> list[str]:
    """Convert a legacy flag-form arg list into v0.2 subcommand form.

    If `extra` already starts with a v0.2 subcommand name, insert
    `trace_path` after it (unless one is already present) and return.

    Otherwise scan for the legacy mode flag, extract its positional
    args, and emit `<subcommand> <trace_path> <positionals> <rest>`.
    """
    extra = list(extra)

    # Pass-through: user already used subcommand form.
    if extra and extra[0] in _V02_CONSUMER_SUBCOMMANDS:
        return _passthrough_subcommand(extra, trace_path)

    # No mode flag at all → default to `signal` subcommand.
    if not any(f in extra for f in _LEGACY_MODE_FLAGS):
        sys.exit(
            "ERROR: No query mode specified. Pick one:\n"
            "  --list, --wave, --log, --find, --sample, --stats,\n"
            "  --stuck, --diff, --distance, --at, --tree\n"
            "Or use the v0.2 subcommand form directly:\n"
            "  bwave <signal|wave|find|sample|...> <trace> ..."
        )

    return _translate_legacy_flags(extra, trace_path)


def _handle_grep(extract: list[str], trace_path: str, extra: list[str]) -> None:
    """Handle --grep shortcut: list signals filtered by pattern (v0.2 surface)."""
    idx = extra.index("--grep")
    if idx + 1 >= len(extra):
        _exit_usage("ERROR: --grep requires a pattern argument")
    pattern = extra[idx + 1]
    remaining = [*extra[:idx], *extra[idx + 2 :]]
    cmd_args = ["list", trace_path, "-s", pattern, *remaining]
    _apply_limit_default(cmd_args)
    # Propagate the inner exit code like every other query path — --grep
    # used to swallow it, so `bwave @dut --grep '[oops'` exited 0 while the
    # inner `list` exited 2 on the bad glob.
    sys.exit(_run([*extract, *cmd_args]))


# `list` prints one line per signal, so 5000 lines is ~150 KB — an order of
# magnitude past the MCP stdout window, which then keeps the *tail* and drops
# the scope header plus the top of the tree. A few hundred names fits the
# window; the binary says how many were withheld and how to raise it.
_LIST_LIMIT_DEFAULT = "400"
_QUERY_LIMIT_DEFAULT = "2000"


def _apply_limit_default(cmd_args: list[str]) -> None:
    """Normalize explicit limits without concealing native input errors."""
    locations = [
        i for i, arg in enumerate(cmd_args) if arg == "--limit" or arg.startswith("--limit=")
    ]
    if not locations:
        default = _LIST_LIMIT_DEFAULT if cmd_args[:1] == ["list"] else _QUERY_LIMIT_DEFAULT
        cmd_args.extend(["--limit", default])
        return
    if len(locations) != 1:
        return
    idx = locations[0]
    equals_form = cmd_args[idx].startswith("--limit=")
    if not equals_form and idx + 1 >= len(cmd_args):
        return
    raw = cmd_args[idx].partition("=")[2] if equals_form else cmd_args[idx + 1]
    if re.fullmatch(r"\+?[0-9]+", raw) is None:
        return
    try:
        value = int(raw)
    except ValueError:
        return
    if value > 10000:
        cmd_args[idx if equals_form else idx + 1] = "--limit=10000" if equals_form else "10000"
        warning = "--limit capped at 10000"
        print(warning, file=sys.stderr)
        cmd_args.extend(["--wrapper-warning", warning])


# ---------------------------------------------------------------------------
# Subcommand: gui (selection check; no viewer)
# ---------------------------------------------------------------------------
# Cap on the glob expansion of `gui --signals`. A selection of hundreds of
# signals is a query bug, so overflow is a hard error, not a silent truncation.
_GUI_SIGNAL_CAP = 64

# `gui --time` brackets the range with two markers; the plan keys them by name.
_MAIN_MARKER = "markerTime"
_ALT_MARKER = "altMarkerTime"

_NO_VIEWER_MESSAGE = (
    "ERROR: Booley has no waveform viewer until the web Dashboard's waveform "
    "view ships. The text queries still work: see `bwave --help`."
)


def _resolve_gui_target(target: str | None) -> tuple[str, str]:
    """Resolve `gui`'s positional to (absolute trace path, alias for markers)."""
    # `query` treats a nonexistent first token as query args and falls back to
    # the session trace; `gui` takes no trailing args, so a nonexistent
    # explicit path can only be a typo — error instead of silently checking a
    # different trace.
    if target and not target.startswith("@") and not Path(target).exists():
        sys.exit(f"ERROR: Trace file not found: {target}")
    # Marker names in --time/--cursor resolve against the alias actually used
    # (same rule as query: bare invocations read the default session).
    alias = target[1:] if target and target.startswith("@") else _DEFAULT_ALIAS
    trace_path = _resolve_trace([target] if target else [])
    # Users type `bwave gui SIM_DIR` by analogy with `bwave register
    # SIM_DIR`. Reuse register's discovery (.fst preferred, .vcd fallback
    # with auto-conversion) to find the trace in the directory.
    if Path(trace_path).is_dir():
        found = find_trace(Path(trace_path))
        if found is None:
            sys.exit(_no_trace_found_message(trace_path))
        trace_path = str(found)
    # .lower(): the host FS may be case-insensitive (TRACE.BWAVE resolves).
    # Exit 2 to match the Rust binary's own legacy-.bwave rejection.
    if Path(trace_path).suffix.lower() == ".bwave":
        _exit_usage("ERROR: legacy .bwave cache -- not viewable; re-run the sim to produce FST")
    return str(Path(trace_path).resolve()), alias


# Cap on how much of the inner binary's output rides along in a wrapper error.
# Its diagnostics are a handful of lines; anything longer is a dump we tail.
_INNER_OUTPUT_TAIL_CHARS = 4000


def _inner_message(result: subprocess.CompletedProcess[str]) -> str:
    """The inner binary's own message, tail-capped — stderr first, then stdout.

    Diagnostics go to stderr (`--format json` owns stdout), so stderr is the
    message; stdout is the fallback for the rare plain-text failure.
    """
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    if len(text) > _INNER_OUTPUT_TAIL_CHARS:
        text = "[...truncated...]\n" + text[-_INNER_OUTPUT_TAIL_CHARS:]
    return text


def _exit_with_inner_error(
    result: subprocess.CompletedProcess[str],
    cmd_args: list[str],
    plumbing: str,
) -> NoReturn:
    """Exit surfacing the inner binary's message verbatim, never the plumbing.

    The Rust binary writes genuinely useful diagnostics — e.g. an ERROR line
    plus a did-you-mean HINT — and some failures exit with no JSON on stdout,
    some even with status 0. The
    wrapper used to answer that with "bwave returned non-JSON output for: value
    <argv>", which hides the hint the user needed and leaks internal plumbing.
    So: the inner text is the error; *plumbing* (which internal query broke, and
    how) goes to the debug channel only.
    """
    logger.debug("%s: %s", plumbing, " ".join(cmd_args))
    detail = _inner_message(result)
    if not detail:
        # Truly silent failure — the plumbing detail is all we have.
        sys.exit(f"ERROR: {plumbing}: {' '.join(cmd_args)}")
    # The binary already prefixes its hard errors with "ERROR:"; don't stutter.
    sys.exit(detail if detail.startswith("ERROR") else f"ERROR: {detail}")


def _run_capture(cmd_args: list[str]) -> dict:
    """Run the Rust binary with --format json and return the envelope `data`.

    Internal helper queries (gui's glob/tick resolution) must not stream to
    the user's terminal like _run(); a failure here is an input problem
    (bad glob, bad time token), so the binary's stderr is surfaced verbatim.
    """
    return _capture_data(_run_query(cmd_args), cmd_args)


def _run_query(cmd_args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one internal Rust query, exiting only on a timeout."""
    cmd = [*_bwave_cmd(), *cmd_args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        sys.exit(f"ERROR: bwave query timed out (120s): {' '.join(cmd_args)}")


def _capture_envelope(result: subprocess.CompletedProcess[str], cmd_args: list[str]) -> dict:
    """Return the JSON envelope, surfacing the binary's own error on failure."""
    if result.returncode != 0:
        _exit_with_inner_error(result, cmd_args, "bwave query failed")
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        _exit_with_inner_error(result, cmd_args, "bwave returned non-JSON output for")
    if not isinstance(envelope.get("data"), dict):
        _exit_with_inner_error(result, cmd_args, "bwave JSON envelope has no data object for")
    return envelope


def _capture_data(result: subprocess.CompletedProcess[str], cmd_args: list[str]) -> dict:
    """Return the JSON envelope `data`, surfacing the binary's own error on failure."""
    return _capture_envelope(result, cmd_args)["data"]


# Trailing bit range on a bwave signal name: "[7:0]" (vector) or "[3]" (bit).
_BIT_RANGE_RE = re.compile(r"\[(\d+)(?::(\d+))?\]$")

_RADIX_FORMATS = {
    "b": "binary",
    "h": "hexadecimal",
    "d": "decimal",
}
# The semantic colors a `@COLOR` suffix may name.
_WAVEFORM_COLORS = frozenset({"red", "blue", "green"})


class _DisplaySelector(NamedTuple):
    """One signal glob plus optional human-presentation metadata."""

    glob: str
    number_format: str | None
    color: str | None


class _OpenSignal(NamedTuple):
    """One requested signal: its bare path plus the bit range split off its name.

    bwave names a vector `tb.dut.state[3:0]` — the VCD/FST bit-range token
    joined onto the leaf. The plan keeps the bare path and the range as
    separate msb/lsb fields, and keeps the raw name as *fallback* for the one
    case the string cannot settle: a 1-bit `foo[3]` may equally be an
    identifier with a bracket in it (an unpacked-array element like `mem[0]`).
    """

    bwave_name: str  # as `bwave list` prints it — what the user recognizes
    instance_path: str  # bare path without the bit range
    msb: int | None
    lsb: int | None
    fallback: str | None  # the whole name, when a 1-bit bracket may be part of it
    number_format: str | None = None
    color: str | None = None


class _OpenGroup(NamedTuple):
    """One requested named group and its resolved signals."""

    name: str
    signals: list[_OpenSignal]


def _split_bit_range(name: str, width: int | None) -> _OpenSignal:
    """Split a bwave signal name into its bare path and bit range."""
    match = _BIT_RANGE_RE.search(name)
    if match is None:
        return _OpenSignal(name, name, None, None, None)
    bare = name[: match.start()]
    msb = int(match.group(1))
    if match.group(2) is not None:  # "[7:0]" — a colon is never an identifier
        return _OpenSignal(name, bare, msb, int(match.group(2)), None)
    # Single index. bwave only ever appends one to a 1-bit var, so anything
    # wider is an identifier that happens to end in a bracket — leave it whole.
    if width is not None and width > 1:
        return _OpenSignal(name, name, None, None, None)
    return _OpenSignal(name, bare, msb, msb, fallback=name)


def _parse_display_selector(spec: str, *, option: str) -> _DisplaySelector:
    """Parse ``GLOB[%RADIX][@COLOR]`` before passing the glob to B-Wave."""
    raw = spec.strip()
    if "@" in raw and "%" in raw and raw.rfind("@") < raw.rfind("%"):
        _exit_usage(f"ERROR: radix must precede color in {option} {spec!r}; use GLOB%RADIX@COLOR")
    color: str | None = None
    if "@" in raw:
        raw, _separator, color = raw.rpartition("@")
        color = color.lower()
        if color not in _WAVEFORM_COLORS:
            _exit_usage(
                f"ERROR: unknown waveform color '@{color}' in {option} {spec!r}; "
                "valid colors are @red, @blue, @green"
            )
    number_format: str | None = None
    if "%" in raw:
        raw, _separator, radix = raw.rpartition("%")
        number_format = _RADIX_FORMATS.get(radix.lower())
        if number_format is None:
            _exit_usage(
                f"ERROR: unknown radix suffix '%{radix}' in {option} {spec!r}; "
                "valid radixes are %b, %h, %d"
            )
    if not raw:
        _exit_usage(f"ERROR: {option} needs a non-empty signal glob before its display suffix")
    return _DisplaySelector(raw, number_format, color)


def _with_selector_style(
    signal: _OpenSignal, selector: _DisplaySelector | _OpenSignal
) -> _OpenSignal:
    """Overlay only the presentation properties explicitly requested."""
    return signal._replace(
        number_format=selector.number_format or signal.number_format,
        color=selector.color or signal.color,
    )


def _list_envelope(trace: str, globs: list[str]) -> dict:
    """One `bwave list` envelope — the trace header plus the matched signals.

    This is the cheap query: `list` reads the hierarchy only (~100 ms on a
    440 MB store) where `stats` walks every transition of every signal and can
    run for minutes. Everything `gui` needs to resolve a selection — the
    signal set, the clock, the end of the trace — rides on this one call.
    """
    flags = [flag for glob in globs for flag in ("-s", glob)]
    cmd_args = ["list", trace, *flags, "--no-bit-slices", "--format", "json", "--limit", "10000"]
    result = _run_query(cmd_args)
    if result.returncode == EXIT_USAGE:
        # `gui` takes whole signals: a bit-slice selector is refused.
        _exit_usage(_inner_message(result))
    return _capture_envelope(result, cmd_args)


def _list_query(trace: str, globs: list[str]) -> dict:
    """The `data` object of one `bwave list` envelope (see `_list_envelope`)."""
    return _list_envelope(trace, globs)["data"]


def _trace_total_ticks(trace: str) -> int:
    """The end tick `bwave list` reports for *trace*."""
    total = _list_query(trace, []).get("total_ticks")
    if not isinstance(total, int):
        sys.exit("ERROR: bwave list did not report total_ticks")
    return total


def _clock_of(data: dict) -> _OpenSignal | None:
    """The clock the Rust binary detected, from a `list` envelope.

    Reusing its detection (which honors `--clock`) beats re-deriving "1-bit and
    named *clk*" wrapper-side. None on an async trace with no clock.
    """
    name = data.get("clock")
    if not isinstance(name, str) or not name:
        return None
    return _split_bit_range(name, 1)  # a detected clock is 1-bit by construction


def _resolve_signal_globs(
    trace: str, globs: list[str], cap: int, *, option: str = "--signals"
) -> tuple[list[_OpenSignal], dict]:
    """Resolve each selector through B-Wave, then attach its presentation.

    B-Wave remains the only owner of signal-pattern semantics. In particular,
    trailing Verilog indices such as ``words[56]`` are literals there but
    character classes to Python glob implementations. Resolving one selector
    per query preserves that contract without reimplementing its matcher.
    """
    selectors = [_parse_display_selector(glob, option=option) for glob in globs]
    listing: dict | None = None
    signals: dict[str, _OpenSignal] = {}
    unmatched: list[str] = []
    suggestions: list[str] = []
    for selector in selectors:
        envelope = _list_envelope(trace, [selector.glob])
        data = envelope["data"]
        if listing is None:
            listing = data
        if not data.get("signals"):
            unmatched.append(selector.glob)
            suggestions.extend(
                warning
                for warning in envelope.get("warnings") or []
                if isinstance(warning, str) and DID_YOU_MEAN_MARKER in warning
            )
        prefix = data.get("scope_prefix") or ""
        for sig in data.get("signals", []):
            name = sig.get("name")
            if not isinstance(name, str):
                continue
            full = f"{prefix}.{name}" if prefix else name
            width = sig.get("width")
            signal = signals.get(full) or _split_bit_range(
                full, width if isinstance(width, int) else None
            )
            signals[full] = _with_selector_style(signal, selector)
    if not signals:
        # Same wording class (and exit code) as the Rust total-miss error —
        # NO_MATCH_MARKER keeps the two surfaces greppable the same way.
        pat = ", ".join(f"'{g}'" for g in globs)
        hint = f"; {' '.join(suggestions)}" if suggestions else ""
        _exit_usage(f"ERROR: {NO_MATCH_MARKER} {option} {pat} in {trace}{hint}")
    for glob in unmatched:
        # Like the Rust partial miss: the other selectors still show.
        print(f"WARNING: {NO_MATCH_MARKER} {option} '{glob}'; not shown", file=sys.stderr)
    resolved = list(signals.values())
    if len(resolved) > cap:
        head = "\n".join(f"  {signal.bwave_name}" for signal in resolved[:5])
        _exit_usage(
            f"ERROR: {option} matches {len(resolved)} signals (cap {cap}); first matches:\n"
            f"{head}\n"
            "Narrow the glob, or raise the cap with --max-signals if you really "
            "want them all."
        )
    assert listing is not None
    return resolved, listing


def _parse_group_spec(spec: str) -> tuple[str, str]:
    """Parse one ``NAME=GLOB`` group argument at the CLI boundary."""
    name, separator, glob = spec.partition("=")
    name = name.strip()
    glob = glob.strip()
    if not separator or not name or not glob:
        _exit_usage(f"ERROR: --group takes NAME=GLOB with both parts non-empty (got {spec!r})")
    if len(name) > 80 or any(ord(char) < 32 or char in "<>&" for char in name):
        _exit_usage(
            "ERROR: --group NAME must be at most 80 characters and cannot contain "
            f"control characters or <, >, &: {name!r}"
        )
    return name, glob


def _resolve_signal_groups(
    trace: str, specs: list[str], cap: int
) -> tuple[list[_OpenGroup], dict | None]:
    """Resolve repeatable named groups, merging repeated names in input order."""
    by_name: dict[str, list[_OpenSignal]] = {}
    listing: dict | None = None
    seen: set[str] = set()
    for spec in specs:
        name, glob = _parse_group_spec(spec)
        matches, current_listing = _resolve_signal_globs(
            trace, [glob], cap, option=f"--group {name!r}"
        )
        if listing is None:
            listing = current_listing
        group = by_name.setdefault(name, [])
        for signal in matches:
            if signal.bwave_name in seen:
                continue
            seen.add(signal.bwave_name)
            group.append(signal)
    if len(seen) > cap:
        head = "\n".join(
            f"  {signal.bwave_name}" for group in by_name.values() for signal in group
        )
        _exit_usage(
            f"ERROR: --group matches {len(seen)} unique signals (cap {cap}); "
            f"first matches:\n{head[:1000]}\n"
            "Narrow the globs, or raise the cap with --max-signals if you really "
            "want them all."
        )
    return [_OpenGroup(name, signals) for name, signals in by_name.items()], listing


def _merge_gui_signals(
    flat: list[_OpenSignal], groups: list[_OpenGroup], cap: int
) -> tuple[list[_OpenSignal], list[_OpenGroup]]:
    """Deduplicate one view, giving an explicit named group ownership."""
    grouped_names = {signal.bwave_name for group in groups for signal in group.signals}
    merged: dict[str, _OpenSignal] = {}
    ordered = [*flat, *(signal for group in groups for signal in group.signals)]
    for signal in ordered:
        previous = merged.get(signal.bwave_name)
        merged[signal.bwave_name] = (
            signal if previous is None else _with_selector_style(previous, signal)
        )
    flat = [merged[signal.bwave_name] for signal in flat if signal.bwave_name not in grouped_names]
    flat = list({signal.bwave_name: signal for signal in flat}.values())
    groups = [
        _OpenGroup(
            group.name,
            list(
                {signal.bwave_name: merged[signal.bwave_name] for signal in group.signals}.values()
            ),
        )
        for group in groups
    ]
    unique = [*flat, *(signal for group in groups for signal in group.signals)]
    if len(unique) > cap:
        head = "\n".join(f"  {signal.bwave_name}" for signal in unique[:5])
        _exit_usage(
            f"ERROR: gui selection matches {len(unique)} signals (cap {cap}); "
            f"first matches:\n{head}\n"
            "Narrow the globs, or raise the cap with --max-signals if you really "
            "want them all."
        )
    return unique, groups


class _TickResolver:
    """Convert gui's time tokens (cycles/markers/units) to file-timescale ticks.

    The plan speaks native FST/VCD timescale units; the Rust binary already
    resolves every accepted time token to that unit as `valueData.target_tick`,
    so one tiny `value` query per distinct token is the whole conversion — no
    clock math is duplicated wrapper-side (and the sync/async + marker grammar
    stays single-sourced in Rust).
    """

    def __init__(self, trace: str, alias: str):
        self.trace = trace
        self.markers = MarkerLookup(alias, _get_markers(_load_sessions(), alias))
        self._total_ticks: int | None = None

    def to_tick(self, token: str, *, option: str) -> int:
        """Resolve *token*; one past the end of the trace is a usage error."""
        tick = self._tick_within_trace(token)
        if tick is None:
            sys.exit(f"ERROR: {option} {token} is past the end of the trace")
        return tick

    def _tick_within_trace(self, token: str) -> int | None:
        """Resolve *token* to a tick, or None when it lies past the end of the trace."""
        # Markers are stored as cycle numbers; make the unit explicit so the
        # token survives async traces' bare-integer rejection with a clear
        # error pointing at the cycle grammar rather than a mystery number.
        cycle = self.markers.cycle(token)
        if cycle is not None:
            token = f"{cycle}c"
        command = ["value", self.trace]
        # Tick and physical-time tokens are absolute file positions. Resolving
        # them in sync mode first quantizes them to a clock cycle and then maps
        # that cycle back through the effective-start offset. Besides shifting
        # exact ranges, that makes absolute tokens unusable on clockless traces.
        # Async mode asks the native engine for the raw file tick directly.
        if token.endswith(("t", "ps", "ns", "us", "ms")):
            command.append("--async")
        command.extend(["--at", token, "--format", "json", "--limit", "1"])
        result = _run_query(command)
        if result.returncode == EXIT_USAGE and BEYOND_RANGE_MARKER in result.stderr.lower():
            return None
        data = _capture_data(result, command)
        tick = data.get("target_tick")
        if not isinstance(tick, int):
            sys.exit(f"ERROR: could not resolve time token '{token}' to a tick")
        return tick

    def total_ticks(self) -> int:
        """End of the trace, for open-ended ranges (`--time 100c:`).

        Read off `list`, not `stats`: stats walks every transition of every
        signal, which on a large store runs for minutes and blows the query
        timeout — so an open-ended range used to hang rather than resolve.
        """
        if self._total_ticks is None:
            self._total_ticks = _trace_total_ticks(self.trace)
        return self._total_ticks

    def range_to_ticks(self, spec: str) -> tuple[int, int]:
        """Resolve a START:END --time spec (either end optional)."""
        if ":" not in spec:
            sys.exit(
                f"ERROR: --time takes a range START:END (got '{spec}'). "
                "Either end may be omitted: '500c:' or ':100ns'."
            )
        raw_start, raw_end = spec.split(":", 1)
        start = self.to_tick(raw_start, option="--time start") if raw_start else 0
        end = self._tick_within_trace(raw_end) if raw_end else self.total_ticks()
        # A window cannot end past the trace (its end is the last timestamp plus
        # a margin, never less than `list`'s total_ticks).
        last = self.total_ticks()
        if start >= last:
            sys.exit(
                f"ERROR: --time starts at tick {start}, at or after the end of the trace "
                f"(tick {last})"
            )
        if end is None or end > last:
            print(
                f"NOTE: --time end {raw_end} is past the end of the trace; "
                f"checking up to tick {last}",
                file=sys.stderr,
            )
            end = last
        if start >= end:
            sys.exit(f"ERROR: empty --time range: start tick {start} >= end tick {end}")
        return start, end


class _GuiPlan(NamedTuple):
    signals: list[_OpenSignal]
    groups: list[_OpenGroup]
    viewport: tuple[int, int] | None
    markers: list[tuple[int, str, str]]


def _resolve_gui_selection(
    trace_abs: str, args: argparse.Namespace
) -> tuple[list[_OpenSignal], list[_OpenGroup]]:
    """Resolve flat/grouped globs and the implicit top-level clock."""
    cap = args.max_signals if args.max_signals is not None else _GUI_SIGNAL_CAP
    if cap <= 0:
        _exit_usage("ERROR: --max-signals must be greater than zero")
    flat_signals: list[_OpenSignal] = []
    listing: dict | None = None
    if args.signals:
        flat_signals, listing = _resolve_signal_globs(trace_abs, args.signals, cap)
    groups, group_listing = _resolve_signal_groups(trace_abs, args.groups, cap)
    listing = listing if listing is not None else group_listing
    signals, groups = _merge_gui_signals(flat_signals, groups, cap)
    clock = _clock_of(listing) if listing and not args.append and not args.no_clock else None
    if clock is None:
        return signals, groups
    requested_clock = next(
        (signal for signal in signals if signal.bwave_name == clock.bwave_name), None
    )
    if requested_clock is not None:
        clock = _with_selector_style(clock, requested_clock)
    groups = [
        _OpenGroup(
            group.name,
            [signal for signal in group.signals if signal.bwave_name != clock.bwave_name],
        )
        for group in groups
    ]
    signals = [signal for signal in signals if signal.bwave_name != clock.bwave_name]
    return [clock, *signals], [group for group in groups if group.signals]


def _build_gui_plan(trace_abs: str, alias: str, args: argparse.Namespace) -> _GuiPlan:
    """Resolve the side-effect-free signal, viewport, and marker plan."""
    signals, groups = _resolve_gui_selection(trace_abs, args)
    resolver = _TickResolver(trace_abs, alias)
    viewport = resolver.range_to_ticks(args.time) if args.time else None
    cursor_tick = resolver.to_tick(args.cursor, option="--cursor") if args.cursor else None
    markers: list[tuple[int, str, str]] = []
    if viewport is not None:
        markers.append(
            (cursor_tick if cursor_tick is not None else viewport[0], _MAIN_MARKER, "START")
        )
        markers.append((viewport[1], _ALT_MARKER, "END"))
    elif cursor_tick is not None:
        markers.append((cursor_tick, _MAIN_MARKER, "cursor"))
    return _GuiPlan(signals, groups, viewport, markers)


def cmd_gui(args: argparse.Namespace) -> None:
    """Resolve the trace and the selection, then report that Booley has no viewer."""
    trace_abs, alias = _resolve_gui_target(args.target)
    if args.signals or args.groups or args.time or args.cursor or args.append:
        if args.append and not (args.signals or args.groups or args.time or args.cursor):
            sys.exit(
                "ERROR: --append needs something to add or move: --signals/--group/--time/--cursor"
            )
        _build_gui_plan(trace_abs, alias, args)
    sys.exit(_NO_VIEWER_MESSAGE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str]) -> int:
    """Run a command, streaming stdout/stderr."""
    try:
        result = subprocess.run(
            cmd,
            timeout=120,
            check=False,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print("ERROR: Command timed out (120s)")
        return 1
    except FileNotFoundError:
        print(f"ERROR: Command not found: {cmd[0]}")
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_QUERY_HELP_TEXT = """\
usage: bwave [@ALIAS | TRACE_PATH] <subcommand> [args...] [options...]

Query signal values, waveforms, and statistics from a registered trace.

Use the subcommand form for queries. Run `bwave <subcommand> --help` for authoritative
per-command flags, or `bwave docs topics` for the full reference.

Subcommands (pick one):
  list               Signal tree with bit widths
  signal             Cycle-by-cycle trace (default query mode)
  wave               Horizontal waveform (rows=signals, cols=cycles)
  value --at T       Snapshot all signals at one time point
  find PAT VALUE     Find cycles where PAT equals VALUE
  sample PAT VALUE   Snapshot signals each time PAT matches VALUE
  diff T1 T2         Compare values between two time points
  distance PAT VAL [--to PAT VAL]   Time between events (latency)
  overview           Clock-grid verdict and stopped-stretch summary
  stats              Per-signal transition count and time-in-state
  stuck [VALUE]      Find signals stuck at a constant
  schema             Print --format json envelope schema
  docs / skill       Embedded narrative docs and workflow skill

Common filters and options (consumer subcommands accept these):
  -s PATTERN[%RADIX] Signal filter, repeatable. %d / %b / %h (default %h).
  -t START:END       Time *range* on signal/wave/find/sample/distance/
                     stats. Tokens: bare int / Nc / Nt / Nns/us/ms/ps.
                     Bare int is cycle in sync mode, REJECTED in async.
                     Open-ended OK: '5c:' or ':100ns'.
                     The wrapper also accepts `value -t N` and rewrites
                     it to `--at N` for consistency.
  --at T             Single time *point* — **value subcommand only**
                     (not -t). Same time-token grammar as -t. Use
                     negative points precede the physical origin and
                     exit with code 2.
  --tree             list-only: show scopes only (text output).
  --rle              wave-only: run-length-encode the output. Contiguous
                     columns where every signal repeats collapse to
                     `<val>xN`. Marker columns are preserved.
  --async            Raw timescale timestamps instead of cycle numbers.
  --with-reset       Include reset phase (default: skip until deassert).
  --clock SIG        Clock for cycle grid (default: auto *clk*).
  --sample-mode MODE Sync observation: post-edge (default) or pre-edge.
  --reset SIG        Reset for skip detection (default: auto *rst* or *reset*).
  --limit N          Record/column cap (query default 2000; list 400).
  --format text|json JSON: list/value/find/stats/signal/wave/distance/overview.

find / sample modifiers:
  --include-stopped  Include stopped grid points in literal trigger matches.
  --first            Stop after first match.
  --last             Return only the last match.
  --before N         Implies --last, sets -t :N (bare int — cycle/tick).
  --after N          Implies --first, sets -t N: (bare int — cycle/tick).
  --count            Print only match count.

Time tokens (v0.2 phase 4):
  Bare int (sync only) — cycles.   Nc — cycles.   Nt — simulation ticks.
  Nns / Nus / Nms / Nps — physical time, converted via VCD timescale.
  Async mode REQUIRES a suffix. Run `bwave docs show reference/time-tokens`.

Radix display:
  -s "data_out%d" → decimal output.    -s "sig%b" → binary.
  Values in find/sample/distance use Verilog literals: 'd255, 'hFF,
  'b1010, 8'd255. Bare hex like "FF" is rejected.

JSON envelope (--format json):
  Implemented for list, value, find, stats, signal, wave, distance, overview.
  Shape: {"$schema", "command", "data", "warnings"} — see
  `bwave schema` for the full grammar. Inspect clock_regularity and the
  overlapping clock_stopped ranges before relying on cycle observations.
  - `value` fields on signalValue/findMatch are raw cache
    values (no Verilog prefix).
  - `stats.signals[*].value_hist` keys ARE Verilog literals
    ('hFF, 'd255, ...) matching text-mode radix.
  - `stats.signals[*].time_in_state_ticks` / `_ns` make the
    duration unit explicit (older `time_in_state` field was
    in ticks but the name didn't say so).

Virtual signals (Verilog-subset, on wave/find/sample/distance/value):
  --virtual "name = expr"   1-bit boolean over existing signals.
    Bitwise & | ^ ~ ; logical && || ! ; compare == != > >= < <= .
    Bit slicing: sig[7:4] (MSB:LSB) or sig[7] (single bit / unpacked).
    Values: Verilog literals only ('d255 / 'hFF / 'b1010 / 8'd255).
    Signal-to-signal comparisons require matching widths.
    Parens required to mix operators: (*a & *b) | *c.
    Examples:
      --virtual "hsk = *valid & *ready"
      --virtual "hi = *counter > 'd127"
      --virtual "msb = *data[15]"

Named markers (set via 'bwave markers'):
  Marker names resolve anywhere a cycle number is accepted in the
  wrapper layer: -t error_start:dma_done, --before checkpoint,
  diff A B. In wave mode they appear as labels above the cycle header.
  The native --marker annotation option itself is wave-only.

Sync vs async:
  Default sync mode samples at the rising clock edge and reports
  post-edge state (what FFs drive after capturing). Matches GTKWave /
  Vivado convention. Async mode reports every transition with raw
  VCD timestamps; bare-int time arguments are rejected — use a suffix.

Examples:
  # signal tree by pattern
  bwave @dut list -s "axi*valid"
  # waveform with decimal radix
  bwave @dut wave -s "top.dut.state" -s "top.dut.data_*%d" -t 100:200
  # cycle-by-cycle trace
  bwave @dut signal -s "*fsm*" -t 0:50
  # first cycle where error asserts
  bwave @dut find top.dut.error 'd1 --first
  # find a decimal value (Verilog literal)
  bwave @dut find top.dut.data 'd255 --first
  # search backwards for the rising edge before cycle 1000
  bwave @dut find top.dut.req_fire rising -t :1000
  # snapshot data signals each time valid fires
  bwave @dut sample top.dut.valid 'd1 -s "top.dut.data_*"
  # waveform between two named markers
  bwave @dut wave -s "*err*" -t error_start:dma_done
  # how long was a block active?
  bwave @dut stats -s "top.dut.active"
  # snapshot in async mode (suffix required)
  bwave @dut value --at 500ns --async
  # JSON envelope
  bwave @dut find top.dut.error rising --format json
"""


def _show_query_help() -> None:
    """Print query subcommand help and exit."""
    _safe_print(_QUERY_HELP_TEXT)
    sys.exit(0)


_TOP_HELP_TEXT = """\
usage:
  bwave register PATH --as ALIAS
  bwave gui [@ALIAS | TRACE_PATH]
  bwave [@ALIAS | TRACE_PATH] COMMAND [ARGS...] [OPTIONS...]

RTL debug helper for simulation traces.
Works with .fst waveform stores and raw .vcd traces. Registering a sim
directory auto-builds an .fst store; a .vcd passed directly must be
converted with `bwave build` before querying (`register --build` does
both in one step).

Setup:
  register           Register a trace file or sim directory as @ALIAS

Viewing:
  gui                Check a waveform selection (no viewer for now)

Trace commands:
  list               Signal tree with bit widths
  signal             Cycle-by-cycle trace
  wave               Horizontal waveform table
  value --at T       Snapshot signals at one time point
  find PAT VALUE     Find cycles where PAT equals VALUE
  sample PAT VALUE   Snapshot signals each time PAT matches VALUE
  diff T1 T2         Compare values between two time points
  distance PAT VAL   Measure time between events
  overview           Clock-grid verdict and stopped-stretch summary
  stats              Per-signal transitions and time-in-state
  stuck [VALUE]      Find signals stuck at a constant

Meta/direct subcommands:
  build              Build an .fst waveform store from VCD input
  markers            Manage named cycle markers on a registered trace
  schema             Print the --format json schema
  docs               Browse embedded docs: topics, search, show
  skill              Print the workflow skill markdown

Common options:
  -s PATTERN[%RADIX] Repeatable signal filter; radix suffix %h/%d/%b
  -t START:END       Time range for signal/wave/find/sample/distance/stats
  --at T             Single time point; value subcommand only
  --async            Use raw VCD timestamps; suffix time tokens required
  --with-reset       Include reset phase
  --clock PAT        Override clock detection
  --sample-mode MODE Sync observation: post-edge (default) or pre-edge
  --include-stopped  find/sample literal matches include stopped cycles
  --format text|json JSON: list, value, find, stats, signal, wave, distance, overview

Help:
  bwave skill                  How to use these commands (workflows, pitfalls)
  bwave <subcommand> --help    Authoritative Rust/clap flags
  bwave --version              Booley version this wrapper ships with

Examples:
  bwave register util/sim/work/my_config --as dut
  bwave @dut list -s "*state*"
  bwave @dut wave -s "top.dut.state" -t 100:200
  bwave @dut value --at 500 -s "top.dut.*"
"""


def version_line() -> str:
    """The one line `bwave --version` prints.

    Names the *wrapper's* provenance (the Booley distribution), since the whole
    point of asking is to tell the Python wrapper apart from a Rust `bwave`
    binary that may be shadowing it on PATH.
    """
    from booley import __version__

    return f"bwave (booley {__version__})"


def _show_top_help() -> None:
    """Print the real wrapper entrypoints instead of argparse's stale subset."""
    _safe_print(_TOP_HELP_TEXT)
    sys.exit(0)


def _safe_print(text: str) -> None:
    """Print help text even on legacy Windows code pages."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoded = text.encode(sys.stdout.encoding or "utf-8", errors="replace")
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(encoded + os.linesep.encode())
        else:
            print(encoded.decode(sys.stdout.encoding or "utf-8", errors="replace"))


def _build_parser() -> argparse.ArgumentParser:
    """Build the bwave CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="bwave",
        description="RTL debug helper: query waveform stores (.fst format)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Wrapper commands:
  register   Register a trace file/dir under an alias (for use as @ALIAS)
  markers    Manage named markers (cycles) on a registered trace
  gui        Check a waveform selection (no viewer for now)
  query      Query a trace (default if first arg isn't a wrapper command)

Query subcommands (forwarded to the bwave binary — see `bwave query --help`):
  Trace-consuming:  list  signal  wave  value  find  sample  diff
                    distance  overview  stats  stuck  build
  Meta (no trace):  schema  docs  skill

Run 'bwave query --help' for the full query reference, or
'bwave <subcommand> --help' for per-subcommand flag lists (forwarded
to the Rust binary — gives the authoritative flag surface).

Quick start:
  bwave register util/sim/work/my_config --as dut
  bwave @dut wave -s "top.dut.state" -t 100:200
  bwave @dut value --at 500 -s "top.dut.*"
""",
    )

    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=version_line(),
        help="Print the Booley version this bwave wrapper ships with",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_reg = sub.add_parser(
        "register",
        help="Register a trace file or sim directory under an alias",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_REGISTER_EPILOG,
    )
    p_reg.add_argument(
        "sim_dir", metavar="PATH", help="Trace file (.fst/.vcd) or sim output directory"
    )
    p_reg.add_argument(
        "--as", dest="alias", required=True, help="Alias name for use with @ALIAS in query"
    )
    p_reg.add_argument(
        "--build",
        action="store_true",
        help="If PATH resolves to a raw .vcd, build the .fst store beside it "
        "and register that (otherwise a raw .vcd is refused)",
    )

    p_markers = sub.add_parser(
        "markers",
        help="Manage named timepoints for a trace alias",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_MARKERS_EPILOG,
    )
    p_markers.add_argument(
        "extra", nargs=argparse.REMAINDER, help="[@ALIAS] set|list|delete [NAME] [CYCLE]"
    )

    p_gui = sub.add_parser(
        "gui",
        help="Check a waveform selection (no viewer for now)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_GUI_EPILOG,
    )
    p_gui.add_argument(
        "target",
        metavar="@ALIAS|TRACE_PATH",
        nargs="?",
        help="Registered alias or trace file (default: latest session trace)",
    )
    p_gui.add_argument(
        "-s",
        "--signals",
        action="append",
        default=[],
        metavar="GLOB[%RADIX][@COLOR]",
        help="Select top-level signals matching GLOB; optional %%b/%%h/%%d radix and "
        "@red/@blue/@green color (repeatable)",
    )
    p_gui.add_argument(
        "-g",
        "--group",
        dest="groups",
        action="append",
        default=[],
        metavar="NAME=GLOB[%RADIX][@COLOR]",
        help="Select GLOB as a named signal group (repeatable; "
        "repeat NAME to add patterns; optional %%b/%%h/%%d and @red/@blue/@green)",
    )
    p_gui.add_argument(
        "-t",
        "--time",
        metavar="START:END",
        help="Time range; same tokens as query -t (cycles, Nc/Nt/Nns..., markers); "
        "either end may be omitted",
    )
    p_gui.add_argument(
        "--cursor",
        metavar="T",
        help="Time T to check (same time tokens); an error when past the end of the trace",
    )
    p_gui.add_argument(
        "--append",
        action="store_true",
        help="Treat the selection as an addition to an existing one (no clock row is added)",
    )
    p_gui.add_argument(
        "--no-clock",
        action="store_true",
        help="Do not add the trace's clock to the selection",
    )
    p_gui.add_argument(
        "--max-signals",
        type=int,
        metavar="N",
        help=f"Cap on combined --signals/--group expansion (default {_GUI_SIGNAL_CAP})",
    )

    p_query = sub.add_parser(
        "query", help="Query signals, waveforms, statistics (default if omitted)"
    )
    p_query.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="[@ALIAS | TRACE_PATH] [bwave args...]. "
        "Accepts mode flags or query subcommands. "
        "Omit to use last registered trace.",
    )
    return parser


_REGISTER_EPILOG = """
Accepts a .fst/.vcd file directly, or a sim directory
(finds .fst preferred, .vcd fallback). A sim directory with only a trace.vcd
is auto-converted to .fst. A raw .vcd that stays a raw .vcd is REFUSED
(non-zero exit): queries need a built store, so an alias bound to a VCD
could never answer one. Build it first with `bwave build <vcd> -o <out>.fst`,
or pass --build to have register do it for you.

Examples:
  bwave register trace.fst --as dut
  bwave register util/sim/work/my_config --as dut
  bwave build waveform.vcd -o waveform.fst   # convert a raw VCD first
  bwave register waveform.fst --as dut
  bwave register waveform.vcd --as dut --build   # ... or in one step
  bwave @dut --wave -s "*state*" -t 0:100
"""

_MARKERS_EPILOG = """
Actions:
  set NAME CYCLE     Create or update a named timepoint
  list               Show all markers sorted by cycle
  delete NAME        Remove a marker

Marker names need a non-digit character and cannot be a time token (5c, 10ns)
or contain ':'.
Markers persist across queries in the session file.

Once set, markers resolve anywhere a cycle number is accepted:
  -t error_start:dma_done    --before checkpoint    --diff A B
In --wave mode, markers appear as labels above the cycle header.

Examples:
  bwave markers @dut set error_start 1042
  bwave markers @dut set dma_done 5678
  bwave markers @dut list
  bwave @dut --wave -s "*err*" -t error_start:dma_done
  bwave markers @dut delete error_start
"""

_GUI_EPILOG = """
Booley has no waveform viewer for now: `gui` resolves its target and its
selection, reports any error in them, and then exits non-zero with that
message. To read values, use `signal`/`find`/`value`/`stats`.

The target resolves exactly like `query`: `@ALIAS` uses a registered
session, an explicit path is used as-is, no argument means the latest
registered trace. A sim directory is accepted like `register` (finds
.fst, falls back to .vcd).

Selection: --signals / --group select signals by glob (GLOB[%RADIX][@COLOR]
with %b, %h or %d and @red, @blue or @green), --time START:END and --cursor T
take the same tokens as `query -t`. A glob that matches nothing is an error
when nothing matches at all and a WARNING otherwise; a selection larger than
--max-signals is an error.
"""

_KNOWN_COMMANDS = ("register", "markers", "gui", "-h", "--help", "--version", "-V")
_HELP_FLAGS = ("-h", "--help")
#: `bwave --version` used to fall through the "not a wrapper command" branch
#: and get forwarded to the Rust binary as a *query*, which answered with the
#: session usage block (SETUP-F-29c). Handled in main() before that branch —
#: and declared on the parser too, so `--help` lists it.
_VERSION_FLAGS = ("--version", "-V")


def _forward_subcommand_help(extra: list[str]) -> None:
    """If `extra` is `<v0.2-subcommand> [tokens...] --help`, run the Rust
    binary directly so the user gets the *subcommand-specific* help text
    (clap output) instead of the wrapper's generic banner. Exits on success;
    returns silently otherwise so the normal query path takes over."""
    if not extra or extra[0] not in _V02_SUBCOMMANDS:
        return
    if not any(h in extra for h in _HELP_FLAGS):
        return
    # Forward verbatim — no trace path injection (clap prints help and exits
    # before touching the BWAVE_FILE positional).
    sys.exit(_run([*_bwave_cmd(), *extra]))


def main() -> None:
    # Runtime-location guard (ADR 0028): bwave works on trace artifacts inside the
    # Sandbox; host-side paths/caches would diverge.
    location_error = runtime_context.container_only_error("bwave")
    if location_error is not None:
        print(location_error, file=sys.stderr)
        raise SystemExit(2)

    if len(sys.argv) == 1 or sys.argv[1] in _HELP_FLAGS:
        _show_top_help()

    if sys.argv[1] in _VERSION_FLAGS:
        _safe_print(version_line())
        raise SystemExit(0)

    # `open` was renamed to `gui`. Say so — otherwise the unknown subcommand
    # falls through to the query translator and dies about a missing pattern.
    if len(sys.argv) > 1 and sys.argv[1] == "open":
        sys.exit(
            "ERROR: `bwave open` is now `bwave gui`.\n"
            f"  Retry as: bwave gui {' '.join(quote_arg(arg) for arg in sys.argv[2:])}".rstrip()
        )

    # Default to query if first arg isn't a known subcommand
    if len(sys.argv) > 1 and sys.argv[1] not in _KNOWN_COMMANDS:
        extra = sys.argv[2:] if sys.argv[1] == "query" else sys.argv[1:]
        # `bwave query --help` (or `-h`) shows the wrapper-level query help
        # banner. Bare `--help` after a v0.2 subcommand name forwards to the
        # Rust binary so callers see the real per-subcommand flag list. Peek
        # past an optional leading @alias — the MCP tool surface always
        # prepends one, but clap doesn't care about aliases.
        sub_idx = 1 if (extra and extra[0].startswith("@")) else 0
        if len(extra) > sub_idx and extra[sub_idx] in _V02_SUBCOMMANDS:
            _forward_subcommand_help(extra[sub_idx:])
        elif sys.argv[1] == "query" and any(h in extra for h in _HELP_FLAGS):
            _show_query_help()
        ns = argparse.Namespace(command="query", extra=extra)
        cmd_query(ns)
        return

    parser = _build_parser()
    args = parser.parse_args()
    {"register": cmd_register, "markers": cmd_markers, "gui": cmd_gui, "query": cmd_query}[
        args.command
    ](args)


if __name__ == "__main__":
    main()
