"""Canonical machine timestamps and human-visible dates for Booley."""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from booley.core.boundary import as_float

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


class UtcLogFormatter(logging.Formatter):
    """Logging formatter with canonical UTC RFC 3339 event timestamps."""

    def formatTime(  # noqa: N802 — stdlib logging.Formatter defines this camelCase hook
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        del datefmt
        return rfc3339_from_epoch(record.created)


def rfc3339_from_datetime(value: datetime, *, microseconds: bool = False) -> str:
    """Return *value* as UTC RFC 3339, optionally preserving microseconds."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    timespec = "microseconds" if microseconds else "seconds"
    return aware.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


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

    This runs while generating the Sandbox spec. The result is passed
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


def _display_instant(value: object) -> datetime | None:
    """Coerce a presentation input without relaxing persisted timestamp rules."""
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.utcoffset() is not None else None
        epoch = as_float(value)
        return datetime.fromtimestamp(epoch, UTC) if epoch is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def format_clock(value: object, *, now: object = None, tz: tzinfo | None = None) -> str:
    """Render an instant using local calendar buckets; invalid inputs are unknown."""
    instant = _display_instant(value)
    current = datetime.now(UTC) if now is None else _display_instant(now)
    if instant is None or current is None:
        return "unknown"
    zone = local_timezone() if tz is None else tz
    try:
        shown, today = instant.astimezone(zone), current.astimezone(zone)
    except (ValueError, OverflowError):
        return "unknown"
    if shown.date() == today.date():
        return f"{shown.hour:02d}:{shown.minute:02d}:{shown.second:02d}"
    clock = f"{shown.hour:02d}:{shown.minute:02d}"
    if shown.year == today.year:
        return f"{_MONTHS[shown.month].title()} {shown.day:02d} {clock}"
    return f"{shown.year:04d}-{shown.month:02d}-{shown.day:02d} {clock}"


def format_duration(value: object) -> str:
    """Render finite elapsed seconds with adaptive units and floored whole seconds."""
    try:
        seconds = as_float(value)
    except OverflowError:
        seconds = None
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "0s"
    if seconds < 1:
        return f"{seconds:.1f}s"
    whole = int(seconds)
    if seconds < 60:
        result = f"{whole}s"
    elif seconds < 3600:
        result = f"{whole // 60}m {whole % 60:02d}s"
    elif seconds < 86400:
        result = f"{whole // 3600}h {whole % 3600 // 60:02d}m"
    else:
        result = f"{whole // 86400}d {whole % 86400 // 3600:02d}h"
    return result


def format_age(value: object, *, now: object = None, tz: tzinfo | None = None) -> str:
    """Render time since an instant, clamping future instants to just now."""
    del tz  # Ages compare instants in UTC, including across DST folds.
    instant = _display_instant(value)
    current = datetime.now(UTC) if now is None else _display_instant(now)
    if instant is None or current is None:
        return "unknown"
    seconds = max(0, (current - instant).total_seconds())
    if seconds < 5:
        return "just now"
    for unit, divisor in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= divisor:
            return f"{int(seconds) // divisor}{unit} ago"
    raise AssertionError("age bucket must exist")
