"""``[jobs]`` cap parsing."""

import logging

import pytest

from booley.config.jobs import parse_caps


def test_parse_caps_accepts_memory_budget_without_unknown_key_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="booley.config.jobs"):
        parse_caps({"jobs": {"memory_budget": "24g", "max_heavy": 2}})

    assert "unknown key" not in caplog.text
    assert "memory_budget" not in caplog.text
