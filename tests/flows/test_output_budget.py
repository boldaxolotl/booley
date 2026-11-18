"""Tests for _output_budget — MCP-budget-derived excerpt/tail caps.

The Booley Flows bound their failure excerpts against the MCP server's stdout
truncation window (BOOLEY_MCP_MAX_STDOUT_BYTES, default 12 000). These pin
the contract: unset env keeps the historical caps byte-identical; a raised
budget scales them proportionally; a shrunken/garbage budget never shrinks
a cap below its default.
"""

from __future__ import annotations

import pytest

from booley.flows.output_budget import mcp_stderr_budget, mcp_stdout_budget, scaled
from booley.mcp import report_encoding

_ENV = "BOOLEY_MCP_MAX_STDOUT_BYTES"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 12_000),
        ("", 12_000),
        ("bad", 12_000),
        ("0", 12_000),
        ("-2", 12_000),
        ("300", 300),
        ("24000", 24_000),
    ],
)
def test_stream_budgets_match_actual_mcp_server(monkeypatch, value, expected):
    for variable in ("BOOLEY_MCP_MAX_STDOUT_BYTES", "BOOLEY_MCP_MAX_STDERR_BYTES"):
        if value is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)
    assert mcp_stdout_budget() == expected
    # Private seam: the MCP server's limits are module internals of report
    # encoding; this sync check (#1120) compares both readers on one environment.
    assert mcp_stderr_budget() == report_encoding._max_stderr_bytes()
    assert mcp_stdout_budget() == report_encoding._max_stdout_bytes()


@pytest.mark.parametrize(
    ("env", "default", "per_12k", "expected"),
    [
        (None, 30, None, 30),  # unset keeps the historical cap
        (None, 2000, None, 2000),
        ("12000", 30, None, 30),
        ("24000", 30, None, 60),
        ("24000", 2000, None, 4000),
        ("18000", 30, None, 45),
        ("18000", 2000, None, 3000),
        ("13000", 30, None, 32),  # 32.5, floored
        ("6000", 30, None, 30),  # a shrunken budget never shrinks a cap
        ("6000", 2000, None, 2000),
        ("24000", 30, 60, 120),  # per_12k decouples the slope from the floor
        ("12000", 30, 60, 30),
    ],
)
def test_scaled(monkeypatch, env, default, per_12k, expected):
    if env is None:
        monkeypatch.delenv(_ENV, raising=False)
    else:
        monkeypatch.setenv(_ENV, env)
    kwargs = {} if per_12k is None else {"per_12k": per_12k}
    assert scaled(default, **kwargs) == expected
