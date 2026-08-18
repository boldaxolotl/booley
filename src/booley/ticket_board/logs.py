"""Log management: progress tracking, incidents, and retry cleanup."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from booley.timefmt import format_human_datetime

logger = logging.getLogger(__name__)

from .constants import STEP_ORDER
from .helpers import now_iso
from .paths import (
    existing_runtime_file,
    human_log_file,
    legacy_file,
    runtime_file,
    ticket_log_dir,
)

RESET_BOUNDARY_PREFIX = "### Reset Boundary ("

# ---------------------------------------------------------------------------
# progress.json -- runtime execution state (step, steps_completed, etc.)
# ---------------------------------------------------------------------------

# Default values for every runtime field in progress.json
PROGRESS_DEFAULTS = {
    "step": "",
    "steps_completed": [],
    "workspace_intent": "fresh",
    "last_update": "",
    "failed_step": None,
    "error": None,
    "blocked_reason": None,
    "blocked_step": None,
}


def load_progress(logs_dir: str | Path, slug: str) -> dict[str, Any] | None:
    """Load progress.json for a ticket. Returns dict or None if missing.

    None signals the caller to fall back to frontmatter (backward compat).
    When the file exists, merges with PROGRESS_DEFAULTS so newly added
    fields get default values automatically.
    """
    path = existing_runtime_file(logs_dir, slug, "progress.json")
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupted file — fall back to frontmatter like a missing file
            return None
        result = copy.deepcopy(PROGRESS_DEFAULTS)
        result.update(data)
        return result
    return None


def save_progress(logs_dir: str | Path, slug: str, progress: dict[str, Any]) -> None:
    """Save progress.json for a ticket atomically. Creates logs dir if needed."""
    import os
    import tempfile

    path = runtime_file(logs_dir, slug, "progress.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(progress, indent=2) + "\n"
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix="progress")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        Path(tmp_name).replace(path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def progress_default(key: str) -> Any:
    """Return a deep copy of the default value for a runtime field."""
    return copy.deepcopy(PROGRESS_DEFAULTS[key])


def reset_progress(logs_dir: str | Path, slug: str) -> None:
    """Reset progress.json to defaults (for full reset)."""
    save_progress(logs_dir, slug, copy.deepcopy(PROGRESS_DEFAULTS))


def _reset_progress_file(prog_path, target_step, planned_steps):
    """Reset progress.json: truncate steps_completed, clear error/blocked fields."""
    try:
        progress = json.loads(prog_path.read_text(encoding="utf-8"))
        # Boundary: external JSON may decode to any type; we mutate it as a dict.
        if not isinstance(progress, dict):
            logger.warning("Ignoring non-object progress.json at %s", prog_path)
            return
        steps_done = progress.get("steps_completed", [])
        if target_step in STEP_ORDER:
            idx = STEP_ORDER.index(target_step)
            planned = planned_steps or set(STEP_ORDER)
            prereqs = set(STEP_ORDER[:idx])
            keep = [s for s in STEP_ORDER[:idx] if s in planned] + [
                s for s in steps_done if s not in STEP_ORDER and s not in prereqs
            ]
            progress["steps_completed"] = keep
        for key in ("error", "failed_step", "blocked_reason", "blocked_step"):
            progress[key] = None
        progress["last_update"] = now_iso()
        # Atomic write: temp file + rename
        content = json.dumps(progress, indent=2) + "\n"
        tmp_path = prog_path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        try:
            tmp_path.replace(prog_path)
        except PermissionError:
            prog_path.write_text(content, encoding="utf-8")
            tmp_path.unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to reset progress.json: %s", e)


def clear_from_step(
    logs_dir: str | Path, slug: str, target_step: str, *, planned_steps: set[str] | None = None
) -> None:
    """Reset progress and status from target step onward.

    NOT locked -- callers must hold the per-ticket lock (or otherwise
    guarantee exclusivity) before calling this.
    """
    log_dir = ticket_log_dir(logs_dir, slug)
    if not log_dir.exists():
        return

    # 1. Remove status.json from both canonical and legacy locations.
    for status_path in (
        runtime_file(logs_dir, slug, "status.json"),
        legacy_file(logs_dir, slug, "status.json"),
    ):
        if status_path.exists():
            try:
                status_path.unlink()
            except OSError as e:
                logger.warning("Failed to remove status.json for %s: %s", slug, e)

    # 2. Reset progress.json
    prog_path = runtime_file(logs_dir, slug, "progress.json")
    if not prog_path.exists():
        legacy_prog = log_dir / "progress.json"
        if legacy_prog.exists():
            prog_path = legacy_prog
    if prog_path.exists():
        _reset_progress_file(prog_path, target_step, planned_steps)

    # 3. Append retry banner to harness.log
    try:
        retry_log = human_log_file(logs_dir, slug, "harness.log")
        retry_log.parent.mkdir(parents=True, exist_ok=True)
        with retry_log.open("a", encoding="utf-8") as f:
            f.write(f"\n=== RETRY from {target_step} at {now_iso()} ===\n")
    except OSError:
        pass


def append_incident(
    logs_dir: str | Path,
    slug: str,
    incident_type: str,
    step: str,
    description: str,
    resolution: str = "unresolved",
) -> int:
    """Append an incident entry to logs/<slug>/incidents.md.

    NOT locked — the read-count-append sequence is racy under concurrent
    access.  Use TicketIO.locked_append_incident() for safe concurrent
    access, or call this directly only when the per-ticket lock is
    already held by the caller.

    Returns the incident number.
    """
    log_path = ticket_log_dir(logs_dir, slug)
    log_path.mkdir(parents=True, exist_ok=True)
    incidents_file = log_path / "incidents.md"

    # Count existing incidents to determine N
    n = 1
    if incidents_file.exists():
        with incidents_file.open(encoding="utf-8") as f:
            content = f.read()
        n = content.count("## Incident ") + 1

    timestamp = format_human_datetime(now_iso(), seconds=True)
    entry = (
        f"\n## Incident {n}: {incident_type}\n"
        f"**Step:** {step}\n"
        f"**Time:** {timestamp}\n"
        f"**Description:** {description}\n"
        f"**Resolution:** {resolution}\n"
    )

    with incidents_file.open("a", encoding="utf-8") as f:
        if n == 1:
            f.write("# Incidents\n")
        f.write(entry)

    return n
