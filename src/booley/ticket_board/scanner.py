"""Ticket discovery and scanning — find and enumerate tickets on the board.

Module-level functions for scanning ticket directories, parsing frontmatter,
and enriching entries with runtime state data. Extracted from io.py for
single-responsibility (P8).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .constants import DIR_STATUS_MAP, TICKET_DIRS
from .execution import next_from_planned
from .frontmatter import parse_frontmatter
from .logs import load_progress
from .paths import existing_runtime_file

logger = logging.getLogger(__name__)


def find_ticket_file(tickets_dir: str | Path, slug: str) -> tuple[Path | None, str | None]:
    """Scan all TICKET_DIRS for .md files matching slug by filename stem,
    or by feature_branch field in frontmatter.

    Returns (Path, status_str) or (None, None).
    """
    tickets_dir = Path(tickets_dir)
    # The board prints ticket names as ``<slug>.md`` in VS Code (its terminal
    # only auto-links plain file paths), so users copy that straight into
    # ``board reset``/``show``/etc. Accept it by stripping a trailing ``.md``.
    if slug.endswith(".md"):
        slug = slug[:-3]
    # First pass: exact filename match (fast)
    for d in TICKET_DIRS:
        dir_path = tickets_dir / d
        if not dir_path.is_dir():
            continue
        for md_file in dir_path.glob("*.md"):
            if md_file.stem == slug:
                return md_file, DIR_STATUS_MAP.get(d, d)
    # Second pass: match by feature_branch field in frontmatter
    for d in TICKET_DIRS:
        dir_path = tickets_dir / d
        if not dir_path.is_dir():
            continue
        for md_file in dir_path.glob("*.md"):
            try:
                with md_file.open(encoding="utf-8") as f:
                    text = f.read(4096)  # frontmatter is near the top
                fields, _ = parse_frontmatter(text)
                feature_branch = fields.get("feature_branch", "")
                if feature_branch and feature_branch == slug:
                    return md_file, DIR_STATUS_MAP.get(d, d)
            except OSError:
                continue
    return None, None


def _load_state_data(state_path: Path) -> dict[str, Any] | None:
    """Load and cache the full booley_state.json. Returns None if unavailable."""
    if not state_path.exists():
        return None
    try:
        with state_path.open(encoding="utf-8") as sf:
            data = json.load(sf)
    except (json.JSONDecodeError, OSError, TypeError) as e:
        logger.warning("Failed to load state from %s: %s", state_path, e)
        return None
    # Boundary: external JSON may decode to any type; callers assume a dict.
    if not isinstance(data, dict):
        logger.warning("Ignoring non-object state in %s", state_path)
        return None
    return data


def _load_criteria_summary(state_data: dict[str, Any] | None) -> tuple[int, int] | None:
    """Extract criteria passed/total from already-loaded state data."""
    if state_data is None:
        return None
    criteria = state_data.get("criteria", {})
    # Boundary: external JSON — criteria must be a dict to iterate its values.
    if not isinstance(criteria, dict):
        return None
    # Drop internal `_`-prefixed criteria (currently `_report_submitted`, seeded
    # by the harness, not by the ticket author). They are invisible everywhere
    # else — criteria_acceptance._compute_criteria_stats filters them for the
    # run.log block — so counting them here made `board show` read 5/5 against
    # run.log's 4/4 for the same ticket (fpu F-43).
    real = {k: v for k, v in criteria.items() if not k.startswith("_")}
    total = len(real)
    if total == 0:
        return None
    passed = sum(1 for v in real.values() if (v.get("met") if isinstance(v, dict) else v))
    return passed, total


def _load_timeline_summary(state_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract last endpoint, endpoint count, and last update from timeline."""
    if state_data is None:
        return None
    timeline = state_data.get("timeline", [])
    # Boundary: external JSON — timeline must be a non-empty list of dicts.
    if not isinstance(timeline, list) or not timeline:
        return None
    last_entry = timeline[-1]
    if not isinstance(last_entry, dict):
        return None
    endpoint = (
        last_entry.get("flow") or last_entry.get("mcp_tool") or last_entry.get("agent") or ""
    )
    return {
        "last_endpoint": endpoint,
        "endpoints_run": len(timeline),
        "last_update": last_entry.get("timestamp", state_data.get("last_updated", "")),
    }


def _enrich_from_state(entry: dict[str, Any], logs_dir: Path, slug: str) -> None:
    """Augment a ticket entry with data from booley_state.json (criteria + timeline)."""
    state_data = _load_state_data(existing_runtime_file(logs_dir, slug, "booley_state.json"))

    cr = _load_criteria_summary(state_data)
    if cr is not None:
        entry["criteria_passed"], entry["criteria_total"] = cr

    # Override stale progress.json fields with authoritative timeline data
    tl = _load_timeline_summary(state_data)
    if tl is not None:
        entry["step"] = tl["last_endpoint"]
        entry["steps_completed"] = ["_"] * tl["endpoints_run"]  # count only
        entry["last_update"] = tl["last_update"]


def _derive_step(rt):
    """Derive current step from runtime state (steps_completed or step field)."""
    completed = rt.get("steps_completed", [])
    if completed:
        from .constants import STEP_ORDER as _SO

        return next_from_planned(_SO, completed[-1]) or "done"
    return rt.get("step", "")


def _build_ticket_entry(md_file, d, dir_status, fields, rt):
    """Build a ticket entry dict from parsed frontmatter and runtime state."""
    completed = rt.get("steps_completed", [])
    entry = {
        "file": f"{d}/{md_file.name}",
        "summary": fields.get("summary", md_file.stem),
        "type": fields.get("type", "feature"),
        "status": dir_status,
        "branch": fields.get("branch", ""),
        "feature_branch": fields.get("feature_branch") or md_file.stem,
        "step": _derive_step(rt),
        "steps_completed": completed,
        "created": fields.get("created", ""),
        "last_update": rt.get("last_update", ""),
    }
    for opt_key in (
        "on_success",
        "integration_base",
        "dependencies",
        "priority",
        "scope",
        "criteria",
        "spec",
        "base_sha",
    ):
        if opt_key in fields:
            entry[opt_key] = fields[opt_key]
    for opt_key in ("blocked_reason", "blocked_step", "error", "failed_step"):
        val = rt.get(opt_key)
        if val is not None:
            entry[opt_key] = val
    return entry


def scan_all_tickets(tickets_dir: str | Path) -> list[dict[str, Any]]:
    """Scan all ticket dirs, parse each .md file's frontmatter.

    Runtime fields are read from progress.json when available,
    falling back to frontmatter for backward compatibility.
    """
    tickets_dir = Path(tickets_dir)
    logs_dir = tickets_dir / "logs"
    result = []

    for d in TICKET_DIRS:
        dir_path = tickets_dir / d
        if not dir_path.is_dir():
            continue
        dir_status = DIR_STATUS_MAP.get(d, d)
        for md_file in sorted(dir_path.glob("*.md")):
            try:
                with md_file.open(encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            fields, _ = parse_frontmatter(text)
            progress = load_progress(logs_dir, md_file.stem)
            rt = progress if progress is not None else fields

            entry = _build_ticket_entry(md_file, d, dir_status, fields, rt)
            _enrich_from_state(entry, logs_dir, md_file.stem)
            result.append(entry)

    return result
