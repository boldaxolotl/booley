"""Shared MCP-output-budget helper for the Booley Flows' excerpt/tail caps.

The MCP server tail-truncates every Flow's stdout to a byte budget
(``BOOLEY_MCP_MAX_STDOUT_BYTES``, default 12 000 — see
``mcp_server._max_stdout_bytes``). The Booley Flows bound their own failure
excerpts well under that budget so one chatty failing target can't
monopolize the surviving window — but those bounds were pinned at
12 KB-era literals, so an operator who raises the MCP budget got no more
context out of the Flows.

This module reads the SAME env knob (deliberately replicated rather than
imported — the Flows must not depend on the server module, which pulls in
the whole MCP stack) and scales a Flow's cap proportionally with it.
With the env unset the caps are byte-identical to the historical
defaults.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Must mirror mcp_server._DEFAULT_MAX_STDOUT_BYTES / the env name read by
# mcp_server._max_stdout_bytes() — the caps below are calibrated against the
# server's truncation window, so the two sides have to agree on the knob.
_BUDGET_ENV = "BOOLEY_MCP_MAX_STDOUT_BYTES"
_DEFAULT_BUDGET_BYTES = 12_000


def mcp_stdout_budget() -> int:
    """The MCP server's stdout byte budget (env-overridable, validated).

    Same tolerant read as ``mcp_server._env_positive_int``: garbage or a
    non-positive value falls back to the 12 000-byte default with a warning
    — a zero/negative cap would render every excerpt empty, which is never
    what an operator wants.
    """
    raw = os.environ.get(_BUDGET_ENV)
    if raw is None or raw == "":
        return _DEFAULT_BUDGET_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", _BUDGET_ENV, raw)
        return _DEFAULT_BUDGET_BYTES
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r", _BUDGET_ENV, raw)
        return _DEFAULT_BUDGET_BYTES
    return value


def scaled(default: int, *, per_12k: int | None = None) -> int:
    """Scale a Flow-side excerpt cap with the MCP stdout budget.

    Returns *default* unchanged at the stock 12 000-byte budget (keeping the
    historical behavior byte-identical when the env is unset); with a raised
    budget it grows proportionally: ``rate * budget / 12_000`` (integer),
    where *rate* is *per_12k* — the cap earned per 12 KB of budget — when
    given, else *default* itself. Never returns less than *default*: a
    shrunken budget keeps today's caps, since the MCP layer's own truncation
    already enforces the smaller window.
    """
    budget = mcp_stdout_budget()
    if budget == _DEFAULT_BUDGET_BYTES:
        return default
    rate = default if per_12k is None else per_12k
    return max(default, (rate * budget) // _DEFAULT_BUDGET_BYTES)
