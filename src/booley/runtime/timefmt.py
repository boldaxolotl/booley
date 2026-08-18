"""Canonical machine timestamps and human-visible dates for Booley."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MACHINE_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LOCAL_TIMEZONE_ENV = "BOOLEY_LOCAL_TIMEZONE"

_MONTHS = (
    "",
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


def utc_now_rfc3339() -> str:
    """Return the current instant as second-resolution UTC RFC 3339."""
    return datetime.now(UTC).strftime(MACHINE_TIMESTAMP_FORMAT)


def rfc3339_from_epoch(epoch: float) -> str:
    """Return *epoch* as second-resolution UTC RFC 3339."""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime(MACHINE_TIMESTAMP_FORMAT)


def compact_utc_now(*, microseconds: bool = False) -> str:
    """Return a filesystem-safe compact UTC timestamp with an explicit ``Z``."""
    pattern = "%Y%m%dT%H%M%S%fZ" if microseconds else "%Y%m%dT%H%M%SZ"
    return datetime.now(UTC).strftime(pattern)


def parse_timestamp(value: str) -> datetime:
    """Parse ISO-8601/RFC-3339, accepting Booley's legacy timestamp variants."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _fixed_offset(value: str) -> tzinfo | None:
    """Parse ``+HH:MM``/``-HH:MM`` as a fixed-offset timezone."""
    if len(value) != 6 or value[0] not in "+-" or value[3] != ":":
        return None
    try:
        hours = int(value[1:3])
        minutes = int(value[4:6])
    except ValueError:
        return None
    if hours > 23 or minutes > 59:
        return None
    offset = timedelta(hours=hours, minutes=minutes)
    return timezone(offset if value[0] == "+" else -offset)


def local_timezone() -> tzinfo:
    """Return the user's timezone, including inside a Booley container."""
    configured = os.environ.get(LOCAL_TIMEZONE_ENV, "").strip()
    if configured:
        fixed = _fixed_offset(configured)
        if fixed is not None:
            return fixed
        try:
            return ZoneInfo(configured)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or UTC


def _offset_name(value: datetime) -> str:
    """Return a portable ``+HH:MM`` fallback for the host's current offset."""
    offset = value.utcoffset() or timedelta(0)
    total_minutes = round(offset.total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def detect_host_timezone() -> str:
    """Return an IANA timezone name, or a current fixed offset as fallback.

    This runs while generating the Session Runtime spec. The result is passed
    into the container as :data:`LOCAL_TIMEZONE_ENV`, so "local" continues to
    mean the user's timezone instead of the container image's UTC default.
    """
    configured = os.environ.get("TZ", "").strip()
    if configured:
        try:
            ZoneInfo(configured)
        except ZoneInfoNotFoundError:
            pass
        else:
            return configured

    timezone_file = Path("/etc/timezone")
    try:
        candidate = timezone_file.read_text(encoding="utf-8").strip()
    except OSError:
        candidate = ""
    if candidate:
        try:
            ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            pass
        else:
            return candidate

    current = datetime.now().astimezone()
    key = getattr(current.tzinfo, "key", "")
    return key or _offset_name(current)


def format_human_date(value: date | datetime) -> str:
    """Format a date as ``DD MMM YYYY``; aware datetimes first become local."""
    shown = value
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        shown = aware.astimezone(local_timezone())
    return f"{shown.day:02d} {_MONTHS[shown.month]} {shown.year:04d}"


def format_human_datetime(value: str | datetime, *, seconds: bool = False) -> str:
    """Format local time as ``HH:MM[:SS] · DD MMM YYYY``."""
    parsed = parse_timestamp(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    shown = parsed.astimezone(local_timezone())
    clock = f"{shown.hour:02d}:{shown.minute:02d}"
    if seconds:
        clock += f":{shown.second:02d}"
    return f"{clock} · {format_human_date(shown)}"


def format_human_datetime_safe(value: str, *, seconds: bool = False) -> str:
    """Format *value* for display, preserving an unparseable legacy value."""
    try:
        return format_human_datetime(value, seconds=seconds)
    except ValueError:
        return value
