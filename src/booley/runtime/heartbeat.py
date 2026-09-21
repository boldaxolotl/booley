"""Shared heartbeat timer for long-running subprocesses.

Used by simulator run-halves and the harness to print periodic progress updates
so the terminal does not appear stuck. Each tick also refreshes the Sandbox
activity heartbeat, so interactive long-running Booley Flows count as activity
just like ticket-driven runs.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from booley.core.boundary import as_float
from booley.core.file_lock import nonblocking_file_lock

HeartbeatRenderer = Callable[[str, str, str], None]


# ---------------------------------------------------------------------------
# Reaper heartbeat (ADR 0018 WS2/WS4, ADR 0028 Decision 11)
# ---------------------------------------------------------------------------

# Epoch-seconds file the idle reaper (booley.docker.reaper) reads via
# ``docker exec`` to decide whether the Sandbox container is idle.
# Touched by MCP servers, the ``booley run`` loop, and supervised Sandbox
# Attachments while protected work is active.
REAPER_HEARTBEAT_PATH = "/tmp/booley_mcp_heartbeat"


def touch_reaper_heartbeat(path: str | None = REAPER_HEARTBEAT_PATH) -> None:
    """Best-effort atomic publication of wall-clock epoch seconds.

    The heartbeat is advisory: any OSError is swallowed so a full disk or a
    read-only ``/tmp`` never breaks the caller (MCP server, ticket runner).
    ``path=None`` disables the touch (used by lifetimes without a heartbeat).
    """
    if not path:
        return
    heartbeat = Path(path)
    temporary: str | None = None
    try:
        lock_path = heartbeat.with_name(f".{heartbeat.name}.lock")
        with lock_path.open("a+", encoding="ascii") as lock, nonblocking_file_lock(lock):
            previous = _read_valid_epoch(heartbeat)
            candidate = time.time()
            if previous is not None:
                candidate = max(candidate, previous)
            fd, temporary = tempfile.mkstemp(
                dir=heartbeat.parent,
                prefix=f".{heartbeat.name}.",
                suffix=".tmp",
                text=True,
            )
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(f"{candidate:.0f}\n")
            Path(temporary).replace(heartbeat)
            temporary = None
    except OSError:
        if temporary is not None:
            with suppress(OSError):
                Path(temporary).unlink()


def _read_valid_epoch(path: Path) -> float | None:
    try:
        return as_float(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


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
        hb = Heartbeat("Yosys synthesis", render=render_heartbeat, interval=60)
        hb.start()
        subprocess.run(...)  # blocks for a long time
        hb.stop()

    Supports an optional `status_fn` callback that returns a string to
    append to the heartbeat line (e.g. current stage from a checkpoint).
    """

    def __init__(
        self,
        desc: str,
        *,
        render: HeartbeatRenderer,
        interval: float = 300,
        status_fn: Callable[[], str | None] | None = None,
    ) -> None:
        self._desc = desc
        self._render = render
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
            self._render(self._desc, fmt_elapsed(elapsed), extra)

    def __enter__(self) -> Heartbeat:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
