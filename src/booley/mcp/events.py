"""Display/event emission helpers for MCP endpoints.

Principle 8 (Single Responsibility): these module-level free functions build
and write the ``display.jsonl`` event stream (endpoint_start/progress/end,
specialist_thinking, criteria_update) plus the cheap console TB-top lookup.
They were extracted from ``base.py`` so the ``McpTool`` ABC keeps only its core
run/report/state responsibility; ``base.py`` re-imports them for use and
backward compatibility.

Runtime dependencies are one-way: this module never imports ``base`` at
runtime (only ``McpToolResult`` under ``TYPE_CHECKING`` for annotations, which are
strings thanks to ``from __future__ import annotations``). At runtime the event
builders only read attributes off the passed objects (duck-typed).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from booley.criteria.state import DevelopmentState
from booley.runtime.timefmt import utc_now_rfc3339
from booley.ticket_board.paths import ticket_runtime_dir

if TYPE_CHECKING:
    from .base import McpToolResult

logger = logging.getLogger(__name__)


def _emit_criteria_update(state: DevelopmentState) -> None:
    """Emit a criteria_update event to display.jsonl after state changes."""
    criteria_snapshot = {}
    for k, e in state.criteria.items():
        if k.startswith("_"):
            continue
        entry_d: dict[str, Any] = {
            "met": e.met,
            "mandatory": e.mandatory,
            "detail": e.detail or {},
            "params": e.params or {},
        }
        if e.stale:
            entry_d["stale"] = True
        if e.ever_met:
            entry_d["ever_met"] = True
        if e.ever_failed:
            entry_d["ever_failed"] = True
        criteria_snapshot[k] = entry_d
    _write_display_event(
        {
            "type": "criteria_update",
            "criteria": criteria_snapshot,
            "timestamp": utc_now_rfc3339(),
        }
    )


def _write_display_event(event: dict) -> None:
    """Append a JSON event to $BOOLEY_RUNTIME_DIR/display.jsonl.

    No-op when BOOLEY_LOGS_DIR is unset (human mode).
    """
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR")
    if not logs_dir:
        return
    try:
        path = ticket_runtime_dir(logs_dir) / "display.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        logger.debug("display.jsonl write failed", exc_info=True)


def _endpoint_start_event(endpoint_name: str, display_target: str | None) -> dict:
    """Build an endpoint_start display event dict."""
    return {
        "type": "endpoint_start",
        "endpoint": endpoint_name,
        "target": display_target,
        "pid": os.getpid(),
        "timestamp": utc_now_rfc3339(),
    }


def _endpoint_progress_event(
    endpoint_name: str,
    line: str,
    *,
    completion: bool = False,
    repeats_at_end: bool = False,
) -> dict:
    """Build an endpoint_progress display event dict."""
    event = {
        "type": "endpoint_progress",
        "endpoint": endpoint_name,
        "line": line,
        "timestamp": utc_now_rfc3339(),
    }
    if completion:
        event["completion"] = True
    if repeats_at_end:
        # A live watcher suppresses this one duplicate at close; a watcher that
        # missed the progress event still gets the self-contained final display.
        event["repeats_at_end"] = True
    return event


def _specialist_thinking_event(text: str) -> dict:
    """Build a specialist_thinking display event dict."""
    return {
        "type": "specialist_thinking",
        "text": text,
        "timestamp": utc_now_rfc3339(),
    }


def _endpoint_end_event(
    endpoint_name: str,
    display_target: str | None,
    result: McpToolResult,
    duration: float,
    dry_run: bool = False,
) -> dict:
    """Build an endpoint_end display event dict."""
    return {
        "type": "endpoint_end",
        "endpoint": endpoint_name,
        "target": display_target,
        "exit_code": result.exit_code,
        # A dry-run's rc=0 verified nothing; the display labels it [DRY-RUN]
        # so it can't be misread as a green verdict next to a real FAIL (A-6).
        "dry_run": dry_run,
        "duration_s": round(duration, 1),
        "cost_usd": round(result.cost_usd, 4) if result.cost_usd else 0.0,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "lines_added": result.lines_added,
        "lines_removed": result.lines_removed,
        "criterion_key": result.criterion_key,
        "criterion_met": result.criterion_met,
        "report_text": result.report_text or "",
        "display_lines": result.display_lines or [],
        "summary": result.summary,
        "timestamp": utc_now_rfc3339(),
    }
