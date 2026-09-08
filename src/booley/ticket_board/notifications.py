"""Push notifications via ntfy.sh."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from booley.core.boundary import as_dict, as_str_list
from booley.runtime.timefmt import format_human_datetime

from .paths import existing_runtime_file


def _load_notifications_config() -> dict:
    """Load the [notifications] section from booley.toml."""
    from booley.runtime.project_dir import resolve_project_dir

    try:
        project = resolve_project_dir()
        current = project / "booley.toml"
        toml_path = current if current.exists() else project / "pipeline.toml"
        with toml_path.open("rb") as file:
            config = tomllib.load(file)
        return as_dict(config.get("notifications")) or {}
    except (OSError, ValueError):
        return {}


def _read_ntfy_topic() -> str:
    """Read ntfy_topic from booley.toml [notifications] section."""
    topic = _load_notifications_config().get("ntfy_topic", "")
    return topic.strip() if isinstance(topic, str) else ""


def is_event_enabled(event: str) -> bool:
    """Check if a notification event is enabled in booley.toml.

    Events are enabled by listing them in ``notifications.events``.
    If the key is absent, all events are enabled (backwards-compatible default).
    """
    cfg = _load_notifications_config()
    events = cfg.get("events")
    if events is None:
        return True
    return event in as_str_list(events)


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
    with contextlib.suppress(OSError, ValueError):
        subprocess.Popen(
            [
                "curl",
                "-s",
                "--connect-timeout",
                "5",
                "--max-time",
                "15",
                "-H",
                f"Title: {safe_title}",
                "-H",
                f"Priority: {priority}",
                "-d",
                safe_body,
                f"https://ntfy.sh/{topic}",
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
    """Summarize open findings and accepted review waivers, or return None.

    Prefers the new ``pending`` field (open, blocking items) and falls back to
    the legacy ``issue_list`` for tickets predating the pending/resolved split.
    """
    open_count = 0
    waived_count = 0
    for crit in criteria.values():
        detail = crit.get("detail") if isinstance(crit, dict) else None
        if not isinstance(detail, dict):
            continue
        issues = detail.get("pending") or detail.get("issue_list")
        if isinstance(issues, list) and issues:
            open_count += len(issues)
        resolved = detail.get("resolved")
        if isinstance(resolved, list):
            waived_count += sum(
                1
                for finding in resolved
                if isinstance(finding, dict)
                and finding.get("status") in {"waived", "impasse_deferred"}
            )
    parts = []
    if open_count:
        parts.append(f"{open_count} issues found")
    if waived_count:
        parts.append(f"{waived_count} review waivers")
    return ", ".join(parts) or None


def ntfy_review_digest(logs_dir: str | Path, slug: str) -> str:
    """Build an advisory digest; malformed artifacts must never fail handoff."""
    try:
        return _review_digest(logs_dir, slug)
    except (OSError, ValueError, TypeError, OverflowError):
        return ""


def _review_digest(logs_dir: str | Path, slug: str) -> str:
    import json

    state_path = existing_runtime_file(logs_dir, slug, "booley_state.json")
    if not state_path.exists():
        return ""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
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
    except (ValueError, OSError):
        prep = {}
    if isinstance(prep, dict) and prep.get("status") in ("ready", "failed"):
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


def notify_rate_limit(rate_limit_type: str | None, sleep_s: float, resets_at: int | None) -> None:
    """Fire-and-forget ntfy notification for rate limit sleep."""
    if not is_event_enabled("rate_limit"):
        return
    reset_str = (
        format_human_datetime(datetime.fromtimestamp(resets_at, tz=UTC))
        if resets_at
        else "unknown"
    )
    ntfy_send(
        title=f"Harness rate-limited ({rate_limit_type or 'unknown'})",
        body=f"Sleeping {sleep_s / 60:.0f}min until {reset_str}",
        priority="3",
    )
