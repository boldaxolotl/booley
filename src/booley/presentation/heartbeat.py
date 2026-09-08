"""Standalone heartbeat rendering for non-Harness callers."""

from __future__ import annotations

import os
import sys


def _dim(text: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[2m{text}\033[0m"


def format_heartbeat(description: str, elapsed: str, extra: str = "") -> str:
    """Format the shared heartbeat text independently of its output policy."""
    suffix = f" | {extra}" if extra else ""
    return f"  * [{description}] elapsed: {elapsed}{suffix}"


def render_heartbeat(description: str, elapsed: str, extra: str = "") -> None:
    """Render one standalone progress line without Harness presentation state."""
    print(_dim(format_heartbeat(description, elapsed, extra)), flush=True)
