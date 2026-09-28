"""Tests for logging_utils.py."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.harness.logging_utils import (
    TerseFormatter,
    get_current_step,
    now_iso,
    set_current_step,
    setup_file_logging,
    teardown_file_logging,
)
from booley.runtime.timefmt import UtcLogFormatter, parse_timestamp
from booley.ticket_board.io import TicketIO

# ===========================================================================
# Step tracking
# ===========================================================================


class TestStepTracking:
    def test_set_and_get(self):
        set_current_step("planning")
        assert get_current_step() == "planning"

    def test_set_empty_string(self):
        set_current_step("planning")
        set_current_step("")
        assert get_current_step() == ""

    def teardown_method(self):
        set_current_step("")  # reset global state


# ===========================================================================
# now_iso
# ===========================================================================


class TestNowIso:
    def test_format(self):
        ts = now_iso()
        # Should match ISO-8601 UTC format: YYYY-MM-DDTHH:MM:SSZ
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts)

    def test_ends_with_z(self):
        assert now_iso().endswith("Z")


def test_log_formatters_use_record_instant_as_utc_rfc3339():
    created = datetime(2026, 9, 25, 13, 7, 17, tzinfo=UTC).timestamp()
    record = logging.LogRecord("test", logging.INFO, "", 0, "event", (), None)
    record.created = created

    full = UtcLogFormatter("%(asctime)s %(levelname)s %(message)s").format(record)
    terse = TerseFormatter().format(record)

    assert full.startswith("2026-09-25T13:07:17Z INFO event")
    assert terse.startswith("2026-09-25T13:07:17Z event")


def test_harness_warning_handler_uses_utc_formatter():
    from booley.harness import __main__ as harness_main

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    noisy_levels = {
        name: logging.getLogger(name).level
        for name in ("claude_agent_sdk._internal", "httpx", "httpcore")
    }
    try:
        root.handlers.clear()
        harness_main._setup_logging(verbose=False)
        record = logging.LogRecord("harness", logging.WARNING, "", 0, "event", (), None)
        record.created = datetime(2026, 9, 25, 13, 7, 17, tzinfo=UTC).timestamp()

        assert root.handlers[-1].format(record).startswith("2026-09-25T13:07:17Z WARNING")
    finally:
        root.handlers.clear()
        root.handlers.extend(original_handlers)
        root.setLevel(original_level)
        for name, level in noisy_levels.items():
            logging.getLogger(name).setLevel(level)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires POSIX timezone switching")
def test_harness_and_transition_logs_record_same_utc_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+04:00")
    time.tzset()
    tickets_dir = tmp_path / "tickets"
    harness_log = tickets_dir / "logs/demo/human-logs/harness.log"
    try:
        setup_file_logging(harness_log)
        event_logger = logging.getLogger("test_timestamp_alignment")
        event_logger.setLevel(logging.INFO)
        event_logger.info("event")
        teardown_file_logging()
        TicketIO(tickets_dir, project_root=tmp_path).append_transition(
            "demo", "ready", "active", "developer", "event"
        )
    finally:
        teardown_file_logging()
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        time.tzset()

    harness_token = harness_log.read_text(encoding="utf-8").split()[0]
    transition_log = tickets_dir / "logs/demo/human-logs/transitions.log"
    transition_token = transition_log.read_text(encoding="utf-8").split()[0]
    assert harness_token.endswith("Z")
    assert transition_token.endswith("Z")
    difference = abs(
        (parse_timestamp(harness_token) - parse_timestamp(transition_token)).total_seconds()
    )
    assert difference <= 2


# ===========================================================================
# File logging setup/teardown
# ===========================================================================


class TestFileLogging:
    def test_setup_creates_log_file(self, tmp_path: Path):
        setup_file_logging(tmp_path)
        log_path = tmp_path / "harness.log"

        # Write a test message
        logger = logging.getLogger("test_file_logging")
        logger.setLevel(logging.DEBUG)
        logger.info("test message")

        teardown_file_logging()

        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "test message" in content

    def test_teardown_removes_handler(self, tmp_path: Path):
        setup_file_logging(tmp_path)
        root = logging.getLogger()
        handler_count_before = len(root.handlers)

        teardown_file_logging()

        assert len(root.handlers) == handler_count_before - 1

    def test_idempotent_setup(self, tmp_path: Path):
        """Calling setup twice doesn't add duplicate handlers."""
        setup_file_logging(tmp_path)
        root = logging.getLogger()
        count_after_first = len(root.handlers)

        setup_file_logging(tmp_path)
        assert len(root.handlers) == count_after_first

        teardown_file_logging()

    def test_teardown_without_setup(self):
        """teardown when no handler exists -> no-op."""
        teardown_file_logging()  # should not raise
