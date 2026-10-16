from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from booley.runtime import timefmt


@pytest.mark.parametrize(
    ("month", "abbreviation"),
    [
        (1, "JAN"),
        (2, "FEB"),
        (3, "MAR"),
        (4, "APR"),
        (5, "MAY"),
        (6, "JUN"),
        (7, "JUL"),
        (8, "AUG"),
        (9, "SEP"),
        (10, "OCT"),
        (11, "NOV"),
        (12, "DEC"),
    ],
)
def test_human_date_has_stable_uppercase_english_month(month, abbreviation):
    assert timefmt.format_human_date(date(2026, month, 3)) == f"03 {abbreviation} 2026"


def test_human_datetime_uses_configured_local_timezone(monkeypatch):
    monkeypatch.setenv(timefmt.LOCAL_TIMEZONE_ENV, "+04:00")
    assert (
        timefmt.format_human_datetime("2026-08-10T09:11:49Z", seconds=True)
        == "13:11:49 · 10 AUG 2026"
    )


def test_human_date_converts_aware_datetime_to_local_date(monkeypatch):
    monkeypatch.setenv(timefmt.LOCAL_TIMEZONE_ENV, "+04:00")
    value = datetime(2026, 8, 9, 22, 30, tzinfo=UTC)
    assert timefmt.format_human_date(value) == "10 AUG 2026"


def test_parser_accepts_z_offset_fractional_and_legacy_naive():
    assert timefmt.parse_timestamp("2026-08-10T09:11:49Z").tzinfo is not None
    assert timefmt.parse_timestamp("2026-08-10T13:11:49.123456+04:00").tzinfo is not None
    assert timefmt.parse_timestamp("2026-08-10T09:11:49").tzinfo == UTC


def test_safe_human_formatter_preserves_unparseable_legacy_value():
    assert timefmt.format_human_datetime_safe("not-a-date") == "not-a-date"


def test_epoch_serialization_is_second_resolution_utc():
    epoch = datetime(2026, 8, 10, 9, 11, 49, tzinfo=UTC).timestamp()
    assert timefmt.rfc3339_from_epoch(epoch) == "2026-08-10T09:11:49Z"


def test_datetime_serialization_can_preserve_microseconds():
    value = datetime(2026, 8, 10, 13, 11, 49, 123456, tzinfo=timezone(timedelta(hours=4)))
    assert timefmt.rfc3339_from_datetime(value, microseconds=True) == (
        "2026-08-10T09:11:49.123456Z"
    )


def test_detect_host_timezone_falls_back_to_offset(monkeypatch):
    fixed = timezone(timedelta(hours=-3, minutes=-30))
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(timefmt.Path, "read_text", lambda *_a, **_kw: "")

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=fixed)

        def astimezone(self, tz=None):
            return self

    monkeypatch.setattr(timefmt, "datetime", FixedDatetime)
    assert timefmt.detect_host_timezone() == "-03:30"


def _display_helper(name):
    helper = getattr(timefmt, name, None)
    assert callable(helper), f"missing shared {name}"
    return helper


def test_adaptive_duration_boundaries():
    duration = _display_helper("format_duration")
    cases = [
        (-1, "0s"),
        (-0.0, "0s"),
        (0, "0s"),
        (0.0001, "0.0s"),
        (0.012, "0.0s"),
        (0.4, "0.4s"),
        (0.96, "1.0s"),
        (0.999, "1.0s"),
        (1, "1s"),
        (1.99, "1s"),
        (59, "59s"),
        (59.999, "59s"),
        (60, "1m 00s"),
        (60.1, "1m 00s"),
        (3599, "59m 59s"),
        (3599.999, "59m 59s"),
        (3600, "1h 00m"),
        (3600.1, "1h 00m"),
        (86399, "23h 59m"),
        (86399.999, "23h 59m"),
        (86400, "1d 00h"),
        (86400.1, "1d 00h"),
        (176461, "2d 01h"),
    ]
    for value, expected in cases:
        assert duration(value) == expected, value


def test_relative_age_boundaries():
    age = _display_helper("format_age")
    for seconds, expected in [
        (-1, "just now"),
        (0, "just now"),
        (0.4, "just now"),
        (4.999, "just now"),
        (5, "5s ago"),
        (5.001, "5s ago"),
        (59, "59s ago"),
        (59.999, "59s ago"),
        (60, "1m ago"),
        (3599, "59m ago"),
        (3600, "1h ago"),
        (86399, "23h ago"),
        (86400, "1d ago"),
        (86400.1, "1d ago"),
    ]:
        assert age(100000 - seconds, now=100000, tz=UTC) == expected, seconds


def test_clock_calendar_buckets():
    clock = _display_helper("format_clock")
    now = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
    for instant, expected in [
        ("2026-10-08T14:44:03Z", "14:44:03"),
        ("2026-10-03T14:44:03Z", "Oct 03 14:44"),
        ("2025-10-03T14:44:03Z", "2025-10-03 14:44"),
    ]:
        assert clock(instant, now=now, tz=UTC) == expected
        parsed = timefmt.parse_timestamp(instant)
        assert clock(parsed, now=now.timestamp(), tz=UTC) == expected
        assert clock(parsed.timestamp(), now=now, tz=UTC) == expected
    assert timefmt.format_human_date(now) == "08 OCT 2026"


def test_display_invalid_and_skew():
    duration = _display_helper("format_duration")
    age = _display_helper("format_age")
    clock = _display_helper("format_clock")
    for value in (None, True, False, "bad", float("nan"), float("inf"), -float("inf"), 10**1000):
        assert duration(value) == "unknown"
        assert age(value, now=1000, tz=UTC) == "unknown"
        assert clock(value, now=1000, tz=UTC) == "unknown"
    for value in (datetime(2026, 10, 8), "2026-10-08T12:00:00", 1e100):
        assert age(value, now=1000, tz=UTC) == "unknown"
        assert clock(value, now=1000, tz=UTC) == "unknown"
    assert age(1001, now=1000, tz=UTC) == "just now"
    assert age(1000, now=True, tz=UTC) == "unknown"
    assert clock(1000, now="bad", tz=UTC) == "unknown"


def test_clock_local_rollover_and_dst(monkeypatch):
    from zoneinfo import ZoneInfo

    clock = _display_helper("format_clock")
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert clock("2026-11-01T05:30:00Z", now=now, tz=tz) == "01:30:00"
    age = _display_helper("format_age")
    assert age("2026-11-01T05:30:00Z", now=now, tz=tz) == "1h ago"
    east = timezone(timedelta(hours=4))
    now = datetime(2026, 10, 3, 21, 0, tzinfo=UTC)
    assert clock("2026-10-03T20:30:00Z", now=now, tz=east) == "00:30:00"
    assert clock("2026-10-03T19:59:00Z", now=now, tz=east) == "Oct 03 23:59"
    assert (
        clock("2025-12-31T22:00:00Z", now=datetime(2026, 1, 1, 1, tzinfo=UTC), tz=east)
        == "02:00:00"
    )
    monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+04:00")
    assert clock("2026-10-03T20:30:00Z", now=now) == "00:30:00"
