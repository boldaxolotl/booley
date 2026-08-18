"""Lifecycle tests for automatic blocked-ticket dossier preparation."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from booley.harness import developer
from booley.harness.models import TicketContext


@pytest.mark.asyncio
async def test_setup_block_prepares_blocked_triage_dossier(tmp_path: Path, monkeypatch):
    ctx = TicketContext(
        slug="demo",
        ticket_path=tmp_path / "demo.md",
        ticket_type="feature",
        branch="main",
        summary="demo",
        project_root=tmp_path,
    )
    monkeypatch.setattr(developer, "_display_ticket_banner", lambda _ctx: None)
    monkeypatch.setattr(developer, "_recover_setup_state", lambda *_args: None)
    monkeypatch.setattr(developer, "_invalidate_missing_worktree", lambda *_args: None)
    monkeypatch.setattr(developer, "_run_setup_step", AsyncMock(return_value=True))
    prepare = AsyncMock()
    monkeypatch.setattr(developer, "_prepare_blocked_triage", prepare)

    await developer._run_ticket_body(ctx, tmp_path, 0.0)

    prepare.assert_awaited_once_with(ctx, tmp_path)
