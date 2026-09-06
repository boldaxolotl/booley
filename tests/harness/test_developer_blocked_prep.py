"""Lifecycle tests for automatic blocked-ticket dossier preparation."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from booley.harness import developer
from booley.harness.models import StepResult, TicketContext


def _context(tmp_path: Path, **overrides: Any) -> TicketContext:
    return TicketContext(
        slug="demo",
        ticket_path=tmp_path / "demo.md",
        ticket_type="feature",
        branch="main",
        summary="demo",
        project_root=tmp_path,
        **overrides,
    )


@pytest.mark.asyncio
async def test_setup_block_prepares_blocked_triage_dossier(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    monkeypatch.setattr(developer, "_display_ticket_banner", lambda _ctx: None)
    monkeypatch.setattr(developer, "_recover_setup_state", lambda *_args: None)
    monkeypatch.setattr(developer, "_invalidate_missing_worktree", lambda *_args: None)
    monkeypatch.setattr(developer, "_run_setup_step", AsyncMock(return_value=True))
    prepare = AsyncMock()
    monkeypatch.setattr(developer, "_prepare_blocked_triage", prepare)

    await developer._run_ticket_body(ctx, tmp_path, 0.0)

    prepare.assert_awaited_once_with(ctx, tmp_path)


@pytest.mark.asyncio
async def test_invalid_resumed_basis_prepares_blocked_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(
        tmp_path,
        completed_steps=["setup"],
        acceptance_basis=MagicMock(),
        worktree_path=tmp_path / "worktree",
    )
    monkeypatch.setattr(developer, "_display_ticket_banner", lambda _ctx: None)
    monkeypatch.setattr(developer, "_recover_setup_state", lambda *_args: None)
    monkeypatch.setattr(developer, "_invalidate_missing_worktree", lambda *_args: None)
    monkeypatch.setattr(
        developer,
        "_resumed_basis_failure",
        lambda _ctx: "acceptance-input-change-required: basis changed",
    )
    block = MagicMock()
    monkeypatch.setattr(developer, "block_ticket", block)
    prepare = AsyncMock()
    monkeypatch.setattr(developer, "_prepare_blocked_triage", prepare)

    await developer._run_ticket_body(ctx, tmp_path, 0.0)

    block.assert_called_once_with(
        ctx,
        "acceptance-input-change-required: basis changed",
        "setup",
    )
    prepare.assert_awaited_once_with(ctx, tmp_path)


def test_context_without_completed_setup_needs_no_invalidation(tmp_path: Path) -> None:
    ctx = _context(tmp_path)

    developer._invalidate_missing_worktree(ctx, tmp_path)

    assert ctx.completed_steps == []


def test_missing_resumed_worktree_invalidates_completed_setup(tmp_path: Path) -> None:
    ctx = _context(
        tmp_path,
        completed_steps=["setup"],
        current_step="implement",
        feature_branch="demo",
    )

    developer._invalidate_missing_worktree(ctx, tmp_path)

    assert "setup" not in ctx.completed_steps
    assert ctx.current_step == ""
    assert ctx.feature_branch == ""


def test_resumed_basis_is_revalidated_in_existing_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    ctx = _context(
        tmp_path,
        acceptance_basis=MagicMock(),
        worktree_path=worktree,
    )
    validate = MagicMock(return_value=StepResult(block_reason="basis changed"))
    monkeypatch.setattr(
        "booley.harness.setup.workspace._validate_materialized_acceptance_basis",
        validate,
    )

    assert developer._resumed_basis_failure(ctx) == "basis changed"
    validate.assert_called_once_with(ctx, worktree)


def test_valid_resumed_basis_allows_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    ctx = _context(
        tmp_path,
        acceptance_basis=MagicMock(),
        worktree_path=worktree,
    )
    validate = MagicMock(return_value=None)
    monkeypatch.setattr(
        "booley.harness.setup.workspace._validate_materialized_acceptance_basis",
        validate,
    )

    assert developer._resumed_basis_failure(ctx) is None
    validate.assert_called_once_with(ctx, worktree)


def test_basisless_resume_needs_no_basis_validation(tmp_path: Path) -> None:
    ctx = _context(tmp_path)

    assert developer._resumed_basis_failure(ctx) is None


def test_basis_bound_resume_without_worktree_reports_basis_failure(tmp_path: Path) -> None:
    ctx = _context(tmp_path, acceptance_basis=MagicMock())

    assert developer._resumed_basis_failure(ctx) == (
        "acceptance-input-change-required: Ticket worktree is unavailable"
    )


def test_deferred_criteria_initializes_against_materialized_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    ctx = _context(
        tmp_path,
        worktree_path=worktree,
        criteria_state_needs_init=True,
    )
    roots: list[Path] = []

    def capture_root(context: TicketContext) -> None:
        roots.append(context.work_dir)

    monkeypatch.setattr("booley.harness.setup.intake._init_criteria_state", capture_root)

    assert developer._deferred_criteria_failure(ctx) is None
    assert roots == [worktree]
    assert ctx.criteria_state_needs_init is False


def test_current_criteria_state_needs_no_initialization(tmp_path: Path) -> None:
    ctx = _context(tmp_path)

    assert developer._deferred_criteria_failure(ctx) is None


def test_deferred_criteria_returns_fatal_initialization_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path, criteria_state_needs_init=True)
    monkeypatch.setattr(
        "booley.harness.setup.intake._init_criteria_state",
        MagicMock(side_effect=developer.FatalError("criteria initialization failed")),
    )

    assert developer._deferred_criteria_failure(ctx) == "criteria initialization failed"
    assert ctx.criteria_state_needs_init is True
