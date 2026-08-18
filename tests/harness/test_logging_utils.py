"""Tests for logging_utils.py."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from booley.harness.logging_utils import (
    get_current_step,
    now_iso,
    set_current_step,
    setup_file_logging,
    teardown_file_logging,
)

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
