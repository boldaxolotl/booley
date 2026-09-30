"""Log management: incidents and retry cleanup."""

from __future__ import annotations

import logging
from pathlib import Path

from booley.runtime.timefmt import format_human_datetime

logger = logging.getLogger(__name__)

from .board_layout import read_state_record, write_state_record
from .constants import STEP_ORDER
from .helpers import now_iso
from .paths import (
    human_log_file,
    legacy_file,
    runtime_file,
    ticket_log_dir,
)

RESET_BOUNDARY_PREFIX = "### Reset Boundary ("


def _reset_runtime_from_step(
    logs_dir: str | Path, slug: str, target_step: str, planned_steps: set[str] | None
) -> None:
    """Truncate steps_completed before *target_step* and clear error/blocked fields.

    Updates the Ticket's state record in place, keeping its state. A draft has
    no record and nothing to reset; a corrupt record raises
    :class:`~booley.ticket_board.board_layout.StateRecordError`.
    """
    tickets_dir = Path(logs_dir).parent
    record = read_state_record(tickets_dir, slug)
    if record is None:
        return
    progress = record.progress()
    steps_done = progress["steps_completed"]
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
    write_state_record(tickets_dir, slug, record.with_runtime(progress))


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

    # 2. Reset the runtime fields of the Ticket's state record
    _reset_runtime_from_step(logs_dir, slug, target_step, planned_steps)

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
