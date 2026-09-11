"""Tests for developer finalization behind detached ticket jobs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from booley.harness import developer
from booley.harness.models import TicketContext
from booley.ticket_board import ticket_jobs


@pytest.mark.asyncio
async def test_developer_drains_jobs_before_final_bookkeeping(tmp_path: Path, monkeypatch):
    ctx = TicketContext(
        slug="demo",
        ticket_path=tmp_path / "demo.md",
        ticket_type="bugfix",
        branch="main",
        summary="demo",
        project_root=tmp_path,
    )
    active = [SimpleNamespace(endpoint="mutation_tester")]
    wait = AsyncMock()
    monkeypatch.setattr(ticket_jobs, "active_ticket_jobs", lambda _log_dir: active)
    monkeypatch.setattr(ticket_jobs, "wait_for_ticket_jobs", wait)
    monkeypatch.setattr(developer.terminal, "raw", lambda _line: None)

    await developer._drain_outstanding_ticket_jobs(ctx)

    wait.assert_awaited_once_with(ctx.logs_dir)
