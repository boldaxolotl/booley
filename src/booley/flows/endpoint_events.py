"""Transport-independent endpoint display events and Criteria notifications."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.criteria.presentation import criteria_display_snapshot
from booley.criteria.state import DevelopmentState
from booley.runtime.timefmt import utc_now_rfc3339

if TYPE_CHECKING:
    from booley.runtime.endpoint_execution import EndpointOutcome

logger = logging.getLogger(__name__)

MAX_DISPLAY_EVENT_BYTES = 64 * 1024
_MAX_DISPLAY_TEXT_BYTES = 2 * 1024
_MAX_DISPLAY_LINES = 16
_DISPLAY_TEXT_FIELDS = (
    "endpoint",
    "target",
    "display_label",
    "line",
    "text",
    "report_text",
    "summary",
    "invocation_id",
    "display_scope",
    "timestamp",
    "outcome",
)


def _emit_criteria_update(state: DevelopmentState) -> None:
    """Emit a criteria_update event to display.jsonl after state changes."""
    _write_display_event(
        {
            "type": "criteria_update",
            "criteria": criteria_display_snapshot(state),
            "timestamp": utc_now_rfc3339(),
        }
    )


def _truncate_utf8(value: str, limit: int = _MAX_DISPLAY_TEXT_BYTES) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    suffix = "…"
    budget = limit - len(suffix.encode("utf-8"))
    return raw[:budget].decode("utf-8", errors="ignore") + suffix


def _bounded_criteria(value: object) -> dict[str, dict[str, Any]]:
    """Keep only presentation fields understood by the Ticket header."""
    if not isinstance(value, dict):
        return {}
    criteria: dict[str, dict[str, Any]] = {}
    for raw_key, raw_entry in value.items():
        if not isinstance(raw_entry, dict):
            continue
        entry: dict[str, Any] = {
            key: bool(raw_entry.get(key))
            for key in ("met", "mandatory", "stale", "ever_met", "ever_failed")
            if key in raw_entry
        }
        presentation = raw_entry.get("presentation")
        if isinstance(presentation, dict):
            entry["presentation"] = {
                "label": _truncate_utf8(str(presentation.get("label", ""))),
                "detail": _truncate_utf8(str(presentation.get("detail", ""))),
            }
        criteria[_truncate_utf8(str(raw_key))] = entry
    return criteria


def _display_safe_event(event: dict[str, Any]) -> dict[str, Any]:
    safe = dict(event)
    for key in _DISPLAY_TEXT_FIELDS:
        value = safe.get(key)
        if isinstance(value, str):
            safe[key] = _truncate_utf8(value)
    lines = safe.get("display_lines")
    if isinstance(lines, list):
        safe["display_lines"] = [_truncate_utf8(str(line)) for line in lines[:_MAX_DISPLAY_LINES]]
        if len(lines) > _MAX_DISPLAY_LINES:
            safe["display_lines_truncated"] = len(lines) - _MAX_DISPLAY_LINES
    if safe.get("type") == "criteria_update":
        safe["criteria"] = _bounded_criteria(safe.get("criteria"))
    return safe


def _json_line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"


def _minimal_display_event(event: dict[str, Any]) -> dict[str, Any]:
    """Retain lifecycle identity when an unexpected payload exceeds the budget."""
    scalar_keys = (
        "type",
        "endpoint",
        "target",
        "display_label",
        "invocation_id",
        "display_scope",
        "pid",
        "timestamp",
        "exit_code",
        "outcome",
        "duration_s",
        "dry_run",
    )
    minimal = {key: event[key] for key in scalar_keys if key in event}
    minimal["truncated"] = True
    if event.get("type") != "criteria_update":
        return minimal
    minimal["criteria"] = {}
    for key, entry in _bounded_criteria(event.get("criteria")).items():
        minimal["criteria"][key] = {
            field: value for field, value in entry.items() if field != "presentation"
        }
        if len(_json_line(minimal).encode("utf-8")) > MAX_DISPLAY_EVENT_BYTES:
            minimal["criteria"].pop(key)
            minimal["omitted_criteria"] = True
            break
    return minimal


def _serialize_display_event(event: dict[str, Any]) -> str:
    """Serialize one display record with a hard, newline-inclusive byte ceiling."""
    safe = _display_safe_event(event)
    line = _json_line(safe)
    if len(line.encode("utf-8")) <= MAX_DISPLAY_EVENT_BYTES:
        return line
    line = _json_line(_minimal_display_event(safe))
    if len(line.encode("utf-8")) <= MAX_DISPLAY_EVENT_BYTES:
        return line
    return '{"type":"display_event_dropped","truncated":true}\n'


def _write_display_event(event: dict) -> None:
    """Append a JSON event to $BOOLEY_RUNTIME_DIR/display.jsonl.

    No-op when composition did not supply BOOLEY_RUNTIME_DIR (human mode).
    """
    runtime_dir = os.environ.get("BOOLEY_RUNTIME_DIR")
    if not runtime_dir:
        return
    try:
        path = Path(runtime_dir) / "display.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(_serialize_display_event(event))
    except (OSError, TypeError, ValueError):
        logger.debug("display.jsonl write failed", exc_info=True)


def _event_identity(
    event: dict[str, Any],
    invocation_id: str | None,
    display_scope: str | None,
) -> dict[str, Any]:
    identity = (
        invocation_id
        or os.environ.get("BOOLEY_RUN_ID")
        or os.environ.get("BOOLEY_DISPLAY_INVOCATION_ID")
    )
    if identity:
        event["invocation_id"] = identity
    event["display_scope"] = display_scope or (
        "nested" if os.environ.get("BOOLEY_NESTED_AGENT") == "1" else "developer"
    )
    return event


def _endpoint_start_event(
    endpoint_name: str,
    display_target: str | None,
    *,
    display_label: str | None = None,
    dry_run: bool = False,
    invocation_id: str | None = None,
    display_scope: str | None = None,
) -> dict:
    """Build an endpoint_start display event dict."""
    event = {
        "type": "endpoint_start",
        "endpoint": endpoint_name,
        "target": display_target,
        "pid": os.getpid(),
        "timestamp": utc_now_rfc3339(),
    }
    if display_label:
        event["display_label"] = display_label
    if dry_run:
        event["dry_run"] = True
    return _event_identity(event, invocation_id, display_scope)


def _endpoint_progress_event(
    endpoint_name: str,
    line: str,
    *,
    completion: bool = False,
    repeats_at_end: bool = False,
    invocation_id: str | None = None,
    display_scope: str | None = None,
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
    return _event_identity(event, invocation_id, display_scope)


def _specialist_thinking_event(text: str) -> dict:
    """Build a specialist_thinking display event dict."""
    return _event_identity(
        {
            "type": "specialist_thinking",
            "text": text,
            "timestamp": utc_now_rfc3339(),
        },
        None,
        None,
    )


def _endpoint_end_event(
    endpoint_name: str,
    display_target: str | None,
    result: EndpointOutcome,
    duration: float,
    *,
    display_label: str | None = None,
    dry_run: bool = False,
    invocation_id: str | None = None,
    display_scope: str | None = None,
) -> dict:
    """Build an endpoint_end display event dict."""
    event = {
        "type": "endpoint_end",
        "endpoint": endpoint_name,
        "target": display_target,
        "exit_code": result.exit_code,
        "outcome": "completed" if result.exit_code == 0 else "failed",
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
    if display_label:
        event["display_label"] = display_label
    return _event_identity(event, invocation_id, display_scope)
