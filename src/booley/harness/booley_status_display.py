#!/usr/bin/env python3
"""Ticket-status reading + heartbeat formatting for the Booley harness.

WHAT: Reads the most recent per-ticket status/checkpoint file and formats a
short, human-readable heartbeat line (e.g. "Still debugging | my-slug |
sim-debug-loop | 2m ago"). Also hosts ``_run_with_heartbeat``, the thin
subprocess wrapper that prints those lines on an interval while the harness
child runs.

WHY: This "read latest status file -> format for display" responsibility is
distinct from the CLI argument parsing and persistent run-loop in
``booley.py`` (SRP / principle 8). Keeping it here lets the run-loop depend on
a small, self-contained display surface.

CONSUMERS: ``booley.harness.booley`` re-exports every public-ish name below for
backward compatibility (tests reference them as ``booley`` module attributes,
and the run-loop calls ``_run_with_heartbeat``).
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from booley.runtime.timefmt import parse_timestamp
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.paths import existing_ticket_runtime_file

logger = logging.getLogger("booley")


_STEP_GERUNDS: dict[str, str] = {
    # High-level harness steps
    "parse-validate": "validating",
    "setup": "setting up",
    "developer": "running developer",
    "post-processing": "post-processing",
    "planning": "planning",
    "run-config": "configuring",
    "implementation": "implementing",
    "implementation-tb": "implementing TB",
    "lint-check": "linting",
    "rtl-review-1": "reviewing RTL",
    "tb-review": "reviewing TB",
    "sim-debug-loop": "debugging",
    "rtl-mutation-testing": "mutation testing",
    "rtl-review-final": "final review",
    "post-review-sim": "post-review sim",
    "synthesis": "synthesizing",
    "acceptance-check": "checking acceptance",
    "summary": "summarizing",
    # MCP endpoint names (from display.jsonl during developer)
    "tb_coder": "implementing",
    "reviewer": "reviewing",
    "lint": "linting",
    "sim": "simulating",
    "mutation_tester": "mutation testing",
    "synth": "synthesizing",
    "coverage_analyst": "analyzing coverage",
}


def _active_endpoint_from_display(ticket_logs_dir: Path) -> tuple[str, str | None] | None:
    """Read display.jsonl to find the currently active MCP endpoint (started but not ended).

    Returns ``(endpoint_name, target)`` or ``None``.
    """
    display_path = existing_ticket_runtime_file(ticket_logs_dir, "display.jsonl")
    if not display_path.exists():
        return None
    try:
        open_endpoints: dict[str, str | None] = {}
        last_open: str | None = None
        for raw_line in display_path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            endpoint = event.get("endpoint", "")
            if etype == "endpoint_start" and endpoint:
                open_endpoints[endpoint] = event.get("target")
                last_open = endpoint
            elif etype == "endpoint_end" and endpoint:
                open_endpoints.pop(endpoint, None)
                if last_open == endpoint:
                    last_open = None
        if last_open and last_open in open_endpoints:
            return (last_open, open_endpoints[last_open])
        if open_endpoints:
            first = next(iter(open_endpoints))
            return (first, open_endpoints[first])
    except OSError:
        pass
    return None


def _read_checkpoint_status(project_root: Path) -> str | None:
    """Read the most recent status.json to get a short status like 'Still debugging (2m ago)'."""
    logs_dir = tickets_dir_from_project_root(project_root) / "logs"
    if not logs_dir.exists():
        return None

    best_file = _find_latest_status_file(logs_dir)
    if best_file is None:
        return None

    try:
        data = json.loads(best_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    slug = data.get("slug", best_file.parent.name)
    step = data.get("current_step", "") or data.get("step", "") or "unknown"
    target: str | None = None
    if step == "developer":
        ticket_log_dir = (
            best_file.parent.parent if best_file.parent.name == ".runtime" else best_file.parent
        )
        result = _active_endpoint_from_display(ticket_log_dir)
        if result:
            step, target = result

    # Derive variant from target (e.g. "coder/rtl" → "rtl")
    variant = target.split("/", 1)[1] if target and "/" in target else None
    gerund_key = f"{step}/{variant}" if variant else step
    verb = _STEP_GERUNDS.get(gerund_key, _STEP_GERUNDS.get(step, step))
    step_label = f"{step} ({variant})" if variant else step
    age_str = _format_age(data.get("last_updated", ""))

    parts = [f"Still {verb}", slug, step_label]
    if age_str:
        parts.append(age_str)
    return " | ".join(parts)


def _find_latest_status_file(logs_dir: Path) -> Path | None:
    """Find the most recently modified status.json or checkpoint.json."""
    best_file = None
    best_mtime = 0.0
    for pattern in ("*/.runtime/status.json", "*/status.json", "*/checkpoint.json"):
        for sf in logs_dir.glob(pattern):
            try:
                mtime = sf.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best_file = sf
            except OSError:
                continue
    return best_file


def _format_age(last_updated: str) -> str:
    """Format a timestamp into a human-readable age string."""
    if not last_updated:
        return ""
    try:
        ts = parse_timestamp(last_updated).astimezone(UTC)
        delta = datetime.now(UTC) - ts
        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes}m ago"
        return f"{minutes // 60}h ago"
    except (ValueError, OSError):
        return ""


def _run_with_heartbeat(cmd: list[str], cwd: str, project_root: Path) -> int:
    """Run harness subprocess with periodic heartbeat output.

    Uses Popen so we can poll + print status while the child runs.
    Stdio is inherited so the developer's own logs appear in terminal.
    """
    try:
        from booley.runtime.heartbeat import Heartbeat, touch_reaper_heartbeat
    except ImportError:
        logger.warning("heartbeat module not available -- running without heartbeat")
        # Fallback: run without heartbeat
        proc = subprocess.Popen(cmd, cwd=cwd)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.wait()
        rc = proc.returncode
        return 130 if rc is None or rc == 0xC000013A else rc

    # Imported lazily to avoid an import cycle: booley.py imports this module
    # at load time, so this module must not import booley.py at module scope.
    from booley.harness.booley import HEARTBEAT_INTERVAL

    def status_fn() -> str:
        # Runner-side reaper heartbeat (ADR 0028 Decision 11): a ticket can be
        # busy without MCP tool traffic (agent thinking, git operations), so
        # the run loop itself reports activity while the harness child runs —
        # otherwise the idle reaper could stop the container mid-ticket.
        touch_reaper_heartbeat()
        return _read_checkpoint_status(project_root) or "working..."

    hb = Heartbeat("harness", interval=HEARTBEAT_INTERVAL, status_fn=status_fn)

    # Touch once up front: the first periodic beat is a full interval away,
    # and BOOLEY_NO_HEARTBEAT suppresses the display thread entirely.
    touch_reaper_heartbeat()
    proc = subprocess.Popen(cmd, cwd=cwd)
    hb.start()
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.wait()
    finally:
        hb.stop()

    # On Windows, Ctrl+C sends CTRL_C_EVENT to the whole console group.
    # The child may exit before Python reads its return code, leaving
    # returncode=None.  Normalize to 130 (SIGINT convention).
    # Windows STATUS_CONTROL_C_EXIT (0xC000013A / 3221225786) is the
    # equivalent of Unix SIGINT — normalize it too.
    rc = proc.returncode
    if rc is None or rc == 0xC000013A:
        return 130
    return rc
