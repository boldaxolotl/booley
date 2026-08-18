"""Push notifications via ntfy.sh."""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from .paths import existing_runtime_file


def _load_notifications_config() -> dict:
    """Load the [notifications] section from booley.toml."""
    try:
        import tomllib
    except ImportError:
        return {}
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from booley.runtime.project_dir import resolve_project_dir

    _proj = resolve_project_dir()
    _new = _proj / "booley.toml"
    toml_path = _new if _new.exists() else _proj / "pipeline.toml"
    if not toml_path.exists():
        return {}
    try:
        with toml_path.open("rb") as f:
            cfg = tomllib.load(f)
        return cfg.get("notifications", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _read_ntfy_topic() -> str:
    """Read ntfy_topic from booley.toml [notifications] section."""
    return _load_notifications_config().get("ntfy_topic", "").strip()


def is_event_enabled(event: str) -> bool:
    """Check if a notification event is enabled in booley.toml.

    Events are enabled by listing them in ``notifications.events``.
    If the key is absent, all events are enabled (backwards-compatible default).
    """
    cfg = _load_notifications_config()
    events = cfg.get("events")
    if events is None:
        return True
    return event in events


def ntfy_send(title: str, body: str, priority: str = "3") -> None:
    """Send push notification via ntfy.sh. Silent no-op if unconfigured.

    Honors the ``NTFY_DISABLE`` env var so test suites (which call ticket_board
    operations via subprocess and thus can't mock-patch) can suppress real
    notifications without setting an empty ``ntfy_topic``.
    """
    if os.environ.get("NTFY_DISABLE"):
        return
    topic = _read_ntfy_topic()
    if not topic:
        return
    # Sanitize header values: strip newlines/carriage returns to prevent
    # HTTP header injection via attacker-controlled ticket summaries.
    safe_title = title.replace("\r", " ").replace("\n", " ")
    safe_body = body.replace("\r", " ").replace("\n", " ")
    # Fire-and-forget: don't block the ticket run
    with contextlib.suppress(OSError):
        subprocess.Popen(
            [
                "curl",
                "-s",
                "-H",
                f"Title: {safe_title}",
                "-H",
                f"Priority: {priority}",
                "-d",
                safe_body,
                f"ntfy.sh/{topic}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _digest_sim_part(criteria: dict) -> str | None:
    """First sim pass/fail summary found in criteria detail, or None."""
    for crit in criteria.values():
        detail = crit.get("detail") if isinstance(crit, dict) else None
        if not isinstance(detail, dict):
            continue
        total = detail.get("tests_total")
        if isinstance(total, (int, float)):
            passed = detail.get("tests_passed")
            passed = passed if isinstance(passed, (int, float)) else 0
            return f"{passed}P/{total - passed}F sim"
    return None


def _digest_issues_part(criteria: dict) -> str | None:
    """First open-issues count found in criteria detail, or None.

    Prefers the new ``pending`` field (open, blocking items) and falls back to
    the legacy ``issue_list`` for tickets predating the pending/resolved split.
    """
    for crit in criteria.values():
        detail = crit.get("detail") if isinstance(crit, dict) else None
        if not isinstance(detail, dict):
            continue
        issues = detail.get("pending") or detail.get("issue_list")
        if isinstance(issues, list) and issues:
            return f"{len(issues)} issues found"
    return None


def ntfy_review_digest(logs_dir: str | Path, slug: str) -> str:
    """Build a short digest from booley_state.json for review notifications (~120 chars)."""
    import json

    state_path = existing_runtime_file(logs_dir, slug, "booley_state.json")
    if not state_path.exists():
        return ""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ""
    # Boundary: external JSON may decode to any type; the digest needs a dict.
    if not isinstance(state, dict):
        return ""

    criteria = state.get("criteria", {})
    if not isinstance(criteria, dict):
        criteria = {}
    parts = [p for p in (_digest_sim_part(criteria), _digest_issues_part(criteria)) if p]

    prep_path = existing_runtime_file(logs_dir, slug, "triage-prep/manifest.json")
    try:
        prep = json.loads(prep_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        prep = {}
    if isinstance(prep, dict) and prep.get("status") in {"ready", "failed"}:
        parts.append("triage ready" if prep["status"] == "ready" else "triage report failed")

    # Total cost from timeline (skip non-list timelines / non-numeric costs).
    timeline = state.get("timeline", [])
    if isinstance(timeline, list):
        total_cost = sum(
            e["cost_usd"]
            for e in timeline
            if isinstance(e, dict) and isinstance(e.get("cost_usd"), (int, float))
        )
        if total_cost > 0:
            parts.append(f"${total_cost:.2f}")

    return " | ".join(parts) if parts else ""
