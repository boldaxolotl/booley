"""Tests for booley.pricing — the one model price / context-window table."""

from __future__ import annotations

import pytest

from booley.pricing import (
    MODEL_PRICING,
    context_limit,
    estimate_cost,
    resolve,
)

# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_exact_match(self):
        p = resolve("claude-opus-5")
        assert p is not None
        assert (p.input, p.output) == (5.00, 25.00)

    def test_context_suffixed_id(self):
        """The [1m] long-context variant must resolve to its base model."""
        assert resolve("claude-opus-5[1m]") is MODEL_PRICING["claude-opus-5"]

    def test_dated_snapshot(self):
        assert resolve("claude-haiku-4-5-20251001") is MODEL_PRICING["claude-haiku-4-5"]

    def test_longest_substring_wins(self):
        """A short key must not shadow a longer, more specific one."""
        assert resolve("claude-sonnet-4-6") is MODEL_PRICING["claude-sonnet-4-6"]
        assert resolve("claude-sonnet-5") is MODEL_PRICING["claude-sonnet-5"]

    def test_family_fallback_over_costs_rather_than_zeroing(self):
        """An unrecognised Opus resolves to the priciest family member, not free."""
        p = resolve("claude-opus-9-turbo")
        assert p is MODEL_PRICING["claude-fable-5"]

    @pytest.mark.parametrize(
        ("model", "expected_key"),
        [
            ("claude-sonnet-42", "claude-sonnet-5"),
            ("claude-haiku-99", "claude-haiku-4-5"),
            ("some-fable-build", "claude-fable-5"),
            ("internal-mythos-x", "claude-mythos-5"),
        ],
    )
    def test_family_fallbacks(self, model: str, expected_key: str):
        assert resolve(model) is MODEL_PRICING[expected_key]

    def test_unknown_returns_none(self):
        assert resolve("totally-unknown-model") is None

    def test_empty_returns_none(self):
        assert resolve("") is None


# ---------------------------------------------------------------------------
# Table invariants
# ---------------------------------------------------------------------------


class TestTableInvariants:
    @pytest.mark.parametrize("name", sorted(MODEL_PRICING))
    def test_cache_rates_are_cheaper_than_input(self, name: str):
        """A cache read must cost less than fresh input, or caching is pointless."""
        p = MODEL_PRICING[name]
        assert 0 < p.cache_read < p.input

    @pytest.mark.parametrize("name", sorted(MODEL_PRICING))
    def test_cache_write_costs_more_than_input(self, name: str):
        """Writing the cache carries a premium over plain input."""
        p = MODEL_PRICING[name]
        assert p.cache_write > p.input

    @pytest.mark.parametrize("name", sorted(MODEL_PRICING))
    def test_output_costs_more_than_input(self, name: str):
        assert MODEL_PRICING[name].output > MODEL_PRICING[name].input


class TestContextLimit:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("claude-opus-5", 1_000_000),
            ("claude-opus-5[1m]", 1_000_000),
            ("claude-fable-5", 1_000_000),
            ("claude-sonnet-5", 1_000_000),
            ("claude-haiku-4-5", 200_000),
        ],
    )
    def test_known_windows(self, model: str, expected: int):
        assert context_limit(model) == expected

    def test_unknown_model_has_no_limit(self):
        assert context_limit("totally-unknown-model") is None

    def test_codex_models_have_no_tracked_limit(self):
        """Not guessed: the Console renders their context without a denominator."""
        assert context_limit("gpt-5.6-sol") is None


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_all_four_token_classes_bill_at_their_own_rate(self):
        # input_tokens is the inclusive prompt total.
        cost = estimate_cost(
            "claude-opus-5",
            input_tokens=100_000,
            cached_tokens=60_000,
            output_tokens=5_000,
            cache_create_tokens=20_000,
        )
        p = MODEL_PRICING["claude-opus-5"]
        expected = (
            20_000 * p.input  # 100k - 60k read - 20k written
            + 60_000 * p.cache_read
            + 20_000 * p.cache_write
            + 5_000 * p.output
        ) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_cache_writes_cost_more_than_plain_input(self):
        """The bug this table replaced: cache writes billed at the input rate."""
        as_write = estimate_cost("claude-opus-5", 10_000, 0, 0, cache_create_tokens=10_000)
        as_plain = estimate_cost("claude-opus-5", 10_000, 0, 0)
        assert as_write > as_plain
        assert as_write == pytest.approx(as_plain * 1.25)

    def test_cache_create_defaults_to_zero(self):
        """Callers that don't track cache writes get the old behaviour."""
        assert estimate_cost("claude-opus-5", 10_000, 0, 1_000) == pytest.approx(
            estimate_cost("claude-opus-5", 10_000, 0, 1_000, 0)
        )

    def test_over_subscribed_cache_clamps_to_zero_uncached(self):
        cost = estimate_cost("claude-opus-5", 1_000, 5_000, 0, cache_create_tokens=5_000)
        p = MODEL_PRICING["claude-opus-5"]
        expected = (5_000 * p.cache_read + 5_000 * p.cache_write) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_unknown_model_is_free(self):
        assert estimate_cost("totally-unknown-model", 1_000, 0, 1_000) == 0.0

    def test_zero_tokens(self):
        assert estimate_cost("claude-opus-5", 0, 0, 0) == 0.0

    def test_a_fully_cached_turn_is_far_cheaper_than_a_cold_one(self):
        cold = estimate_cost("claude-opus-5", 200_000, 0, 1_000)
        warm = estimate_cost("claude-opus-5", 200_000, 200_000, 1_000)
        assert warm < cold / 5
