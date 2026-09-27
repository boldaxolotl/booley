"""Explicit Ticket result coverage for developer orchestration branches."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from booley.harness import developer
from booley.harness.models import AgentResult, TicketContext


def _context(tmp_path: Path, **overrides) -> TicketContext:
    return TicketContext(
        slug="demo",
        ticket_path=tmp_path / "demo.md",
        ticket_type="feature",
        branch="main",
        summary="demo",
        project_root=tmp_path,
        **overrides,
    )


def _patch_ticket_body(monkeypatch, ctx: TicketContext) -> None:
    monkeypatch.setattr(developer, "_display_ticket_banner", lambda _ctx: None)
    monkeypatch.setattr(developer, "_recover_setup_state", lambda *_args: None)
    monkeypatch.setattr(developer, "_invalidate_missing_worktree", lambda *_args: None)
    monkeypatch.setattr(developer, "_prepare_blocked_triage", AsyncMock())
    monkeypatch.setattr(developer, "_log_final_cost", lambda *_args: None)
    monkeypatch.setattr(developer, "_refresh_link_ctx_post_setup", lambda *_args: None)
    assert "setup" in ctx.completed_steps


@pytest.mark.asyncio
async def test_scope_guard_refresh_failure_returns_failed(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, completed_steps=["setup"], worktree_path=tmp_path / "worktree")
    _patch_ticket_body(monkeypatch, ctx)
    monkeypatch.setattr(developer, "_resumed_basis_failure", lambda _ctx: None)
    monkeypatch.setattr(
        "booley.harness.setup.workspace.refresh_scope_guards",
        MagicMock(side_effect=OSError("read-only")),
    )
    fail = MagicMock()
    monkeypatch.setattr(developer, "fail_ticket", fail)

    result = await developer._run_ticket_body(ctx, tmp_path, 0.0)

    assert result.disposition == "failed"
    fail.assert_called_once()


@pytest.mark.asyncio
async def test_worktree_cleanup_runtime_error_returns_failed(tmp_path: Path, monkeypatch):
    ctx = _context(
        tmp_path,
        completed_steps=["setup"],
        current_step="developer",
        worktree_path=tmp_path / "worktree",
    )
    _patch_ticket_body(monkeypatch, ctx)
    monkeypatch.setattr(developer, "_resumed_basis_failure", lambda _ctx: None)
    monkeypatch.setattr(
        "booley.harness.setup.workspace.refresh_scope_guards", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(developer, "_deferred_criteria_failure", lambda _ctx: None)
    monkeypatch.setattr(
        developer, "_reset_worktree_if_dirty", MagicMock(side_effect=RuntimeError("git failed"))
    )
    fail = MagicMock()
    monkeypatch.setattr(developer, "fail_ticket", fail)

    result = await developer._run_ticket_body(ctx, tmp_path, 0.0)

    assert result.disposition == "failed"
    fail.assert_called_once()


class _Budget:
    def start(self, _run_index):
        return None

    def finish(self):
        return None

    def raise_if_exhausted(self):
        return None


def _patch_developer_path(monkeypatch, tmp_path: Path, *, agent_result):
    surface = SimpleNamespace(
        discovered_mcp_tools=[],
        mcp_tool_config={},
        flow_config={},
        booley_src=tmp_path,
        project_mcp_tools_dir=tmp_path,
        mcp_tool_names=[],
    )
    monkeypatch.setattr(
        developer,
        "_detect_crash_recovery",
        lambda _logs: (False, None, 0, tmp_path / "transcript.jsonl"),
    )
    monkeypatch.setattr(developer, "_discover_mcp_surface", AsyncMock(return_value=surface))
    monkeypatch.setattr(developer, "_criterion_endpoint_catalog", lambda *_args: MagicMock())
    monkeypatch.setattr(developer, "_build_prompt_context", lambda *_args: ("system", "user"))
    monkeypatch.setattr(
        developer, "_write_developer_prompt_snapshot", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(developer, "_ensure_worktree_populated", lambda *_args: None)
    monkeypatch.setattr(developer, "DeveloperBudget", lambda *_args, **_kwargs: _Budget())
    monkeypatch.setattr(
        "booley.config.settings.load_developer_limits_config", lambda *_args: MagicMock()
    )
    invoke = AsyncMock(return_value=agent_result)
    monkeypatch.setattr(developer, "_invoke_developer_agent", invoke)
    monkeypatch.setattr(developer, "_drain_outstanding_ticket_jobs", AsyncMock())
    monkeypatch.setattr(developer, "_record_agent_result", lambda *_args: None)
    monkeypatch.setattr(developer, "_run_post_guardrails", lambda *_args: False)
    monkeypatch.setattr(developer, "_run_post_developer_hook", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(developer, "maybe_auto_retry", lambda *_args: False)
    monkeypatch.setattr(developer, "_prepare_blocked_triage", AsyncMock())
    monkeypatch.setattr(developer, "record_crash", MagicMock())
    monkeypatch.setattr(developer, "fail_ticket", MagicMock())
    monkeypatch.setattr(developer.ticket_cli, "ticket_status", MagicMock(return_value="running"))
    return invoke


@pytest.mark.asyncio
async def test_early_blocked_triage_error_preserves_result(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, completed_steps=["setup"], worktree_path=tmp_path / "worktree")
    _patch_ticket_body(monkeypatch, ctx)
    monkeypatch.setattr(developer, "_resumed_basis_failure", lambda _ctx: "basis changed")
    monkeypatch.setattr(developer, "block_ticket", MagicMock())
    monkeypatch.setattr(
        developer,
        "_prepare_blocked_triage",
        AsyncMock(side_effect=RuntimeError("dossier failed")),
    )

    result = await developer._run_ticket_body(ctx, tmp_path, 0.0)

    assert result.disposition == "blocked"


@pytest.mark.asyncio
async def test_agent_invocation_failure_returns_failed(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    _patch_developer_path(monkeypatch, tmp_path, agent_result=None)

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["guardrail", "hook"])
async def test_post_developer_block_returns_blocked(tmp_path: Path, monkeypatch, boundary: str):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    _patch_developer_path(
        monkeypatch,
        tmp_path,
        agent_result=AgentResult(output="done", input_tokens=1, output_tokens=1),
    )
    if boundary == "guardrail":
        monkeypatch.setattr(developer, "_run_post_guardrails", lambda *_args: True)
    else:
        monkeypatch.setattr(developer, "_run_post_developer_hook", lambda *_args, **_kwargs: True)

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "blocked"


@pytest.mark.asyncio
async def test_guardrail_block_wins_over_followup_budget_check(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    _patch_developer_path(
        monkeypatch,
        tmp_path,
        agent_result=AgentResult(output="done", input_tokens=1, output_tokens=1),
    )
    budget = MagicMock()
    budget.raise_if_exhausted.side_effect = [None, AssertionError("must not run after block")]
    monkeypatch.setattr(developer, "DeveloperBudget", lambda *_args, **_kwargs: budget)
    monkeypatch.setattr(developer, "_run_post_guardrails", lambda *_args: True)

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "blocked"
    assert budget.raise_if_exhausted.call_count == 1


@pytest.mark.parametrize(
    ("status", "expected", "expected_failures"),
    [("running", "failed", 1), ("review", "failed", 0), ("done", "done", 0)],
)
def test_path_failure_preserves_durable_board_state(
    tmp_path: Path, monkeypatch, status: str, expected: str, expected_failures: int
):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    monkeypatch.setattr(developer, "record_crash", MagicMock())
    failure = MagicMock()
    monkeypatch.setattr(developer, "fail_ticket", failure)
    monkeypatch.setattr(developer.ticket_cli, "ticket_status", MagicMock(return_value=status))

    result = developer._classify_developer_path_failure(
        ctx, tmp_path, 0, RuntimeError("handoff failed")
    )

    assert result.disposition == expected
    assert failure.call_count == expected_failures


@pytest.mark.asyncio
async def test_transient_crash_requeued_still_returns_failed(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    invoke = _patch_developer_path(monkeypatch, tmp_path, agent_result=None)
    invoke.side_effect = RuntimeError("transient")
    retry = MagicMock(return_value=True)
    monkeypatch.setattr(developer, "maybe_auto_retry", retry)

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "failed"
    retry.assert_called_once()


@pytest.mark.asyncio
async def test_finalizer_error_preserves_classified_result(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    _patch_developer_path(
        monkeypatch,
        tmp_path,
        agent_result=AgentResult(output="done", input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(developer, "_run_post_guardrails", lambda *_args: True)
    monkeypatch.setattr(
        developer, "maybe_auto_retry", MagicMock(side_effect=RuntimeError("finalizer failed"))
    )

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "blocked"
