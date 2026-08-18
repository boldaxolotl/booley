"""Tests for booley.runtime.nested_mcp_capabilities — single allowlist matrix."""

from __future__ import annotations

import pytest

from booley.runtime.nested_mcp_capabilities import (
    NESTED_MCP_CAPABILITIES,
    nested_mcp_tools_for,
)


class TestNestedMcpToolsFor:
    def test_known_specialist_returns_allowlist(self):
        assert "sim" in nested_mcp_tools_for("coverage_analyst")
        assert nested_mcp_tools_for("coverage_analyst") == ["sim", "bwave"]

    def test_returns_fresh_list(self):
        # Caller-mutation must not leak back into the matrix.
        a = nested_mcp_tools_for("coverage_analyst")
        b = nested_mcp_tools_for("coverage_analyst")
        a.append("evil")
        assert "evil" not in b
        assert "evil" not in NESTED_MCP_CAPABILITIES["coverage_analyst"]

    def test_unknown_specialist_returns_empty(self):
        # Default-deny — unknown names get no MCP exposure.
        assert nested_mcp_tools_for("does_not_exist") == []

    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("reviewer", []),
            ("mutation_tester", []),
            ("tb_coder", ["elab"]),
            ("coverage_analyst", ["sim", "bwave"]),
        ],
    )
    def test_specialist_allowlists(self, spec, expected):
        assert nested_mcp_tools_for(spec) == expected
