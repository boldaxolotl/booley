"""Standalone heartbeat rendering for non-Harness callers."""

from __future__ import annotations

import os
import sys


def _dim(text: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[2m{text}\033[0m"


def render_heartbeat(description: str, elapsed: str, extra: str = "") -> None:
    """Render one standalone progress line without Harness presentation state."""
    suffix = f" | {extra}" if extra else ""
    print(_dim(f"  * [{description}] elapsed: {elapsed}{suffix}"), flush=True)
