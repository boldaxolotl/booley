"""Tests for Developer Agent MCP exposure derived from ticket Criteria."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from booley.harness.developer import _discover_mcp_surface
from booley.mcp.registry import McpToolInfo


def test_review_criteria_fail_fast_when_reviewer_not_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "booley.harness.developer._load_endpoint_config",
        lambda _root: ({"reviewer": {"enabled": False}}, {}),
    )
    monkeypatch.setattr(
        "booley.mcp.registry.discover_mcp_tools",
        lambda **_kwargs: [
            McpToolInfo(
                name="sim",
                path="simulate.py",
                description="sim",
            ),
            McpToolInfo(
                name="submit_run_report",
                path="submit_run_report.py",
                description="report",
            ),
        ],
    )
    ctx = SimpleNamespace(
        criteria={"mandatory": {"review_tb_quality_clean": True}},
        work_dir=tmp_path / "work",
    )

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_discover_mcp_surface(tmp_path, ctx))

    assert "reviewer" in str(excinfo.value)


def test_developer_exposes_bwave_when_simulate_is_available(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "booley.harness.developer._load_endpoint_config",
        lambda _root: ({}, {}),
    )
    ctx = SimpleNamespace(criteria={}, work_dir=tmp_path / "work")

    mcp_surface = asyncio.run(_discover_mcp_surface(tmp_path, ctx))

    assert {"sim", "submit_run_report", "bwave"} <= set(mcp_surface.mcp_tool_names)
