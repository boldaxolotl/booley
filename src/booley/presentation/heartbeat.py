"""Standalone heartbeat rendering for non-Harness callers."""

from __future__ import annotations

import sys
from collections.abc import Callable


def format_heartbeat(description: str, elapsed: str, extra: str = "") -> str:
    """Format the shared heartbeat text independently of its output policy."""
    suffix = f" | {extra}" if extra else ""
    return f"  * [{description}] elapsed: {elapsed}{suffix}"


def render_heartbeat(
    description: str,
    elapsed: str,
    extra: str = "",
    *,
    style: Callable[[str], str] | None = None,
) -> None:
    """Render one standalone progress line without Harness presentation state.

    ``style`` (a shared color helper chosen by the caller) applies only on a
    terminal, so captured flow stdout stays free of escape codes even when
    colors are forced on.
    """
    line = format_heartbeat(description, elapsed, extra)
    print(style(line) if style and sys.stdout.isatty() else line, flush=True)
