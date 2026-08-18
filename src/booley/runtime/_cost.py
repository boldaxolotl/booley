"""Cost estimation and usage formatting for agent backends.

Prices live in :mod:`booley.config.pricing` — the one table shared with the
ticket-board reports. This module is the harness-facing wrapper.
"""

from __future__ import annotations

from booley.config.pricing import MODEL_PRICING as _MODEL_PRICES  # noqa: F401 — legacy alias
from booley.config.pricing import estimate_cost


def format_usage_log(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> str:
    """Format a compact usage string for log lines."""
    parts = [f"{input_tokens // 1000}k in"]
    if cached_tokens:
        parts.append(f"{cached_tokens // 1000}k cached")
    parts.append(f"{output_tokens // 1000}k out")
    if cost_usd > 0:
        parts.append(f"~${cost_usd:.1f}")
    return ", ".join(parts)


__all__ = ["estimate_cost", "format_usage_log"]
