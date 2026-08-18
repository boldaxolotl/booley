"""Shared heartbeat timer for long-running subprocesses.

Used by the sim run-halves (sim/run_iverilog_sim.py, sim/verilator_run.py),
yosys/run_yosys_syn.py, and harness/booley.py to print periodic progress
updates so the terminal doesn't appear stuck.  Each tick also refreshes the
Session Runtime idle-reaper timestamp, so standalone long-running Booley Flows are
activity just like ticket-driven runs.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Use booley.terminal for output when available (pipeline context);
# fall back to a minimal built-in for standalone usage (sim/syn scripts).
try:
    from booley.harness.colors import dim as _dim
    from booley.harness.terminal import heartbeat_line as _heartbeat_line
except ImportError:
    import sys

    def _dim(text: str) -> str:
        if (
            os.environ.get("NO_COLOR")
            or not hasattr(sys.stdout, "isatty")
            or not sys.stdout.isatty()
        ):
            return text
        return f"\033[2m{text}\033[0m"

    def _heartbeat_line(desc: str, elapsed_str: str, extra: str = "") -> None:
        suffix = f" | {extra}" if extra else ""
        print(_dim(f"  * [{desc}] elapsed: {elapsed_str}{suffix}"), flush=True)


# ---------------------------------------------------------------------------
# Reaper heartbeat (ADR 0018 WS2/WS4, ADR 0028 Decision 11)
# ---------------------------------------------------------------------------

# Epoch-seconds file the idle reaper (booley.docker.reaper) reads via
# ``docker exec`` to decide whether the Session Runtime container is idle.
# Touched by every MCP server (HTTP and stdio) on endpoint activity and by the
# ``booley run`` loop while a ticket is active, so active tickets never read
# as idle even without MCP traffic.
REAPER_HEARTBEAT_PATH = "/tmp/booley_mcp_heartbeat"


def touch_reaper_heartbeat(path: str | None = REAPER_HEARTBEAT_PATH) -> None:
    """Best-effort write of wall-clock epoch seconds for the idle reaper.

    The heartbeat is advisory: any OSError is swallowed so a full disk or a
    read-only ``/tmp`` never breaks the caller (MCP server, ticket runner).
    ``path=None`` disables the touch (used by lifetimes without a heartbeat).
    """
    if not path:
        return
    try:
        with Path(path).open("w", encoding="utf-8") as fh:
            fh.write(f"{time.time():.0f}\n")
    except OSError:
        pass


def fmt_elapsed(secs: float) -> str:
    """Format seconds as human-readable string."""
    m, s = divmod(secs, 60)
    if m >= 60:
        h, m = divmod(int(m), 60)
        return f"{h}h {m}m {s:.0f}s"
    return f"{int(m)}m {s:.1f}s" if m >= 1 else f"{s:.1f}s"


class Heartbeat:
    """Prints elapsed time every `interval` seconds while active.

    Usage:
        hb = Heartbeat("Yosys synthesis", interval=60)
        hb.start()
        subprocess.run(...)  # blocks for a long time
        hb.stop()

    Supports an optional `status_fn` callback that returns a string to
    append to the heartbeat line (e.g. current stage from a checkpoint).
    """

    def __init__(self, desc: str, interval: int = 300, status_fn: Callable | None = None) -> None:
        self._desc = desc
        self._interval = interval
        self._status_fn = status_fn
        self._start: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.environ.get("BOOLEY_NO_HEARTBEAT"):
            return
        touch_reaper_heartbeat()
        self._start = time.monotonic()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            touch_reaper_heartbeat()
            elapsed = time.monotonic() - self._start
            extra = ""
            if self._status_fn:
                try:
                    status = self._status_fn()
                    if status:
                        extra = status
                except (OSError, ValueError, RuntimeError):
                    pass
            _heartbeat_line(self._desc, fmt_elapsed(elapsed), extra)

    def __enter__(self) -> Heartbeat:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
