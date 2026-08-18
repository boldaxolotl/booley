"""Tests for _output_budget — MCP-budget-derived excerpt/tail caps.

The Booley Flows bound their failure excerpts against the MCP server's stdout
truncation window (BOOLEY_MCP_MAX_STDOUT_BYTES, default 12 000). These pin
the contract: unset env keeps the historical caps byte-identical; a raised
budget scales them proportionally; a shrunken/garbage budget never shrinks
a cap below its default.
"""

from __future__ import annotations

import pytest

from booley.flows.output_budget import mcp_stdout_budget, scaled

_ENV = "BOOLEY_MCP_MAX_STDOUT_BYTES"


class TestMcpStdoutBudget:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(_ENV, raising=False)
        assert mcp_stdout_budget() == 12_000

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(_ENV, "24000")
        assert mcp_stdout_budget() == 24_000

    def test_garbage_falls_back(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(_ENV, "not-a-number")
        assert mcp_stdout_budget() == 12_000

    def test_non_positive_falls_back(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(_ENV, "0")
        assert mcp_stdout_budget() == 12_000
        monkeypatch.setenv(_ENV, "-5")
        assert mcp_stdout_budget() == 12_000

    def test_empty_string_falls_back(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(_ENV, "")
        assert mcp_stdout_budget() == 12_000


class TestScaled:
    def test_default_budget_is_byte_identical(self, monkeypatch: pytest.MonkeyPatch):
        """Unset env → the historical caps come back untouched."""
        monkeypatch.delenv(_ENV, raising=False)
        assert scaled(30) == 30  # simulate excerpt lines
        assert scaled(2000) == 2000  # elaborate tail chars

    def test_explicit_default_budget_is_byte_identical(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(_ENV, "12000")
        assert scaled(30) == 30

    def test_raised_budget_scales_proportionally(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(_ENV, "24000")
        assert scaled(30) == 60
        assert scaled(2000) == 4000
        monkeypatch.setenv(_ENV, "18000")
        assert scaled(30) == 45
        assert scaled(2000) == 3000

    def test_integer_math_floors(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(_ENV, "13000")
        assert scaled(30) == (30 * 13_000) // 12_000  # 32, floored

    def test_never_below_default(self, monkeypatch: pytest.MonkeyPatch):
        """A shrunken budget keeps today's caps — the MCP layer's own
        truncation already enforces the smaller window."""
        monkeypatch.setenv(_ENV, "6000")
        assert scaled(30) == 30
        assert scaled(2000) == 2000

    def test_per_12k_rate_overrides_the_slope(self, monkeypatch: pytest.MonkeyPatch):
        """per_12k decouples the growth rate from the floor."""
        monkeypatch.setenv(_ENV, "24000")
        assert scaled(30, per_12k=60) == 120
        # At the stock budget the default (floor) still wins outright.
        monkeypatch.setenv(_ENV, "12000")
        assert scaled(30, per_12k=60) == 30
