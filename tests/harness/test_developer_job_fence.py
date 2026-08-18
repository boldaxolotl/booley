"""Tests for developer finalization behind detached ticket jobs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from booley.harness import developer, job_fence
from booley.harness.models import TicketContext


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
    monkeypatch.setattr(job_fence, "active_ticket_jobs", lambda _log_dir: active)
    monkeypatch.setattr(job_fence, "wait_for_ticket_jobs", wait)
    monkeypatch.setattr(developer.terminal, "raw", lambda _line: None)

    await developer._drain_outstanding_ticket_jobs(ctx)

    wait.assert_awaited_once_with(ctx.logs_dir)
