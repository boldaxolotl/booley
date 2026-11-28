"""Tests for booley.config.pricing — the one model price / context-window table."""

from __future__ import annotations

import pytest

from booley.config.pricing import (
    MODEL_PRICING,
    context_limit,
    estimate_cost,
    resolve,
)
from booley.runtime.runtime_context import RuntimeLocation
from tests.sandbox_venue_fake import place


@pytest.mark.parametrize(
    ("model", "row"),
    [
        ("claude-opus-5[1m]", "claude-opus-5"),  # long-context variant
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),  # dated snapshot
        # A snapshot is not priced as the older, shorter-named model.
        ("claude-opus-5-5-20261101", "claude-opus-5-5"),
        ("gpt-5.4-mini-20260101", "gpt-5.4-mini"),
        # A short key does not shadow a longer, more specific one.
        ("claude-sonnet-4-6", "claude-sonnet-4-6"),
        # Retired rows remain: historical runs keep their own prices.
        ("claude-fable-5", "claude-fable-5"),
        ("claude-opus-4-8", "claude-opus-4-8"),
        ("gpt-5.6-sol", "gpt-5.6-sol"),
        ("gpt-5.6-luna", "gpt-5.6-luna"),
        # Family fallbacks over-cost an unrecognised id rather than zeroing it.
        ("claude-opus-9-turbo", "claude-fable-5-1"),
        ("claude-sonnet-42", "claude-sonnet-5-5"),
        ("claude-haiku-99", "claude-haiku-4-5"),
        ("some-fable-build", "claude-fable-5-1"),
        ("internal-mythos-x", "claude-mythos-5"),
        ("gpt-7-nova", "gpt-6-astra"),
    ],
)
def test_resolve_picks_the_row(model, row):
    assert resolve(model) is MODEL_PRICING[row]


@pytest.mark.parametrize("model", ["totally-unknown-model", ""])
def test_unknown_model_has_no_price_window_or_cost(model):
    assert resolve(model) is None
    assert context_limit(model) is None
    assert estimate_cost(model, 1_000, 0, 1_000) == 0.0


def test_published_rows():
    """Prices and windows as published: Anthropic and OpenAI pages, checked 2026-11-04."""
    # model: ((input, output, cache_read, cache_write), context window)
    published = {
        "claude-fable-5-1": ((10.00, 50.00, 0.25, 12.50), 1_000_000),
        "claude-opus-5-5": ((4.00, 20.00, 0.20, 5.00), 1_000_000),
        "claude-sonnet-5-5": ((2.00, 10.00, 0.10, 2.50), 1_000_000),
        # Sonnet 5: $2/$10 is the standard price, not an introductory one.
        "claude-sonnet-5": ((2.00, 10.00, 0.20, 2.50), 1_000_000),
        "claude-haiku-5-5": ((0.10, 0.50, 0.01, 0.125), 1_000_000),
        "gpt-6.1-sol": ((2.00, 10.00, 0.10, 2.50), None),
        "gpt-6-astra": ((10.00, 50.00, 1.00, 12.50), None),
        "gpt-6-luna": ((0.10, 0.50, 0.01, 0.125), None),
    }
    for model, (prices, window) in published.items():
        row = resolve(model)
        assert row is MODEL_PRICING[model], model
        assert (row.input, row.output, row.cache_read, row.cache_write) == pytest.approx(prices), (
            model
        )
        assert context_limit(model) == window, model


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5[1m]", 1_000_000),
        ("claude-fable-5", 1_000_000),
        ("claude-haiku-4-5", 200_000),
        # Not guessed: the Console renders their context without a denominator.
        ("gpt-5.6-sol", None),
    ],
)
def test_context_windows(model: str, expected: int | None):
    assert context_limit(model) == expected


def test_unknown_gpt_prices_at_most_expensive_gpt_row():
    """An unrecognised GPT id is over-costed, not reported as free.

    Ranking over the table pins the fallback target: adding a pricier GPT
    row later fails this test until the fallback moves to it.
    """
    gpt_rows = [row for name, row in MODEL_PRICING.items() if name.startswith("gpt-")]
    priciest = max(gpt_rows, key=lambda r: (r.output, r.input, r.cache_write, r.cache_read))
    assert resolve("gpt-7-nova") == priciest


@pytest.mark.parametrize("name", sorted(MODEL_PRICING))
def test_row_rates_are_ordered(name: str):
    """A cache read costs less than fresh input, a cache write and output more."""
    p = MODEL_PRICING[name]
    assert 0 < p.cache_read < p.input < p.cache_write
    assert p.output > p.input


def test_every_builtin_model_has_its_own_row(tmp_path, monkeypatch):
    """A default model without its own row would be priced as another model."""
    from booley.config import agent as agent_config

    for var in ("BOOLEY_PRIMARY_PROVIDER", "BOOLEY_PRIMARY_AUTH", "BOOLEY_DIR"):
        monkeypatch.delenv(var, raising=False)
    place(monkeypatch, RuntimeLocation.HOST)
    # private: load_agent_settings installs process-wide settings; patching the
    # cache lets teardown restore whatever an earlier test installed.
    monkeypatch.setattr(agent_config, "_agent_settings", None)
    project_data = tmp_path / ".booley"
    project_data.mkdir()
    for provider in ("claude", "codex"):
        (project_data / "booley.toml").write_text(f'[agent]\nprovider = "{provider}"\n')
        agent_config.load_agent_settings(tmp_path)
        settings = agent_config.get_agent_settings()
        assert settings.internal_model() in MODEL_PRICING
        for model in settings.model_settings():
            assert model in MODEL_PRICING


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        # input_tokens is the inclusive prompt total. Opus 5 per-MTok: input $5,
        # cache read $0.50, cache write $6.25, output $25: 20k uncached * 5 +
        # 60k * 0.5 + 20k * 6.25 + 5k * 25 = $0.38.
        ((100_000, 60_000, 5_000, 20_000), 0.38),
        # The bug this table replaced: cache writes billed at the input rate ($0.05).
        ((10_000, 0, 0, 10_000), 0.0625),
        # Over-subscribed cache: uncached input clamps to 0, 5k * 0.50 + 5k * 6.25.
        ((1_000, 5_000, 0, 5_000), 0.03375),
        ((0, 0, 0), 0.0),  # no minimum charge; the omitted cache-write count defaults to 0
    ],
    ids=["all-four-classes", "cache-write-rate", "clamped-uncached", "zero-tokens"],
)
def test_estimate_cost_bills_each_token_class_at_its_rate(tokens, expected):
    assert estimate_cost("claude-opus-5", *tokens) == pytest.approx(expected)
