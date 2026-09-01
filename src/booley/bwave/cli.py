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

from booley.bwave import wcp as bwave_wcp
from booley.bwave.contract import NO_MATCH_MARKER
from booley.bwave.contract import exit_usage as _exit_usage
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

    Resolution lives in booley.runtime.paths so this wrapper, the FIFO streamer and
    coverage_analyst all find the same binary. Never resolve it by bare name:
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
    """Find trace file: prefer .fst (fast tmpdir first), fall back to .vcd.

    Thin wrapper around TraceSession.find() for CLI compatibility.
    """
    from booley.flows.sim.trace_session import TraceSession

    return TraceSession(work_dir).find()


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
    SESSION_FILE,  # noqa: F401  # re-exported for backward compatibility
    _get_markers,
    _load_sessions,
    _register_trace,  # noqa: F401  # re-exported for backward compatibility
    _save_sessions,  # noqa: F401  # re-exported for backward compatibility
    _validate_marker_name,  # noqa: F401  # re-exported for backward compatibility
    cmd_markers,
    cmd_register,
)


def _resolve_markers_in_args(extra: list[str], alias: str) -> None:
    """Resolve marker names in time-related arguments, in-place.

    Replaces marker names with their cycle numbers in:
    -t VALUE, --before VALUE, --after VALUE, --diff V1 V2
    """
    sessions = _load_sessions()
    markers = _get_markers(sessions, alias)
    if not markers:
        return

    def resolve_token(tok: str) -> str:
        """Replace marker name with cycle number if it matches."""
        if tok in markers:
            return str(markers[tok])
        return tok

    def resolve_time_range(val: str) -> str:
        """Resolve markers in a -t style range (START:END)."""
        if ":" in val:
            parts = val.split(":", 1)
            return f"{resolve_token(parts[0])}:{resolve_token(parts[1])}"
        return resolve_token(val)

    if extra:
        sub = extra[0]
        if sub == "diff" and len(extra) >= 3:
            extra[1] = resolve_token(extra[1])
            extra[2] = resolve_token(extra[2])
        elif sub == "value":
            for j, tok in enumerate(extra):
                if tok == "--at" and j + 1 < len(extra):
                    extra[j + 1] = resolve_token(extra[j + 1])
                    break

    i = 0
    while i < len(extra):
        if extra[i] in ("-t", "--time") and i + 1 < len(extra):
            extra[i + 1] = resolve_time_range(extra[i + 1])
            i += 2
        elif extra[i] in (
            "--at",
            "--at-time",
            "--at-cycle",
            "--before",
            "--after",
        ) and i + 1 < len(extra):
            extra[i + 1] = resolve_token(extra[i + 1])
            i += 2
        elif extra[i] == "--diff" and i + 2 < len(extra):
            extra[i + 1] = resolve_token(extra[i + 1])
            extra[i + 2] = resolve_token(extra[i + 2])
            i += 3
        else:
            i += 1


def _inject_marker_flags(extra: list[str], alias: str) -> None:
    """For wave queries (both legacy --wave and v0.2 `wave` subcommand),
    inject --marker NAME CYCLE flags for every registered marker so the
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
        extra.extend(["--marker", name, str(cycle)])


# ---------------------------------------------------------------------------
# Subcommand: query
# ---------------------------------------------------------------------------
def _resolve_trace_from_session(alias: str | None = None) -> str:
    """Resolve trace path from session alias, or exit with helpful error."""
    sessions = _load_sessions()
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
            "  bwave register SIM_DIR --as ALIAS"
        )
    # session comes from the on-disk JSON session file (external input): it may
    # be a non-object or lack the "trace" key if hand-edited or corrupted.
    trace = session.get("trace") if isinstance(session, dict) else None
    if not isinstance(trace, str) or not trace:
        sys.exit(
            f"ERROR: Session for @{key} is malformed (no trace path).\n"
            f"Re-register with: bwave register SIM_DIR --as {key}"
        )
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
    _resolve_markers_in_args(extra, alias)
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

    # --distance PAT VAL [--to PAT VAL] [--stats]
    if "--distance" in extra:
        idx = extra.index("--distance")
        if idx + 2 >= len(extra):
            sys.exit("ERROR: --distance requires PATTERN VALUE")
        pat = extra[idx + 1]
        val = extra[idx + 2]
        del extra[idx : idx + 3]
        # --stats becomes a flag on the distance subcommand, not a separate mode.
        return ["distance", trace_path, pat, val, *extra]

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
    cmd_args = ["list", trace_path, "-s", pattern]
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
_QUERY_LIMIT_DEFAULT = "5000"


def _apply_limit_default(cmd_args: list[str]) -> None:
    """Inject a default --limit if not present; clamp to 10000 if too high."""
    if "--limit" not in cmd_args:
        default = _LIST_LIMIT_DEFAULT if cmd_args[:1] == ["list"] else _QUERY_LIMIT_DEFAULT
        cmd_args.extend(["--limit", default])
        return
    idx = cmd_args.index("--limit")
    if idx + 1 < len(cmd_args):
        try:
            val = int(cmd_args[idx + 1])
            if val > 10000:
                cmd_args[idx + 1] = "10000"
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Subcommand: open (Waveform Viewer, ADR 0040 / ADR 0035)
# ---------------------------------------------------------------------------
_VAPORVIEW_EXTENSION = "lramseyer.vaporview"
# Probe order: `code` first, then `cursor`. shutil.which resolves Windows
# `code.cmd` via PATHEXT, so no platform branching is needed.
_VIEWER_CLIS = ("code", "cursor")
# The editor CLI only hands the file to the VS Code server and exits almost
# immediately — a hung CLI is a failure, not a slow query, so this is
# deliberately much shorter than _run()'s 120 s query timeout.
_VIEWER_LAUNCH_TIMEOUT = 30
# Glob-expansion cap for `gui --signals`. Matches VaporView's own openFile
# maxSignals ceiling; an agent shoveling hundreds of signals into a GUI view
# is a query bug, so overflow is a hard error, not a silent truncation.
_GUI_SIGNAL_CAP = 64

# VaporView's two markers. `gui --time` brackets the range with them, so the
# viewer's status bar reports the span as a delta instead of leaving the human
# to subtract two numbers off the ruler.
_MARKER_MAIN = bwave_wcp.WcpClient.MARKER_MAIN
_MARKER_ALT = bwave_wcp.WcpClient.MARKER_ALT


def _launch_viewer(cli: str, trace: str) -> int:
    """Launch the editor CLI on *trace*; return its exit code (nonzero on any failure)."""
    try:
        result = subprocess.run(
            [cli, trace],
            timeout=_VIEWER_LAUNCH_TIMEOUT,
            check=False,
        )
        return result.returncode
    except (subprocess.TimeoutExpired, OSError):
        return 1


def _extension_missing(cli: str) -> bool:
    """Return True only when *cli* positively reports VaporView is not installed.

    The editor CLI exits 0 when opening a trace even without the VaporView
    extension (the user just gets a raw-binary tab), so a pre-launch
    `--list-extensions` probe is the only way to catch containers built
    before VaporView was added to the devcontainer spec. Probe failures
    (timeout, nonzero exit, OSError) return False — never block a launch
    that might work over a flaky probe.
    """
    try:
        result = subprocess.run(
            [cli, "--list-extensions"],
            capture_output=True,
            text=True,
            timeout=_VIEWER_LAUNCH_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    # Extension IDs are case-insensitive to VS Code.
    installed = {line.strip().lower() for line in result.stdout.splitlines()}
    return _VAPORVIEW_EXTENSION not in installed


def _resolve_gui_target(target: str | None) -> tuple[str, str]:
    """Resolve `open`'s positional to (absolute trace path, alias for markers)."""
    # `query` treats a nonexistent first token as query args and falls back to
    # the session trace; `open` takes no trailing args, so a nonexistent
    # explicit path can only be a typo — error instead of silently opening a
    # different trace.
    if target and not target.startswith("@") and not Path(target).exists():
        sys.exit(f"ERROR: Trace file not found: {target}")
    # Marker names in --time/--cursor resolve against the alias actually used
    # (same rule as query: bare invocations read the default session).
    alias = target[1:] if target and target.startswith("@") else _DEFAULT_ALIAS
    trace_path = _resolve_trace([target] if target else [])
    # Users type `bwave gui SIM_DIR` by analogy with `bwave register
    # SIM_DIR`; passing the directory through would open a new VS Code
    # *folder window*. Reuse register's discovery (.fst preferred, .vcd
    # fallback with auto-conversion) instead.
    if Path(trace_path).is_dir():
        found = find_trace(Path(trace_path))
        if found is None:
            sys.exit(
                f"ERROR: No trace file found in {trace_path}"
                + _trace_diagnostics(Path(trace_path))
            )
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

    The Rust binary writes genuinely useful diagnostics — e.g. `--at-time N is
    beyond simulation range (sim length: 15 cycles)` plus a did-you-mean HINT —
    and then, for that class of failure, exits 0 with no JSON on stdout. The
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
    cmd = [*_bwave_cmd(), *cmd_args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        sys.exit(f"ERROR: bwave query timed out (120s): {' '.join(cmd_args)}")
    if result.returncode != 0:
        _exit_with_inner_error(result, cmd_args, "bwave query failed")
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        _exit_with_inner_error(result, cmd_args, "bwave returned non-JSON output for")
    data = envelope.get("data")
    if not isinstance(data, dict):
        _exit_with_inner_error(result, cmd_args, "bwave JSON envelope has no data object for")
    return data


# Trailing bit range on a bwave signal name: "[7:0]" (vector) or "[3]" (bit).
_BIT_RANGE_RE = re.compile(r"\[(\d+)(?::(\d+))?\]$")


class _OpenSignal(NamedTuple):
    """One requested signal, in the shape VaporView's netlist keys it under.

    bwave names a vector `tb.dut.state[3:0]` — the VCD/FST bit-range token
    joined onto the leaf. VaporView does the opposite: its netlist item is
    named `state` and carries the range as separate msb/lsb fields, so the
    bracketed name matches nothing and `add_signal` drops it (still acking
    `{"success": true}`, which is how this went unnoticed). Send the bare
    path plus msb/lsb, and keep the raw name as *fallback* for the one case
    the string cannot settle: a 1-bit `foo[3]` may equally be an identifier
    with a bracket in it (an unpacked-array element like `mem[0]`), and only
    the viewer's netlist knows which.
    """

    bwave_name: str  # as `bwave list` prints it — what the user recognizes
    instance_path: str  # bare path we send to the viewer
    msb: int | None
    lsb: int | None
    fallback: str | None  # alternate path, tried only if the primary is dropped


def _split_bit_range(name: str, width: int | None) -> _OpenSignal:
    """Split a bwave signal name into the viewer's (instance_path, msb, lsb)."""
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


def _list_query(trace: str, globs: list[str]) -> dict:
    """One `bwave list` envelope — the trace header plus the matched signals.

    This is the cheap query: `list` reads the hierarchy only (~100 ms on a
    440 MB store) where `stats` walks every transition of every signal and can
    run for minutes. Everything `gui` needs before it touches the viewer — the
    signal set, the clock, the end of the trace — rides on this one call.
    """
    flags = [flag for glob in globs for flag in ("-s", glob)]
    return _run_capture(["list", trace, *flags, "--format", "json", "--limit", "10000"])


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
    trace: str, globs: list[str], cap: int
) -> tuple[list[_OpenSignal], dict]:
    """Expand --signals globs to viewer-addressable signals; also return the envelope."""
    for glob in globs:
        if "%" in glob:
            _exit_usage(
                f"ERROR: --signals takes plain globs, no %RADIX suffix (got '{glob}').\n"
                "Display formats are set in the viewer, not at open time."
            )
    data = _list_query(trace, globs)
    prefix = data.get("scope_prefix") or ""
    seen: set[str] = set()
    signals: list[_OpenSignal] = []
    for sig in data.get("signals", []):
        name = sig.get("name")
        if not isinstance(name, str):
            continue
        full = f"{prefix}.{name}" if prefix else name
        if full in seen:
            continue
        seen.add(full)
        width = sig.get("width")
        signals.append(_split_bit_range(full, width if isinstance(width, int) else None))
    if not signals:
        # Same wording class (and exit code) as the Rust total-miss error —
        # NO_MATCH_MARKER keeps the two surfaces greppable the same way.
        pat = ", ".join(f"'{g}'" for g in globs)
        _exit_usage(f"ERROR: {NO_MATCH_MARKER} --signals {pat} in {trace}")
    if len(signals) > cap:
        head = "\n".join(f"  {s.bwave_name}" for s in signals[:5])
        _exit_usage(
            f"ERROR: --signals matches {len(signals)} signals (cap {cap}); first matches:\n"
            f"{head}\n"
            "Narrow the glob, or raise the cap with --max-signals if you really "
            "want them all."
        )
    return signals, data


class _TickResolver:
    """Convert gui's time tokens (cycles/markers/units) to file-timescale ticks.

    The viewer speaks native FST/VCD timescale units; the Rust binary already
    resolves every accepted time token to that unit as `valueData.target_tick`,
    so one tiny `value` query per distinct token is the whole conversion — no
    clock math is duplicated wrapper-side (and the sync/async + marker grammar
    stays single-sourced in Rust).
    """

    def __init__(self, trace: str, alias: str):
        self.trace = trace
        self.markers = _get_markers(_load_sessions(), alias)
        self._total_ticks: int | None = None

    def to_tick(self, token: str) -> int:
        # Markers are stored as cycle numbers; make the unit explicit so the
        # token survives async traces' bare-integer rejection with a clear
        # error pointing at the cycle grammar rather than a mystery number.
        if token in self.markers:
            token = f"{self.markers[token]}c"
        command = ["value", self.trace]
        # Tick and physical-time tokens are absolute file positions. Resolving
        # them in sync mode first quantizes them to a clock cycle and then maps
        # that cycle back through the effective-start offset. Besides shifting
        # exact ranges, that makes absolute tokens unusable on clockless traces.
        # Async mode asks the native engine for the raw file tick directly.
        if token.endswith(("t", "ps", "ns", "us", "ms")):
            command.append("--async")
        command.extend(["--at", token, "--format", "json", "--limit", "1"])
        data = _run_capture(command)
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
            total = _list_query(self.trace, []).get("total_ticks")
            if not isinstance(total, int):
                sys.exit("ERROR: bwave list did not report total_ticks")
            self._total_ticks = total
        return self._total_ticks

    def range_to_ticks(self, spec: str) -> tuple[int, int]:
        """Resolve a START:END --time spec (either end optional)."""
        if ":" not in spec:
            sys.exit(
                f"ERROR: --time takes a range START:END (got '{spec}'). "
                "Either end may be omitted: '500c:' or ':100ns'."
            )
        raw_start, raw_end = spec.split(":", 1)
        start = self.to_tick(raw_start) if raw_start else 0
        end = self.to_tick(raw_end) if raw_end else self.total_ticks()
        if start >= end:
            sys.exit(f"ERROR: empty --time range: start tick {start} >= end tick {end}")
        return start, end


def _uri_is_open(uri: str, documents: list[str]) -> bool:
    """Match our file URI against VaporView's (vscode-normalized) document list."""
    from urllib.parse import unquote

    candidates = {uri, unquote(uri)}
    return any(doc in candidates or unquote(doc) in candidates for doc in documents)


def _wcp_ensure_open(client: bwave_wcp.WcpClient, uri: str) -> bool:
    """Make sure *uri* is loaded in the viewer; return True if freshly opened."""
    if _uri_is_open(uri, client.get_open_documents()):
        return False
    client.open_document(uri)
    client.wait_event("waveform_loaded")
    return True


# VaporView's displayed-signal list round-trips through its webview, so it lags
# the add_signal ack by a frame or two — poll it instead of reading once.
_VIEW_SETTLE_TIMEOUT = 3.0
_VIEW_POLL_INTERVAL = 0.05


def _displayed_paths(client: bwave_wcp.WcpClient, uri: str) -> set[str]:
    """Instance paths the viewer is actually showing for *uri*."""
    ids = client.get_item_list(uri=uri)
    if not ids:
        return set()
    return {
        row["name"]
        for row in client.get_item_info(ids, uri=uri)
        if isinstance(row.get("name"), str)
    }


def _wait_until_displayed(client: bwave_wcp.WcpClient, uri: str, expected: set[str]) -> set[str]:
    """Poll the viewer until it shows every path in *expected*, or time out."""
    shown = _displayed_paths(client, uri)
    if not expected:
        return shown
    deadline = time.monotonic() + _VIEW_SETTLE_TIMEOUT
    while not expected <= shown and time.monotonic() < deadline:
        time.sleep(_VIEW_POLL_INTERVAL)
        shown = _displayed_paths(client, uri)
    return shown


def _new_append_signals(
    client: bwave_wcp.WcpClient, uri: str, signals: list[_OpenSignal]
) -> list[_OpenSignal]:
    """Drop signals already displayed so VaporView does not duplicate rows."""
    if not signals or "get_item_info" not in client.capabilities:
        return signals
    shown = _displayed_paths(client, uri)
    return [
        sig
        for sig in signals
        if sig.instance_path not in shown and (sig.fallback is None or sig.fallback not in shown)
    ]


def _add_signals(
    client: bwave_wcp.WcpClient, uri: str, signals: list[_OpenSignal]
) -> tuple[list[_OpenSignal], list[_OpenSignal]]:
    """Add *signals* to the viewer; return the (accepted, dropped) split.

    `add_signal` acks `{"success": true}` even when the lookup misses, so the
    viewer's own item list is the only truthful answer about what landed. An
    older VaporView without get_item_info leaves us nothing to check against —
    there we trust the ack rather than invent a drop.
    """
    for sig in signals:
        client.add_signal(sig.instance_path, uri=uri, msb=sig.msb, lsb=sig.lsb)
    if not signals or "get_item_info" not in client.capabilities:
        return list(signals), []

    shown = _wait_until_displayed(client, uri, {s.instance_path for s in signals})

    # A miss on a `foo[3]` means the bracket belonged to the identifier after
    # all (an `mem[0]`-style array element, not a bit range): retry those under
    # their raw name before writing them off as dropped.
    retried = [s for s in signals if s.instance_path not in shown and s.fallback is not None]
    if retried:
        for sig in retried:
            client.add_signal(sig.fallback, uri=uri)
        shown |= _wait_until_displayed(client, uri, {s.fallback for s in retried})

    def landed(sig: _OpenSignal) -> bool:
        return sig.instance_path in shown or (sig.fallback is not None and sig.fallback in shown)

    return [s for s in signals if landed(s)], [s for s in signals if not landed(s)]


def _gui_bare(trace_abs: str) -> None:
    """Plain `bwave gui`: WCP when the viewer listens, editor CLI otherwise."""
    client = bwave_wcp.try_connect()
    if client is None:
        _gui_via_editor_cli(trace_abs)
        return
    with client:
        uri = Path(trace_abs).as_uri()
        if _wcp_ensure_open(client, uri):
            print(f"Opened in the Waveform Viewer: {trace_abs}")
        else:
            print(f"Already open in the Waveform Viewer: {trace_abs}")


def _gui_via_editor_cli(trace_abs: str) -> None:
    """Fallback launch path: hand the trace to the injected `code`/`cursor` CLI."""
    # Launch via the resolved path: subprocess won't spawn Windows `code.cmd`
    # from the bare name, and shutil.which already found the real file.
    cli = next((hit for c in _VIEWER_CLIS if (hit := shutil.which(c))), None)
    if cli is None:
        sys.exit(
            f"Could not open a viewer. Trace: {trace_abs}\n"
            "Open it manually, or install the VaporView VS Code extension:\n"
            f"  code --install-extension {_VAPORVIEW_EXTENSION}"
        )
    cli_name = Path(cli).stem
    if _extension_missing(cli):
        sys.exit(
            f"VaporView extension is not installed in this editor. Trace: {trace_abs}\n"
            "Install it (rebuilding the devcontainer also installs it):\n"
            f"  {cli_name} --install-extension {_VAPORVIEW_EXTENSION}"
        )
    if _launch_viewer(cli, trace_abs) != 0:
        sys.exit(
            f"Could not open a viewer. Trace: {trace_abs}\n"
            "Open it manually, or install the VaporView VS Code extension:\n"
            f"  {cli_name} --install-extension {_VAPORVIEW_EXTENSION}"
        )


def _gui_scoped(trace_abs: str, alias: str, args: argparse.Namespace) -> None:
    """Scoped/append view: drive the running viewer over WCP (ADR 0035)."""
    if args.append and not (args.signals or args.time or args.cursor):
        sys.exit("ERROR: --append needs something to add or move: --signals/--time/--cursor")

    # Resolve everything BEFORE touching the viewer, so bad globs/tokens
    # fail without side effects (no half-updated view).
    cap = args.max_signals if args.max_signals is not None else _GUI_SIGNAL_CAP
    signals: list[_OpenSignal] = []
    if args.signals:
        signals, listing = _resolve_signal_globs(trace_abs, args.signals, cap)
        # A waveform without its clock is unreadable — you cannot tell a cycle
        # from a glitch. A new view gets it as row 1; --append is adding to a
        # view that already has one. The clock rides on the listing we just
        # fetched, so this costs no extra query.
        clock = _clock_of(listing) if not args.append and not args.no_clock else None
        if clock is not None and all(s.bwave_name != clock.bwave_name for s in signals):
            signals = [clock, *signals]
    resolver = _TickResolver(trace_abs, alias)
    viewport = resolver.range_to_ticks(args.time) if args.time else None
    cursor_tick = resolver.to_tick(args.cursor) if args.cursor else None

    # VaporView has exactly two markers, and reports their delta in the status
    # bar. A requested range therefore gets bracketed: main=START, alt=END —
    # so the span you asked about is measured on screen, not eyeballed.
    # --cursor moves the main marker off START; END stays put.
    markers: list[tuple[int, int, str]] = []  # (tick, marker_type, label)
    if viewport is not None:
        markers.append(
            (cursor_tick if cursor_tick is not None else viewport[0], _MARKER_MAIN, "START")
        )
        markers.append((viewport[1], _MARKER_ALT, "END"))
    elif cursor_tick is not None:
        markers.append((cursor_tick, _MARKER_MAIN, "cursor"))

    try:
        client = bwave_wcp.try_connect()
    except bwave_wcp.WcpError as exc:
        sys.exit(f"{bwave_wcp.setup_hint()}\n  (probe failure: {exc})")
    if client is None:
        sys.exit(bwave_wcp.setup_hint())

    uri = Path(trace_abs).as_uri()
    lines = [f"Waveform Viewer scoped to {trace_abs}:"]
    dropped: list[_OpenSignal] = []
    try:
        with client:
            freshly_opened = _wcp_ensure_open(client, uri)
            if args.append:
                signals = _new_append_signals(client, uri, signals)
            else:
                # Replace semantics: a scoped view shows EXACTLY the request.
                # This also clears the signals VaporView auto-populates on a
                # fresh open_document (its openFile default loads up to 64).
                displayed = client.get_item_list(uri=uri)
                if displayed:
                    client.remove_items(displayed, uri=uri)
            accepted, dropped = _add_signals(client, uri, signals)
            # Report what the VIEWER took, not what we asked for: add_signal
            # acks a miss as success, and echoing the request is what let
            # every vector signal vanish silently.
            lines.extend(f"  {sig.bwave_name}" for sig in accepted)
            if viewport is not None:
                client.set_viewport(viewport[0], viewport[1], uri=uri)
                lines.append(f"  viewport: {viewport[0]}..{viewport[1]} ticks")
            for tick, marker_type, label in markers:
                client.set_marker(tick, uri=uri, marker_type=marker_type)
                lines.append(f"  {label} marker: {tick} ticks")
    except bwave_wcp.WcpError as exc:
        sys.exit(f"ERROR: Waveform Viewer control failed: {exc}")
    if freshly_opened:
        lines[0] = f"Waveform Viewer opened, scoped to {trace_abs}:"
    # Full hierarchical paths on their own lines: VaporView linkifies them in
    # the integrated terminal, so each line is click-to-reveal for the user.
    print("\n".join(lines))
    if dropped:
        names = "\n".join(f"  {sig.bwave_name}" for sig in dropped)
        print(
            f"WARNING: the Waveform Viewer did not display {len(dropped)} requested "
            f"signal(s):\n{names}\n"
            "  They are absent from its netlist under that path. The trace itself is "
            "fine — query them with `bwave signal`/`find`/`value`.",
            file=sys.stderr,
        )


def cmd_gui(args: argparse.Namespace) -> None:
    """Show the resolved trace in the Waveform Viewer (scoped when asked)."""
    trace_abs, alias = _resolve_gui_target(args.target)
    scoped = bool(args.signals or args.time or args.cursor or args.append)
    if scoped:
        _gui_scoped(trace_abs, alias, args)
    else:
        _gui_bare(trace_abs)


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

This wrapper targets the v0.2 subcommand surface. Legacy v0.1 flags
(--list / --find SIG VAL / --wave / --stats / --at-time / etc.) are
still translated transparently, but new scripts should use the
subcommand form. Run `bwave <subcommand> --help` for authoritative
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
  stats              Per-signal transition count and time-in-state
  stuck [VALUE]      Find signals stuck at a constant
  schema             Print --format json envelope schema
  docs / skill       Embedded narrative docs and workflow skill

Common filters and options (consumer subcommands accept these):
  -s PATTERN[%RADIX] Signal filter, repeatable. %d / %b / %h (default %h).
  -t START:END       Time *range* on list/signal/wave/find/sample/distance/
                     stats. Tokens: bare int / Nc / Nt / Nns/us/ms/ps.
                     Bare int is cycle in sync mode, REJECTED in async.
                     Open-ended OK: '5c:' or ':100ns'.
                     The wrapper also accepts `value -t N` and rewrites
                     it to `--at N` for consistency.
  --at T             Single time *point* — **value subcommand only**
                     (not -t). Same time-token grammar as -t. Use
                     '--at=-5' if you need a negative cycle (clap parses
                     bare '-N' as a flag).
  --tree             list-only: show signals as an indented hierarchy
                     instead of a flat list.
  --rle              wave-only: run-length-encode the output. Contiguous
                     columns where every signal repeats collapse to
                     `<val>xN`. Marker columns are preserved.
  --async            Raw timescale timestamps instead of cycle numbers.
  --with-reset       Include reset phase (default: skip until deassert).
  --clock SIG        Clock for cycle grid (default: auto *clk*).
  --reset SIG        Reset for skip detection (default: auto *rst*).
  --limit N          Output line cap (default 2000).
  --format text|json Wrap JSON output in the canonical envelope.

find / sample modifiers:
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
  Currently implemented for list, value, find, stats.
  Shape: {"$schema", "command", "data", "warnings"} — see
  `bwave schema` for the full grammar.
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

Viewing (human):
  gui                Show a trace in the Waveform Viewer (VaporView in VS Code)

Trace commands:
  list               Signal tree with bit widths
  signal             Cycle-by-cycle trace
  wave               Horizontal waveform table
  value --at T       Snapshot signals at one time point
  find PAT VALUE     Find cycles where PAT equals VALUE
  sample PAT VALUE   Snapshot signals each time PAT matches VALUE
  diff T1 T2         Compare values between two time points
  distance PAT VAL   Measure time between events
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
  --format text|json JSON is implemented for list, value, find, stats

Help:
  bwave skill                  How to use these commands (workflows, pitfalls)
  bwave <subcommand> --help    Authoritative Rust/clap flags
  bwave --version              Booley version this wrapper ships with

Examples:
  bwave register util/sim/work/my_config --as dut
  bwave @dut list -s "*state*"
  bwave @dut wave -s "top.dut.state" -t 100:200
  bwave @dut value --at 500 -s "top.dut.*"
  bwave gui @dut
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
  gui        Show a trace in the Waveform Viewer (VaporView in VS Code)
  query      Query a trace (default if first arg isn't a wrapper command)

Query subcommands (forwarded to the bwave binary — see `bwave query --help`):
  Trace-consuming:  list  signal  wave  value  find  sample  diff
                    distance  stats  stuck  build
  Meta (no trace):  schema  docs  skill

Run 'bwave query --help' for the full query reference, or
'bwave <subcommand> --help' for per-subcommand flag lists (forwarded
to the Rust binary — gives the authoritative flag surface).

Quick start:
  bwave register util/sim/work/my_config --as dut
  bwave @dut wave -s "top.dut.state" -t 100:200
  bwave @dut value --at 500 -s "top.dut.*"
  bwave gui @dut
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
        help="Show a trace in the Waveform Viewer (VaporView in VS Code)",
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
        metavar="GLOB",
        help="Show only signals matching GLOB (repeatable; same globs as query -s, no %%RADIX)",
    )
    p_gui.add_argument(
        "-t",
        "--time",
        metavar="START:END",
        help="Viewport time range; same tokens as query -t (cycles, Nc/Nt/Nns..., markers); "
        "either end may be omitted. Brackets the range with the START and END markers",
    )
    p_gui.add_argument(
        "--cursor",
        metavar="T",
        help="Move the START (main) marker to T (same time tokens); END stays at --time's end",
    )
    p_gui.add_argument(
        "--append",
        action="store_true",
        help="Add to the current view instead of replacing it (no clock row is re-added)",
    )
    p_gui.add_argument(
        "--no-clock",
        action="store_true",
        help="Do not prepend the trace's clock to a new view",
    )
    p_gui.add_argument(
        "--max-signals",
        type=int,
        metavar="N",
        help=f"Cap on --signals glob expansion (default {_GUI_SIGNAL_CAP})",
    )

    p_query = sub.add_parser(
        "query", help="Query signals, waveforms, statistics (default if omitted)"
    )
    p_query.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="[@ALIAS | TRACE_PATH] [bwave args...]. "
        "Accepts legacy --flag form or v0.2 subcommand form. "
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
  bwave gui @dut                             # view it in VaporView (VS Code)
"""

_MARKERS_EPILOG = """
Actions:
  set NAME CYCLE     Create or update a named timepoint
  list               Show all markers sorted by cycle
  delete NAME        Remove a marker

Marker names must contain at least one non-digit character.
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
`gui` is for the HUMAN — it puts a waveform on their screen. It is not a
way to read values; `signal`/`find`/`value`/`stats` answer those, and they
work with no viewer running. Reach for `gui` when the human needs to SEE
something: an FSM walking its states, a handshake, a bus settling.

Resolves the trace exactly like `query`: `@ALIAS` uses a registered
session, an explicit path is used as-is, no argument means the latest
registered trace. A sim directory is accepted like `register` (finds
.fst, falls back to .vcd). The trace opens in the attached VS Code
window via the VaporView extension (lramseyer.vaporview).

Scoped views (ADR 0035): --signals / --time / --cursor / --append drive
the RUNNING viewer over its WCP control server (127.0.0.1, port from
the devcontainer's vaporview.wcp.port; override: BOOLEY_WCP_PORT).
A scoped view replaces what is on screen with the requested signals;
--append adds to it instead. If no WCP server is reachable, scoped views
FAIL with a setup hint — they never silently fall back to the whole trace.

What `gui` does for you, so the view is readable on arrival:

  Clock first. A new view gets the trace's clock as row 1 (the one the
  Rust binary already detected — the same one cycle counts are measured
  against). Nothing else in a waveform means anything without it: you
  cannot tell a cycle from a glitch. --append skips it (the view already
  has one); --no-clock opts out.

  START/END markers. --time START:END does not just move the viewport —
  it drops VaporView's two markers on the ends of the range, so the
  viewer's status bar reports the span as a delta and the human can read
  the duration off the screen instead of subtracting ruler numbers.
  --cursor moves the START (main) marker somewhere else inside the range;
  END stays put.

  Honest reporting. The signal list it prints is read back from the
  viewer, so it is what is actually on screen. Anything the viewer would
  not display is named in a WARNING on stderr — relay that to the human
  rather than claiming the full view; the trace itself is still fine and
  still queryable.

Pick signals like you would draw them on a whiteboard: the strobe that
starts the thing, the state it walks, the counter that proves it, the
strobe that ends it. A view with 40 rows in it is not a view.

A bare `bwave gui` also prefers WCP (focus/open the document), and only
falls back to launching the editor CLI (`code`, then `cursor`) that VS
Code injects into attachment terminals. Environments with neither (a
plain `docker exec` shell), or containers whose editor lacks the
VaporView extension, get the resolved trace path plus an install hint
instead of a viewer.

Examples:
  bwave gui                      # latest session trace, whole view
  bwave gui @dut                 # registered alias
  bwave gui util/sim/work/cfg    # sim directory, like register
  bwave gui @dut -s 'tb.dut.fifo.*' -t 1200c:1400c   # clock + fifo, bracketed
  bwave gui @dut -s 'tb.dut.irq' --append            # add a signal
  bwave gui @dut -t error_start:dma_done --cursor error_start
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
    # Session Runtime; host-side paths/caches would diverge.
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
            "ERROR: `bwave open` is now `bwave gui` (it shows a waveform to the human).\n"
            f"  Retry as: bwave gui {' '.join(sys.argv[2:])}".rstrip()
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
