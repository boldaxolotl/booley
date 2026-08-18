"""Unified terminal output for the harness.

All user-facing print output flows through this module so formatting,
color handling, and future features (log mirroring, rate limiting) have
a single choke point.

Thread safety: _output_lock serialises all print calls so concurrent
writers (asyncio callback thread + DisplayWatcher thread) never interleave.

Log mirroring: open_log(path) starts writing a plain-text (ANSI-stripped)
copy of every terminal line to *path*.  close_log() flushes and closes it.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from booley.ticket_board.helpers import fmt_duration

from .colors import (
    _ANSI_RE,
    accent,
    bold_accent,
    bold_fg256,
    bold_red,
    chrome,
    dim,
    fg256,
    green,
    yellow,
)

_output_lock = threading.Lock()
_console_active: bool = False
_console_app: object | None = None
_stdout_available: bool = True


def set_console_active(active: bool, app: object | None = None) -> None:
    """Enable/disable Console mode — suppresses stdout when active."""
    global _console_active, _console_app
    _console_active = active
    _console_app = app if active else None


def get_console_app() -> object | None:
    """Return the active Console app, or None."""
    return _console_app


# -- per-endpoint colors (banner_bold, pipe) ----------------------------------
# Palette B: Jewel Tones (256-color)
# All verification specialists (reviewer / coverage / mutation / debugger)
# share Lavender — they're all "judging existing work" roles.
_orange = (bold_fg256(208), fg256(208))
_mint = (bold_fg256(49), fg256(49))
_lavender = (bold_fg256(141), fg256(141))
_dodgerblue = (bold_fg256(39), fg256(39))
_mauve = (bold_fg256(145), fg256(145))

_ENDPOINT_COLORS: dict[str, tuple[Callable[[str], str], Callable[[str], str]]] = {
    "tb_coder": _mint,
    "reviewer": _lavender,
    "coverage_analyst": _lavender,
    "mutation_tester": _lavender,
    "sim": _dodgerblue,
    "lint": _dodgerblue,
    "synth": _dodgerblue,
}
_DEFAULT_COLORS: tuple[Callable[[str], str], Callable[[str], str]] = _mauve

# Tracks the pipe color of the currently-open endpoint box (set by endpoint_box_open,
# cleared by endpoint_box_close). Protected by _output_lock.
_current_pipe_color: Callable[[str], str] = _mauve[1]

# -- run.log mirroring --------------------------------------------------------
_log_file: io.TextIOWrapper | None = None


def open_log(path: Path) -> None:
    """Start mirroring terminal output to *path* (plain text, no ANSI)."""
    global _log_file
    with _output_lock:
        if _log_file is not None:
            _log_file.close()
        _log_file = path.open("a", encoding="utf-8")


def close_log() -> None:
    """Flush and close the run log file."""
    global _log_file
    with _output_lock:
        if _log_file is not None:
            _log_file.close()
            _log_file = None


def _emit(*args: str, flush: bool = False) -> None:
    """Print to stdout and, if open, mirror ANSI-stripped text to run.log.

    Must be called while holding *_output_lock*.
    When Console is active, stdout is suppressed but run.log still written.
    """
    global _stdout_available
    text = " ".join(args) if args else ""
    if not _console_active and _stdout_available:
        try:
            print(text, flush=flush)
        except UnicodeEncodeError:
            try:
                print(
                    text.encode("utf-8", errors="replace").decode("ascii", errors="replace"),
                    flush=flush,
                )
            except OSError:
                _stdout_available = False
        except OSError:
            # A parent process may abandon the stdout pipe while the harness is
            # still finalizing. Terminal output is best-effort; never let it
            # change ticket disposition after endpoints and criteria have succeeded.
            _stdout_available = False
    if _log_file is not None:
        _log_file.write(_ANSI_RE.sub("", text) + "\n")
        if flush:
            _log_file.flush()


def raw(text: str = "", *, flush: bool = False) -> None:
    """Print an arbitrary line and mirror it to run.log."""
    with _output_lock:
        _emit(text, flush=flush)


def ts() -> str:
    """Current timestamp HH:MM:SS."""
    return datetime.now().strftime("%H:%M:%S")


def _ts_prefix() -> str:
    """Dim-styled timestamp prefix used by most output lines."""
    return dim(f"[{ts()}]")


# ---------------------------------------------------------------------------
# General status output (used by loop runner)
# ---------------------------------------------------------------------------


def status(msg: str) -> None:
    """Print a timestamped status line: ``[HH:MM:SS] msg``."""
    with _output_lock:
        _emit(f"{_ts_prefix()} {msg}")


def status_indent(msg: str) -> None:
    """Print an indented timestamped status line: ``  [HH:MM:SS] msg``."""
    with _output_lock:
        _emit(f"  {_ts_prefix()} {msg}")


# ---------------------------------------------------------------------------
# Step summary output
# ---------------------------------------------------------------------------


def step_start_header(step: str) -> None:
    """Print a step start bar: ``  ┌─ step-name ───┐``."""
    bar = "─" * max(1, 48 - len(step))
    with _output_lock:
        _emit(f"  {bold_accent('┌─')} {bold_accent(step)} {bold_accent(bar + '─')}")


def step_end_header(step: str) -> None:
    """Print a step end bar: ``  └─ step-name ───┘``."""
    bar = "─" * max(1, 48 - len(step))
    with _output_lock:
        _emit(f"  {bold_accent('└─')} {bold_accent(step)} {bold_accent(bar + '─')}")


def step_line(line: str) -> None:
    """Print a step detail line: ``  │ line``."""
    with _output_lock:
        _emit(f"  {accent('│')} {line}")


def step_footer() -> None:
    """Flush after a step summary block."""
    with _output_lock:
        _emit(flush=True)


# ---------------------------------------------------------------------------
# Heartbeat output
# ---------------------------------------------------------------------------


def heartbeat_line(desc: str, elapsed_str: str, extra: str = "") -> None:
    """Print a heartbeat progress line."""
    suffix = f" | {extra}" if extra else ""
    with _output_lock:
        _emit(dim(f"  * [{desc}] elapsed: {elapsed_str}{suffix}"), flush=True)


# ---------------------------------------------------------------------------
# Endpoint-box output (driven by display.jsonl from MCP endpoints)
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    0: green("[PASS]"),
    1: yellow("[FAIL]"),
    2: bold_red("[ERR]"),
}


def endpoint_box_open(endpoint_name: str, target: str | None = None) -> None:
    """Print endpoint start bar: ``    ┌─ endpoint_name [target] ───┐``."""
    global _current_pipe_color
    label = f"{endpoint_name} [{target}]" if target else endpoint_name
    bar = "─" * max(1, 44 - len(label))
    banner, pipe = _ENDPOINT_COLORS.get(endpoint_name, _DEFAULT_COLORS)
    with _output_lock:
        _current_pipe_color = pipe
        _emit(f"    {banner('┌─')} {banner(label)} {banner(bar + '─')}")


def endpoint_box_close(
    endpoint_name: str,
    target: str | None,
    *,
    exit_code: int,
    duration_s: float,
    cost_usd: float = 0.0,
    display_lines: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    """Print display_lines, status icon with duration/cost, and closing bar."""
    global _current_pipe_color
    label = f"{endpoint_name} [{target}]" if target else endpoint_name
    bar = "─" * max(1, 44 - len(label))
    banner, pipe = _ENDPOINT_COLORS.get(endpoint_name, _DEFAULT_COLORS)
    # A successful dry-run verified nothing — label it so it can't be misread
    # as a green verdict next to a real [FAIL] (A-6).
    if dry_run and exit_code == 0:
        icon = accent("[DRY-RUN]")
    else:
        icon = _STATUS_ICONS.get(exit_code, bold_red("[ERR]"))
    dur = f"{duration_s:.0f}s" if duration_s < 60 else f"{duration_s / 60:.1f}m"
    cost = f" ${cost_usd:.2f}" if cost_usd else ""
    with _output_lock:
        if display_lines:
            for dl in display_lines:
                _emit(f"    {pipe('│')} {dl}")
        _emit(f"    {pipe('│')} {icon} {dim(dur)}{dim(cost)}")
        _emit(f"    {banner('└─')} {banner(label)} {banner(bar + '─')}")
        _emit(flush=True)
        _current_pipe_color = _mauve[1]


# ---------------------------------------------------------------------------
# Endpoint progress (live status lines inside an open endpoint box)
# ---------------------------------------------------------------------------


def endpoint_progress_line(line: str, *, dimmed: bool = True) -> None:
    """Print a progress line inside an open endpoint box: ``    │ line``."""
    rendered = dim(line) if dimmed else line
    with _output_lock:
        _emit(f"    {_current_pipe_color('│')} {rendered}")


# ---------------------------------------------------------------------------
# Criteria summary block (end-of-run)
# ---------------------------------------------------------------------------


def criteria_summary(lines: list[str], total_line: str) -> None:
    """Print the criteria summary table with header/footer bars."""
    label = "criteria"
    bar = "─" * max(1, 48 - len(label))
    with _output_lock:
        _emit(f"  {bold_accent('┌─')} {bold_accent(label)} {bold_accent(bar + '─')}")
        for ln in lines:
            _emit(f"  {accent('│')} {ln}")
        _emit(f"  {accent('│')}")
        _emit(f"  {accent('│')} {total_line}")
        _emit(f"  {bold_accent('└─')} {bold_accent(label)} {bold_accent(bar + '─')}")
        _emit(flush=True)


# ---------------------------------------------------------------------------
# Run totals (cost + time after final verdict)
# ---------------------------------------------------------------------------


def run_totals(elapsed_s: float, cost_usd: float) -> None:
    """Print total elapsed time and cost after the run."""
    dur = fmt_duration(elapsed_s)
    cost = f" · ${cost_usd:.2f}" if cost_usd else ""
    with _output_lock:
        _emit(f"  {chrome(f'total: {dur}{cost}')}")
        _emit(flush=True)


# ---------------------------------------------------------------------------
# Agent text streaming (dimmed developer reasoning)
# ---------------------------------------------------------------------------


def agent_text(text: str) -> None:
    """Print dimmed agent reasoning lines with ``  . `` prefix."""
    with _output_lock:
        for line in text.splitlines():
            _emit(dim(f"  . {line}"))


# ---------------------------------------------------------------------------
# Endpoint heartbeat (long-running endpoint elapsed time)
# ---------------------------------------------------------------------------


def endpoint_heartbeat(endpoint_name: str, elapsed_s: float) -> None:
    """Print dimmed endpoint heartbeat: ``    * endpoint: Xm00s elapsed``."""
    mins = int(elapsed_s // 60)
    secs = int(elapsed_s % 60)
    with _output_lock:
        _emit(dim(f"    * {endpoint_name}: {mins}m{secs:02d}s elapsed"), flush=True)
