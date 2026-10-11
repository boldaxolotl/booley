"""UTC log timestamps use each record's creation instant."""

import logging
from datetime import UTC, datetime

from booley.runtime.timefmt import UtcLogFormatter


def test_log_formatters_use_record_instant_as_utc_rfc3339():
    created = datetime(2026, 9, 25, 13, 7, 17, tzinfo=UTC).timestamp()
    record = logging.LogRecord("test", logging.INFO, "", 0, "event", (), None)
    record.created = created

    full = UtcLogFormatter("%(asctime)s %(levelname)s %(message)s").format(record)
    assert full.startswith("2026-09-25T13:07:17Z INFO event")
