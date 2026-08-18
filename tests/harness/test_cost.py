"""Tests for harness._cost — the usage-log formatter and its pricing re-export.

Pricing itself is covered in tests/test_pricing.py; this module keeps the
harness-facing surface honest.
"""

from __future__ import annotations

import pytest

from booley.harness._cost import _MODEL_PRICES, estimate_cost, format_usage_log

# ---------------------------------------------------------------------------
# estimate_cost — reachable through the harness alias
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_exact_model_match(self):
        """Known model should produce a deterministic non-zero cost."""
        cost = estimate_cost("claude-opus-4-6", 100_000, 0, 10_000)
        # 100k * 5/1M + 10k * 25/1M = 0.5 + 0.25 = 0.75
        assert cost == pytest.approx(0.75)

    def test_cached_tokens_reduce_cost(self):
        """Cached tokens use the cheaper per-token rate."""
        full = estimate_cost("claude-opus-4-6", 100_000, 0, 0)
        cached = estimate_cost("claude-opus-4-6", 100_000, 80_000, 0)
        assert cached < full

    def test_cached_tokens_rate(self):
        """Verify the exact cached-token math: uncached * price_in + cached * cache_read."""
        cost = estimate_cost("claude-opus-4-6", 100_000, 60_000, 5_000)
        # uncached = 40k, cache reads = 60k @ 0.1x, output = 5k
        expected = 40_000 * 5 / 1e6 + 60_000 * 0.5 / 1e6 + 5_000 * 25 / 1e6
        assert cost == pytest.approx(expected)

    def test_unknown_model_returns_zero(self):
        """A name with no exact, substring, or family match returns 0.0."""
        assert estimate_cost("totally-unknown-model", 1000, 0, 1000) == 0.0

    def test_substring_match(self):
        """Model name containing a known id should still match."""
        cost = estimate_cost("claude-sonnet-4-6-20260101", 10_000, 0, 1_000)
        assert cost > 0.0

    def test_zero_tokens(self):
        assert estimate_cost("claude-opus-4-6", 0, 0, 0) == 0.0

    def test_cached_exceeds_input_clamped(self):
        """cached > input should clamp uncached to 0 (no negative cost)."""
        cost = estimate_cost("claude-opus-4-6", 1000, 5000, 100)
        expected = 0 + 5000 * 0.5 / 1e6 + 100 * 25 / 1e6
        assert cost == pytest.approx(expected)

    @pytest.mark.parametrize("model", list(_MODEL_PRICES.keys()))
    def test_all_known_models_positive(self, model: str):
        """Every model in the price table produces a positive cost for non-zero tokens."""
        cost = estimate_cost(model, 1000, 0, 1000)
        assert cost > 0.0


# ---------------------------------------------------------------------------
# format_usage_log
# ---------------------------------------------------------------------------


class TestFormatUsageLog:
    def test_basic_format(self):
        result = format_usage_log(10_000, 0, 5_000, 1.5)
        assert "10k in" in result
        assert "5k out" in result
        assert "~$1.5" in result

    def test_cached_shown_when_nonzero(self):
        result = format_usage_log(10_000, 8_000, 5_000, 1.0)
        assert "8k cached" in result

    def test_cached_hidden_when_zero(self):
        result = format_usage_log(10_000, 0, 5_000, 1.0)
        assert "cached" not in result

    def test_cost_hidden_when_zero(self):
        result = format_usage_log(10_000, 0, 5_000, 0.0)
        assert "$" not in result

    def test_comma_separation(self):
        result = format_usage_log(10_000, 5_000, 3_000, 0.5)
        # Should be comma-separated
        assert result.count(",") >= 2
