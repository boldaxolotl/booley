"""Tests for harness._cost — the usage-log formatter.

Pricing itself is covered in tests/test_pricing.py.
"""

from __future__ import annotations

import pytest

from booley.runtime._cost import format_usage_log


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((10_000, 0, 5_000, 1.5), "10k in, 5k out, ~$1.5"),
        ((10_000, 8_000, 5_000, 0.0), "10k in, 8k cached, 5k out"),
    ],
    ids=["cost-without-cache", "cache-without-cost"],
)
def test_format_usage_log(args, expected):
    assert format_usage_log(*args) == expected
