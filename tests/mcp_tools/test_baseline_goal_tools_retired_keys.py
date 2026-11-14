"""The Goal tools that schedule baseline EDA Jobs refuse while a retired [jobs] key is set."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from booley.mcp import server as mcp_server
from tests.mcp_tools.test_mcp_dispatch import _FakeLifetime, _JobManager


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_tool", ["goal_enter", "goal_status", "goal_propose_change"])
async def test_baseline_goal_tools_refuse_retired_jobs_keys(tmp_path, monkeypatch, goal_tool):
    """Like a Flow call, these Goal tools name the retired key instead of starting a baseline."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_bytes(b'[jobs]\nheavy_memory = "8g"\n')
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    monkeypatch.setattr(
        mcp_server, "_dispatch_special_mcp_tool", AsyncMock(side_effect=AssertionError("ran"))
    )

    result = await mcp_server._dispatch_application_tool(
        goal_tool,
        {},
        {"name": goal_tool},
        jobs=_JobManager(_FakeLifetime()),
        status_mcp_tool_names=[],
        mcp_tool_call_counts={},
    )

    assert result.is_error
    assert "heavy_memory (line 2: memory admission replaced it)" in result.value[0].text


@pytest.mark.asyncio
async def test_goal_finish_stays_open_with_a_retired_jobs_key(tmp_path, monkeypatch):
    """goal_finish runs no EDA Job, so the refusal leaves it reachable."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_bytes(b"[jobs]\nmax_heavy = 2\n")
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    reached = AsyncMock(return_value="finished")
    monkeypatch.setattr(mcp_server, "_dispatch_special_mcp_tool", reached)

    result = await mcp_server._dispatch_application_tool(
        "goal_finish",
        {},
        {"name": "goal_finish"},
        jobs=_JobManager(_FakeLifetime()),
        status_mcp_tool_names=[],
        mcp_tool_call_counts={},
    )

    assert result == "finished"
    reached.assert_awaited_once()
