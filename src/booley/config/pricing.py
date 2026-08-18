"""Single source of truth for model prices and context-window sizes.

Both the live agent backends (``booley.runtime._cost``) and the ticket-board
reports (``booley.ticket_board.analytics``) used to carry their own price
tables, which disagreed: the harness table had no cache-*write* rate at all
and billed cache creation at the plain input price, and the two tables
disagreed on unknown-model fallback (one returned $0.00, the other silently
assumed Sonnet). Everything now resolves through :func:`resolve`.

Rates are USD per million tokens, taken from Anthropic/OpenAI list pricing.
Cache reads bill at ~0.1x input, cache writes at 1.25x input for the default
5-minute TTL (a 1h TTL is 2x, which Booley does not request).
"""

from __future__ import annotations

from dataclasses import dataclass

# Context-window sizes we know. ``None`` means "not published here" — callers
# render the running context without a denominator rather than guessing.
_CTX_1M = 1_000_000
_CTX_200K = 200_000


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token rates plus the model's context window."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    context_window: int | None = None


def _anthropic(inp: float, out: float, context_window: int | None) -> ModelPricing:
    """Build an Anthropic price row from the two published rates.

    Cache read/write are fixed multiples of the input rate (0.1x / 1.25x), so
    deriving them keeps the table honest — there is no way to update the input
    price and forget the cache prices.
    """
    return ModelPricing(
        input=inp,
        output=out,
        cache_read=round(inp * 0.1, 4),
        cache_write=round(inp * 1.25, 4),
        context_window=context_window,
    )


# Retired models stay listed: historical runs are re-costed from their recorded
# model id, and a project can still pin one via [models].
#
# NOTE: Sonnet 5 carries an introductory $2/$10 rate through 2026-08-31. We list
# the $3/$15 sticker price, so cost readouts for Sonnet 5 runs made before that
# date are an over-estimate rather than a surprise.
MODEL_PRICING: dict[str, ModelPricing] = {
    # --- Anthropic ---
    "claude-fable-5": _anthropic(10.00, 50.00, _CTX_1M),
    "claude-mythos-5": _anthropic(10.00, 50.00, _CTX_1M),
    "claude-opus-5": _anthropic(5.00, 25.00, _CTX_1M),
    "claude-opus-4-8": _anthropic(5.00, 25.00, _CTX_1M),
    "claude-opus-4-7": _anthropic(5.00, 25.00, _CTX_1M),
    "claude-opus-4-6": _anthropic(5.00, 25.00, _CTX_1M),
    "claude-sonnet-5": _anthropic(3.00, 15.00, _CTX_1M),
    "claude-sonnet-4-6": _anthropic(3.00, 15.00, _CTX_1M),
    "claude-haiku-4-5": _anthropic(1.00, 5.00, _CTX_200K),
    # --- OpenAI (Codex backend). Context windows are not tracked here, so the
    # Console renders their context usage without a denominator. ---
    "gpt-5.6-sol": ModelPricing(5.00, 30.00, 0.50, 6.25),
    "gpt-5.6-terra": ModelPricing(2.50, 15.00, 0.25, 3.125),
    "gpt-5.6-luna": ModelPricing(1.00, 6.00, 0.10, 1.25),
    "gpt-5.5": ModelPricing(5.00, 30.00, 0.50, 6.25),
    "gpt-5.4": ModelPricing(2.50, 15.00, 0.25, 3.125),
    "gpt-5.4-mini": ModelPricing(0.75, 4.50, 0.075, 0.9375),
    "o3": ModelPricing(2.00, 8.00, 0.50, 2.50),
}

# Family fallbacks for ids we don't recognise exactly (e.g. a dated snapshot of
# a model released after this table was written). Each maps to a table key
# whose price is the most expensive in that family, so an unknown id is
# over-costed rather than silently reported as free.
_FAMILY_FALLBACK: tuple[tuple[str, str], ...] = (
    ("fable", "claude-fable-5"),
    ("mythos", "claude-mythos-5"),
    ("opus", "claude-fable-5"),
    ("sonnet", "claude-sonnet-5"),
    ("haiku", "claude-haiku-4-5"),
)

# Substring candidates, longest first: a bare "o3" must not shadow a longer,
# more specific key that also happens to contain it.
_SUBSTRING_KEYS: tuple[str, ...] = tuple(sorted(MODEL_PRICING, key=len, reverse=True))


def resolve(model: str) -> ModelPricing | None:
    """Look up pricing for *model*, or ``None`` if we have no idea.

    Matching is exact, then substring (so ``claude-opus-5[1m]`` and dated
    snapshots resolve), then by model family.
    """
    if not model:
        return None
    exact = MODEL_PRICING.get(model)
    if exact is not None:
        return exact
    for key in _SUBSTRING_KEYS:
        if key in model:
            return MODEL_PRICING[key]
    for token, key in _FAMILY_FALLBACK:
        if token in model:
            return MODEL_PRICING[key]
    return None


def context_limit(model: str) -> int | None:
    """Return *model*'s context window in tokens, or ``None`` if unknown."""
    prices = resolve(model)
    return prices.context_window if prices else None


def estimate_cost(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cache_create_tokens: int = 0,
) -> float:
    """Estimate USD cost for one agent call.

    ``input_tokens`` is the *inclusive* prompt total as the APIs report it — it
    already contains ``cached_tokens`` (cache reads) and
    ``cache_create_tokens`` (cache writes), both of which are subtracted out
    and billed at their own rates. Returns 0.0 for a model we can't price.
    """
    prices = resolve(model)
    if prices is None:
        return 0.0
    uncached = max(input_tokens - cached_tokens - cache_create_tokens, 0)
    return (
        uncached * prices.input
        + cached_tokens * prices.cache_read
        + cache_create_tokens * prices.cache_write
        + output_tokens * prices.output
    ) / 1_000_000
