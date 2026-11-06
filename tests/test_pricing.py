"""Tests for booley.config.pricing — the one model price / context-window table."""

from __future__ import annotations

import pytest

from booley.config.pricing import (
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
        assert p is MODEL_PRICING["claude-fable-5-1"]

    def test_family_fallbacks(self):
        """An unrecognised id resolves to its family's current (or priciest) row."""
        expected = {
            "claude-sonnet-42": "claude-sonnet-5-5",
            "claude-haiku-99": "claude-haiku-4-5",
            "some-fable-build": "claude-fable-5-1",
            "internal-mythos-x": "claude-mythos-5",
        }
        for model, expected_key in expected.items():
            assert resolve(model) is MODEL_PRICING[expected_key], model

    def test_published_rows(self):
        """Prices and windows as published: Anthropic and OpenAI pages, checked 2026-11-04."""
        # model: ((input, output, cache_read, cache_write), context window)
        published = {
            "claude-fable-5-1": ((10.00, 50.00, 0.25, 12.50), 1_000_000),
            "claude-opus-5-5": ((4.00, 20.00, 0.20, 5.00), 1_000_000),
            "claude-sonnet-5-5": ((2.00, 10.00, 0.10, 2.50), 1_000_000),
            # Sonnet 5: $2/$10 is the standard price, not an introductory one.
            "claude-sonnet-5": ((2.00, 10.00, 0.20, 2.50), 1_000_000),
            "gpt-6.1-sol": ((2.00, 10.00, 0.10, 2.50), None),
        }
        for model, (prices, window) in published.items():
            row = resolve(model)
            assert row is MODEL_PRICING[model], model
            assert (row.input, row.output, row.cache_read, row.cache_write) == pytest.approx(
                prices
            ), model
            assert context_limit(model) == window, model

    def test_dated_snapshot_of_current_model_resolves_to_its_row(self):
        """A snapshot must not be priced as the older, shorter-named model."""
        assert resolve("claude-opus-5-5-20261101") is MODEL_PRICING["claude-opus-5-5"]

    def test_retired_rows_remain(self):
        """Historical runs keep their own prices."""
        retired = [
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-sonnet-5",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
        ]
        for model in retired:
            assert model in MODEL_PRICING, model

    def test_unknown_returns_none(self):
        assert resolve("totally-unknown-model") is None

    def test_empty_returns_none(self):
        assert resolve("") is None

    def test_gpt_6_astra_has_its_own_row_and_is_the_gpt_floor(self):
        """Astra is priced as itself, and it is what an unknown GPT id falls back to."""
        row = MODEL_PRICING.get("gpt-6-astra")
        assert row is not None
        assert resolve("gpt-6-astra") is row
        assert (row.input, row.output, row.cache_read, row.cache_write) == pytest.approx(
            (10.00, 50.00, 1.00, 12.50)
        )
        assert resolve("gpt-7-nova") is row

    def test_gpt_6_luna_resolves_to_its_own_row(self):
        p = resolve("gpt-6-luna")
        assert p is not None
        assert (p.input, p.output, p.cache_read, p.cache_write) == pytest.approx(
            (0.10, 0.50, 0.01, 0.125)
        )

    def test_claude_haiku_5_5_resolves_to_its_own_row(self):
        """Must not fall back to the dearer haiku-4-5 row via the family token."""
        p = resolve("claude-haiku-5-5")
        assert p is not None
        assert (p.input, p.output, p.cache_read, p.cache_write) == pytest.approx(
            (0.10, 0.50, 0.01, 0.125)
        )

    def test_unknown_gpt_prices_at_most_expensive_gpt_row(self):
        """An unrecognised GPT id is over-costed, not reported as free.

        Ranking over the table pins the fallback target: adding a pricier GPT
        row later fails this test until the fallback moves to it.
        """
        p = resolve("gpt-7-nova")
        assert p is not None
        gpt_rows = [row for name, row in MODEL_PRICING.items() if name.startswith("gpt-")]
        priciest = max(gpt_rows, key=lambda r: (r.output, r.input, r.cache_write, r.cache_read))
        assert (p.input, p.output, p.cache_read, p.cache_write) == (
            priciest.input,
            priciest.output,
            priciest.cache_read,
            priciest.cache_write,
        )
        # The fallback must not swallow a known dated GPT snapshot.
        assert resolve("gpt-5.4-mini-20260101") is MODEL_PRICING["gpt-5.4-mini"]


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

    def test_every_builtin_model_has_its_own_row(self, tmp_path, monkeypatch):
        """A default model without its own row would be priced as another model."""
        from booley.config import agent as agent_config

        for var in ("BOOLEY_PRIMARY_PROVIDER", "BOOLEY_PRIMARY_AUTH", "BOOLEY_DIR"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(agent_config, "_inside_container", lambda: False)
        project_data = tmp_path / ".booley"
        project_data.mkdir()
        for provider in ("claude", "codex"):
            monkeypatch.setattr(agent_config, "_agent_settings", None)
            (project_data / "booley.toml").write_text(f'[agent]\nprovider = "{provider}"\n')
            agent_config.load_agent_settings(tmp_path)
            settings = agent_config.get_agent_settings()
            assert settings.internal_model() in MODEL_PRICING
            for model in settings.model_settings():
                assert model in MODEL_PRICING


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

    def test_claude_haiku_5_5_window_is_1m(self):
        assert context_limit("claude-haiku-5-5") == 1_000_000

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
