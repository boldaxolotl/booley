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
