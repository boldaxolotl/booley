"""Display-event scan helpers for MCP-endpoint run ownership.

Historically this module also implemented the blocking concurrent-run guard:
scan ``display.jsonl`` for unmatched ``endpoint_start`` events and refuse to
start while another endpoint looked live.
ADR 0028 deleted that path — admission now lives in the shared slot store
(``booley.runtime.job_slots``), claimed by every MCP endpoint process in ``McpTool.main`` — and
``endpoint_start``/``endpoint_end`` events remain **bookkeeping only**: they drive the
Console and telemetry, never admission.

What survives here is the event-stream parsing used by that bookkeeping
(display reconciliation, status rendering, tests). Never imports ``base`` —
one-way dependency.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _as_pid(value: Any) -> int | None:
    """Coerce a display.jsonl-sourced pid to int, or None if unusable.

    Event records are external JSON: ``pid`` may be absent, ``null``, a numeric
    string, or a bool (``true``/``false`` decode to ``bool``, an ``int``
    subclass we must reject). Validate here so PID consumers only ever see a
    real int downstream.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _scan_endpoint_events(
    lines: list[str],
) -> tuple[dict[str, list[tuple[str, int | None]]], dict[str, str]]:
    """Parse display.jsonl lines into unmatched starts and last-end timestamps."""
    unmatched: dict[str, list[tuple[str, int | None]]] = {}
    last_end_ts: dict[str, str] = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        endpoint = ev.get("endpoint", "")
        if not endpoint:
            continue
        if ev.get("type") == "endpoint_start":
            unmatched.setdefault(endpoint, []).append(
                (ev.get("timestamp", ""), _as_pid(ev.get("pid")))
            )
        elif ev.get("type") == "endpoint_end":
            endpoint_unmatched = unmatched.get(endpoint, [])
            if endpoint_unmatched:
                endpoint_unmatched.pop()
            last_end_ts[endpoint] = ev.get("timestamp", "")
    return unmatched, last_end_ts
