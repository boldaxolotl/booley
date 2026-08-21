"""Execution logic: step computation, ticket classification, resume detection."""

from __future__ import annotations

import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .constants import (
    FLOW_STEP_MAP,
    PRIORITY_ORDER,
    STEP_ORDER,
    VALID_TYPES,
)
from .helpers import (
    compute_done_slugs,
    detect_tickets_dir,
    is_pid_alive,
    parse_iso,
    read_lock_pid,
    slug_from_file,
)
from .paths import existing_runtime_file

logger = logging.getLogger(__name__)

# Sort sentinel for tickets with no `created` stamp: sorts after any real ISO
# timestamp so an undated ticket never jumps the queue ahead of dated ones.
_UNDATED_SORTS_LAST = "9999"


def _load_project_toml(project_root: Path) -> dict:
    """Read booley.toml from the project data directory.

    Returns an empty dict if the file is missing or unreadable. A present
    but malformed/unreadable file is logged (not just silently treated as
    absent) — a typo'd booley.toml should be visibly wrong, not a quiet
    fallback to defaults that confuses whoever hits the resulting behavior.
    """
    import tomllib

    candidates = [
        project_root / ".booley_project" / "booley.toml",
        project_root / ".booley" / "project" / "booley.toml",
    ]
    for path in candidates:
        if path.exists():
            try:
                with path.open("rb") as f:
                    return tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError) as e:
                logger.warning("Failed to read %s: %s", path, e)
    return {}


def tb_source_prefixes(project_root: Path) -> list[str]:
    """Return testbench path prefixes from the ``.core`` tb-tagged filesets.

    Defaults to ["tb/"] when no ``.core`` is authored. Used by the validator to
    check that test keys use the project's testbench directories (ADR 0026
    follow-through — the ``.core`` ``tags:[tb]`` partition, not ``[sources.*]``).
    """
    try:
        from booley.fusesoc.fusesoc_registry import source_dirs_from_core

        _rtl, tb_dirs, _incl = source_dirs_from_core(project_root)
    except Exception:  # noqa: BLE001 — registry unavailable; default boundary
        return ["tb/"]
    # A ``.core`` tb entry is either a *directory* (files under ``tb/`` collapse
    # to the ``tb/`` parent) or, in a flat single-file repo (ADR 0026), the
    # testbench *file* itself (``testbench.v`` at the root). A directory needs a
    # trailing ``/`` so it prefix-matches ``tb/foo.sv``; a file must match
    # exactly, since ``testbench.v/`` never prefixes the TB path ``testbench.v``.
    # (shared_infra.source_dir_prefixes is the diff-classification cousin; this
    # stays forward-slash-only because criteria TB paths are POSIX basenames.)
    prefixes: list[str] = []
    for d in tb_dirs:
        if not isinstance(d, str):
            continue
        stem = d.strip().rstrip("/\\")
        if not stem:
            continue
        prefixes.append(stem if (project_root / stem).is_file() else f"{stem}/")
    return prefixes or ["tb/"]


def disabled_flows(project_root: Path) -> set[str]:
    """Return Flow names explicitly disabled in the project's booley.toml."""
    data = _load_project_toml(project_root)
    flows_section = data.get("flows", {})
    # Boundary: external TOML — a mistyped [flows] table means "nothing disabled".
    if not isinstance(flows_section, dict):
        return set()
    disabled: set[str] = set()
    for flow_key in FLOW_STEP_MAP:
        flow_cfg = flows_section.get(flow_key, {})
        if isinstance(flow_cfg, dict) and flow_cfg.get("enabled", True) is False:
            disabled.add(flow_key)
    return disabled


def disabled_flow_steps(project_root: Path) -> set[str]:
    """Return stages gated by explicitly disabled Flows."""
    return {FLOW_STEP_MAP[flow] for flow in disabled_flows(project_root)}


def next_from_planned(planned_steps: list[str], current_step: str) -> str | None:
    """Return next step after current_step in a step list, or None."""
    try:
        idx = planned_steps.index(current_step)
    except ValueError:
        return None
    return planned_steps[idx + 1] if idx + 1 < len(planned_steps) else None


def select_mutation_config(targets: list[str]) -> str | None:
    """Pick the first target for RTL mutation testing (target order is the run config's)."""
    return targets[0] if targets else None


def _check_orphan(t, now, orphan_threshold_min, logs_dir):
    """Check if a running ticket is orphaned via PID liveness then timestamp.

    Returns True (and sets t["_orphan_age_min"]) if orphaned, False otherwise.
    """
    slug = slug_from_file(t.get("file", ""))
    lock_path = existing_runtime_file(logs_dir, slug, "ticket.lock") if logs_dir and slug else None

    # Primary check: if lock file has a PID, check if that process is alive
    if lock_path:
        # Retry to handle races where another process is mid-write to the lock
        pid = None
        for _ in range(3):
            pid = read_lock_pid(lock_path)
            if pid is not None:
                break
            time.sleep(0.1)
        if pid is not None:
            if not is_pid_alive(pid):
                # Process is dead — definitive orphan
                last_update = t.get("last_update", "")
                try:
                    lu_dt = parse_iso(last_update)
                    t["_orphan_age_min"] = int((now - lu_dt).total_seconds() / 60)
                except (ValueError, AttributeError, TypeError):
                    t["_orphan_age_min"] = -1
                return True
            return False  # process alive — not orphaned

    # Fallback: no PID in lock file (legacy) — use timestamp threshold
    last_update = t.get("last_update", "")
    try:
        lu_dt = parse_iso(last_update)
        age_min = (now - lu_dt).total_seconds() / 60
        if age_min > orphan_threshold_min:
            t["_orphan_age_min"] = int(age_min)
            return True
    except (ValueError, AttributeError, TypeError):
        # Can't parse timestamp — treat as orphaned to be safe
        t["_orphan_age_min"] = -1
        return True
    return False


def classify_tickets(
    tickets: list[dict[str, Any]], orphan_threshold_min: int = 30, logs_dir: Any = None
) -> dict[str, list[dict[str, Any]]]:
    """Partition tickets into executable, active, blocked, waiting-on-deps, review, and orphaned lists.

    Waiting tickets live in the waiting/ directory. Queued tickets (in queue/)
    are executable only if all their dependencies are done.

    Orphaned tickets are 'running' tickets detected by:
      1. PID liveness — if the lock file contains a PID and that process is dead
      2. Timestamp fallback — last_update older than orphan_threshold_min minutes
    """
    if logs_dir is None:
        logs_dir = detect_tickets_dir() / "logs"
    executable, active, blocked, waiting, review, orphaned = [], [], [], [], [], []
    now = datetime.now(UTC)

    done_slugs = compute_done_slugs(tickets)

    for t in tickets:
        status = t.get("status", "")
        if status == "blocked":
            blocked.append(t)
        elif status == "review":
            review.append(t)
        elif status == "waiting":
            waiting.append(t)
        elif status == "queued":
            deps = t.get("dependencies", [])
            if deps and not all(d in done_slugs for d in deps):
                waiting.append(t)  # deps unsatisfied — treat as waiting
            else:
                executable.append(t)
        elif status == "running":
            if _check_orphan(t, now, orphan_threshold_min, logs_dir):
                orphaned.append(t)
            else:
                active.append(t)
    # Sort executable: by priority (high > medium > low), then in-progress
    # first, then oldest-created first. The creation tiebreak makes the order
    # total and stable, so the runner claims the highest-priority ticket rather
    # than whichever one the scanner happened to yield first (F-51). Tickets
    # with no `created` stamp sort last rather than first.
    executable.sort(
        key=lambda t: (
            PRIORITY_ORDER.get(t.get("priority", "medium"), 1),
            0 if t.get("steps_completed") else 1,
            t.get("created") or _UNDATED_SORTS_LAST,
        )
    )
    return {
        "executable": executable,
        "active": active,
        "blocked": blocked,
        "waiting": waiting,
        "review": review,
        "orphaned": orphaned,
    }


def _resolve_ticket_type(entry):
    """Resolve ticket_type from entry, warning if missing/invalid. Returns validated type."""
    t = entry.get("type")
    if not t or t not in VALID_TYPES:
        print(
            f"Warning: ticket type '{t}' missing or invalid, defaulting to 'feature'",
            file=sys.stderr,
        )
        return "feature"
    return t


def resume_detect(entry: dict[str, Any]) -> dict[str, Any]:
    """Determine resume action from a board entry.

    Returns dict with keys: action, stage, clear_fields, feature_branch.
    action is one of: fresh, continue, resume_blocked, complete.
    """
    status = entry.get("status", "")
    steps_done = entry.get("steps_completed", [])
    _resolve_ticket_type(entry)  # validates, side-effect: warning

    result = {"feature_branch": entry.get("feature_branch", "")}

    if status == "running" and steps_done:
        next_step = next_from_planned(STEP_ORDER, steps_done[-1])
        action = "complete" if next_step is None else "continue"
        stage = "done" if next_step is None else next_step
        result.update(action=action, stage=stage, clear_fields=[])
        return result

    if status == "blocked":
        result.update(
            action="resume_blocked",
            stage=entry.get("blocked_step", ""),
            clear_fields=["blocked_reason", "blocked_step"],
        )
        return result

    if status == "queued" and steps_done:
        if entry.get("blocked_step"):
            result.update(
                action="resume_blocked",
                stage=entry["blocked_step"],
                clear_fields=["blocked_reason", "blocked_step"],
            )
        else:
            next_step = next_from_planned(STEP_ORDER, steps_done[-1])
            result.update(action="continue", stage=next_step, clear_fields=[])
        return result

    result.update(action="fresh", stage="setup", clear_fields=[])
    return result
