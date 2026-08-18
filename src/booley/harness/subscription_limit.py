"""Subscription-limit detection and reset-time parsing.

Inspects recently blocked tickets for provider rate-limit / subscription-limit
indicators and extracts the wait duration from the error text.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from booley.harness.blocking import LIMIT_PATTERNS
from booley.ticket_board.helpers import tickets_dir_from_project_root

logger = logging.getLogger("booley")


# ---------------------------------------------------------------------------
# Subscription limit detection
# ---------------------------------------------------------------------------


def detect_subscription_limit(project_root: Path) -> int:
    """Check recently blocked tickets for subscription limit indicators.

    Returns wait seconds (>0 if limit detected, 0 otherwise).
    """
    tickets_dir = tickets_dir_from_project_root(project_root)
    blocked_dir = tickets_dir / "board" / "blocked"
    if not blocked_dir.exists():
        return 0

    cutoff = time.time() - 120
    for md in blocked_dir.glob("*.md"):
        slug = md.stem
        text = _read_recent_blocked_text(tickets_dir, slug, cutoff)
        if text is None:
            continue
        wait = _check_text_for_limit(text, slug)
        if wait > 0:
            return wait

    return 0


def _read_recent_blocked_text(
    tickets_dir: Path,
    slug: str,
    cutoff: float,
) -> str | None:
    """Read the last section of a blocked.md if recently modified."""
    for name in ("blocked.md", "failure.md"):
        blocked_path = tickets_dir / "logs" / slug / name
        if blocked_path.exists():
            break
    else:
        return None

    try:
        if blocked_path.stat().st_mtime < cutoff:
            return None
    except OSError:
        return None

    try:
        text = blocked_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Only inspect the last entry to avoid false positives
    sections = text.split("\n## ")
    if len(sections) > 1:
        text = "## " + sections[-1]
    return text


def _check_text_for_limit(text: str, slug: str) -> int:
    """Check error text for limit patterns, return wait seconds or 0."""
    for pattern in LIMIT_PATTERNS:
        if pattern.search(text):
            logger.warning(
                "Subscription limit detected in %s (pattern: %s)", slug, pattern.pattern
            )
            wait = _extract_reset_wait(text)
            if wait:
                logger.debug("Parsed reset wait from error text: %ds", wait)
                return wait
            logger.debug("No reset time found, defaulting to 3600s")
            return 3600
    return 0


def _extract_reset_wait(text: str) -> int | None:
    """Try all known provider formats to extract wait seconds from error text.

    Tries in order:
      1. Claude: "resets 8pm (Asia/Tbilisi)"
      2. Codex relative: "try again in 3 hours 2 minutes"
      3. Codex absolute: "try again at Apr 7th, 2026 1:07 AM"
    """
    for parser in (_parse_claude_reset, _parse_codex_relative, _parse_codex_absolute):
        result = parser(text)
        if result is not None:
            return result
    return None


# -- Claude format: "resets 8:00 pm (Asia/Tbilisi)" --

_CLAUDE_RESET_RE = re.compile(
    r"resets\s+(\d{1,2}(?::\d{2})?)\s*(am|pm)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


def _parse_claude_reset(text: str) -> int | None:
    m = _CLAUDE_RESET_RE.search(text)
    if not m:
        return None
    return _time_ampm_tz_to_wait(m.group(1), m.group(2), m.group(3))


def _time_ampm_tz_to_wait(time_str: str, ampm: str, tz_name: str) -> int | None:
    """Parse '8pm (Asia/Tbilisi)' style reset time into wait seconds."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None

    # This parses a provider rate-limit error message, so the numeric parts
    # are untrusted at this boundary. Keep the int()/split() parsing inside
    # the guard so a format change returns None instead of crashing the
    # retry/backoff path (exactly when a crash hurts most).
    try:
        if ":" in time_str:
            hour, minute = int(time_str.split(":", maxsplit=1)[0]), int(time_str.split(":")[1])
        else:
            hour, minute = int(time_str), 0

        if ampm.lower() == "pm" and hour != 12:
            hour += 12
        elif ampm.lower() == "am" and hour == 12:
            hour = 0

        tz = ZoneInfo(tz_name)
        now_utc = datetime.now(UTC)
        now_local = now_utc.astimezone(tz)
        reset_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset_local <= now_local:
            reset_local += timedelta(days=1)
        wait = int((reset_local - now_local).total_seconds())
        return max(60, min(wait, 86400))  # clamp 1min..24h
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


# -- Codex relative: "try again in 3 hours 2 minutes" --

_CODEX_RELATIVE_RE = re.compile(
    r"try again in\s+(.+?)(?:\.|$)",
    re.IGNORECASE,
)
_DURATION_PART_RE = re.compile(r"(\d+)\s*(day|hour|minute|min|second|sec)s?", re.IGNORECASE)


def _parse_codex_relative(text: str) -> int | None:
    m = _CODEX_RELATIVE_RE.search(text)
    if not m:
        return None
    duration_text = m.group(1)
    parts = _DURATION_PART_RE.findall(duration_text)
    if not parts:
        return None

    multipliers = {"day": 86400, "hour": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
    total = 0
    for value, unit in parts:
        total += int(value) * multipliers[unit.lower()]

    if total <= 0:
        return None
    return max(60, min(total, 86400))  # clamp 1min..24h


# -- Codex absolute: "try again at Apr 7th, 2026 1:07 AM" --

_CODEX_ABSOLUTE_RE = re.compile(
    r"try again at\s+"
    r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})\s+"
    r"(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)
_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_codex_absolute(text: str) -> int | None:
    m = _CODEX_ABSOLUTE_RE.search(text)
    if not m:
        return None

    month_str, day, year = m.group(1).lower()[:3], int(m.group(2)), int(m.group(3))
    hour, minute, ampm = int(m.group(4)), int(m.group(5)), m.group(6).lower()

    month = _MONTH_NAMES.get(month_str)
    if month is None:
        return None

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    try:
        # Codex reset times appear to be UTC
        reset_utc = datetime(year, month, day, hour, minute, tzinfo=UTC)
        now_utc = datetime.now(UTC)
        wait = int((reset_utc - now_utc).total_seconds())
        if wait <= 0:
            return None
        return max(60, min(wait, 86400))  # clamp 1min..24h
    except (ValueError, OverflowError):
        return None
