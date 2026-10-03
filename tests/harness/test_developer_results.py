"""Explicit Ticket result coverage for developer orchestration branches."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from booley.criteria.state import DevelopmentState
from booley.harness import developer
from booley.harness.models import AgentResult, TicketContext
from booley.ticket_board.paths import ticket_runtime_file


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


async def _run_with_budget(coro, _budget):
    return await coro


def _patch_developer_path(monkeypatch, tmp_path: Path, *, agent_result):
    surface = SimpleNamespace(
        discovered_mcp_tools=[],
        specialist_config={},
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
    monkeypatch.setattr(developer, "run_with_developer_budget", _run_with_budget)
    monkeypatch.setattr(
        "booley.config.settings.load_developer_limits_config", lambda *_args: MagicMock()
    )
    invoke = AsyncMock(return_value=agent_result)
    monkeypatch.setattr(developer, "_invoke_developer_agent", invoke)
    monkeypatch.setattr(developer, "_drain_outstanding_ticket_jobs", AsyncMock())
    monkeypatch.setattr(developer, "_record_agent_result", lambda *_args: None)
    monkeypatch.setattr(developer, "_run_post_guardrails", lambda *_args: None)
    monkeypatch.setattr(developer, "_run_post_developer_hook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(developer, "maybe_auto_retry", lambda *_args: False)
    monkeypatch.setattr(developer, "_prepare_blocked_triage", AsyncMock())
    monkeypatch.setattr(developer, "record_crash", MagicMock())
    monkeypatch.setattr(developer, "fail_ticket", MagicMock())
    monkeypatch.setattr(developer.ticket_cli, "ticket_status", MagicMock(return_value="running"))
    return invoke


def _write_active_block(ctx: TicketContext, reason: str) -> Path:
    state_path = ticket_runtime_file(ctx.logs_dir, "booley_state.json")
    state = DevelopmentState.load(state_path)
    state.slug = ctx.slug
    state.work_dir = str(ctx.work_dir)
    state.init_criteria({"sim_pass": True, "_blocked_reason": False})
    state.set_criterion("_blocked_reason", True, detail={"reason": reason})
    state.save()
    return state_path


def _init_dirty_worktree(tmp_path: Path) -> tuple[Path, Path]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
    source = worktree / "rtl" / "dut.sv"
    source.parent.mkdir()
    source.write_text("module dut; endmodule\n", encoding="utf-8")
    subprocess.run(["git", "add", "rtl/dut.sv"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, check=True)
    source.write_text("module dut; wire unfinished; endmodule\n", encoding="utf-8")
    return worktree, source


def _porcelain_status(worktree: Path) -> bytes:
    return subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
    ).stdout


def _patch_worktree_mutators(monkeypatch) -> list[MagicMock]:
    mutators = [MagicMock() for _ in range(4)]
    for name, mock in zip(
        (
            "_commit_ticket_paths",
            "_reset_worktree_if_dirty",
            "_restore_worktree",
            "_clean_worktree",
        ),
        mutators,
        strict=True,
    ):
        monkeypatch.setattr(developer, name, mock, raising=False)
    return mutators


def _assert_preserved_dirty_handoff(
    ctx: TicketContext,
    board_block: MagicMock,
    source: Path,
    before_bytes: bytes,
    before_status: bytes,
    mutators: list[MagicMock],
) -> None:
    assert board_block.call_args.kwargs["reason"] == (
        "Need the maintainer to choose the reset behavior."
    )
    blocked_log = (ctx.logs_dir / "blocked.md").read_text(encoding="utf-8")
    assert blocked_log.count("## Run 0 -- Blocked") == 1
    assert "**Reason:** Need the maintainer to choose the reset behavior." in blocked_log
    assert "### Secondary context" in blocked_log
    assert "1 uncommitted file(s) preserved: M rtl/dut.sv" in blocked_log
    assert source.read_bytes() == before_bytes
    assert _porcelain_status(ctx.work_dir) == before_status
    for mutator in mutators:
        mutator.assert_not_called()


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
async def test_blocked_reason_and_dirty_preserves_primary_and_worktree(
    tmp_path: Path, monkeypatch
):
    worktree, source = _init_dirty_worktree(tmp_path)
    ctx = _context(
        tmp_path,
        worktree_path=worktree,
        scope_raw=["rtl/dut.sv"],
    )
    state_path = ticket_runtime_file(ctx.logs_dir, "booley_state.json")
    state = DevelopmentState.load(state_path)
    state.slug = ctx.slug
    state.work_dir = str(worktree)
    state.init_criteria({"sim_pass": True, "_blocked_reason": False})
    state.set_criterion(
        "_blocked_reason",
        True,
        detail={"reason": "Need the maintainer to choose the reset behavior."},
    )
    state.save()

    before_bytes = source.read_bytes()
    before_status = _porcelain_status(worktree)

    real_guardrails = developer._run_post_guardrails
    _patch_developer_path(
        monkeypatch,
        tmp_path,
        agent_result=AgentResult(output="blocked", input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(developer, "_run_post_guardrails", real_guardrails)
    monkeypatch.setattr(developer, "_report_scope_deviations", MagicMock())
    board_block = MagicMock()
    monkeypatch.setattr(developer.ticket_cli, "block", board_block)
    mutators = _patch_worktree_mutators(monkeypatch)

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "blocked"
    _assert_preserved_dirty_handoff(
        ctx,
        board_block,
        source,
        before_bytes,
        before_status,
        mutators,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["guardrail", "hook"])
async def test_post_developer_block_returns_blocked(tmp_path: Path, monkeypatch, boundary: str):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    _patch_developer_path(
        monkeypatch,
        tmp_path,
        agent_result=AgentResult(output="done", input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(developer, "block_ticket", MagicMock())
    if boundary == "guardrail":
        monkeypatch.setattr(
            developer,
            "_run_post_guardrails",
            lambda *_args: developer.PostDeveloperFinding("handoff failed", "handoff failed"),
        )
    else:
        monkeypatch.setattr(
            developer,
            "_run_post_developer_hook",
            lambda *_args, **_kwargs: developer.PostDeveloperFinding("hook failed", "hook failed"),
        )

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "blocked"


@pytest.mark.asyncio
async def test_post_hook_finding_reprojects_declared_reason(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    _write_active_block(ctx, "Need a maintainer decision.")
    _patch_developer_path(
        monkeypatch,
        tmp_path,
        agent_result=AgentResult(output="blocked", input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(
        developer,
        "_run_post_developer_hook",
        lambda *_args, **_kwargs: developer.PostDeveloperFinding(
            "post-developer hook: failed", "post-developer hook: failed"
        ),
    )
    block = MagicMock()
    monkeypatch.setattr(developer, "block_ticket", block)

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "blocked"
    block.assert_called_once_with(
        ctx,
        "Need a maintainer decision.",
        "developer",
        run_index=0,
        secondary_context=["post-developer hook: failed"],
    )


@pytest.mark.asyncio
async def test_post_drain_path_error_preserves_declared_reason(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    _write_active_block(ctx, "Need a maintainer decision.")
    _patch_developer_path(
        monkeypatch,
        tmp_path,
        agent_result=AgentResult(output="blocked", input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(
        developer,
        "_record_agent_result",
        MagicMock(side_effect=RuntimeError("bookkeeping failed")),
    )
    block = MagicMock()
    retry = MagicMock(return_value=False)
    monkeypatch.setattr(developer, "block_ticket", block)
    monkeypatch.setattr(developer, "maybe_auto_retry", retry)

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "blocked"
    developer.record_crash.assert_called_once()
    retry.assert_called_once()
    assert block.call_args.args[1] == "Need a maintainer decision."
    assert block.call_args.kwargs["secondary_context"] == [
        "Developer Agent path error: RuntimeError: bookkeeping failed"
    ]


def test_post_drain_projection_error_preserves_failure_transition(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    failure = MagicMock()
    monkeypatch.setattr(developer, "record_crash", MagicMock())
    monkeypatch.setattr(developer, "fail_ticket", failure)
    monkeypatch.setattr(developer.ticket_cli, "ticket_status", MagicMock(return_value="running"))
    monkeypatch.setattr(
        "booley.ticket_board.criteria_acceptance.project_active_declared_block_reason",
        MagicMock(side_effect=OSError("state unreadable")),
    )

    result = developer._classify_developer_path_failure(
        ctx,
        tmp_path,
        0,
        RuntimeError("bookkeeping failed"),
        state_path=tmp_path / "booley_state.json",
        jobs_drained=True,
    )

    assert result.disposition == "failed"
    failure.assert_called_once()


@pytest.mark.asyncio
async def test_disposition_failure_does_not_use_declared_reason_precedence(
    tmp_path: Path, monkeypatch
):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    _write_active_block(ctx, "Need a maintainer decision.")
    _patch_developer_path(
        monkeypatch,
        tmp_path,
        agent_result=AgentResult(output="blocked", input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(
        developer,
        "_resolve_ticket_disposition",
        AsyncMock(side_effect=RuntimeError("disposition failed")),
    )
    block = MagicMock()
    failure = MagicMock()
    monkeypatch.setattr(developer, "block_ticket", block)
    monkeypatch.setattr(developer, "fail_ticket", failure)

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "failed"
    block.assert_not_called()
    failure.assert_called_once()


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
    monkeypatch.setattr(
        developer,
        "_run_post_guardrails",
        lambda *_args: developer.PostDeveloperFinding("handoff failed", "handoff failed"),
    )
    monkeypatch.setattr(developer, "block_ticket", MagicMock())

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


def test_path_failure_best_effort_errors_still_return_failed(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path, worktree_path=tmp_path / "worktree")
    monkeypatch.setattr(
        developer, "record_crash", MagicMock(side_effect=RuntimeError("record failed"))
    )
    monkeypatch.setattr(
        developer.ticket_cli,
        "ticket_status",
        MagicMock(side_effect=RuntimeError("status failed")),
    )
    failure = MagicMock(side_effect=RuntimeError("transition failed"))
    monkeypatch.setattr(developer, "fail_ticket", failure)

    result = developer._classify_developer_path_failure(
        ctx, tmp_path, 0, RuntimeError("developer failed")
    )

    assert result.disposition == "failed"
    failure.assert_called_once()


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
    monkeypatch.setattr(
        developer,
        "_run_post_guardrails",
        lambda *_args: developer.PostDeveloperFinding("handoff failed", "handoff failed"),
    )
    monkeypatch.setattr(
        developer, "maybe_auto_retry", MagicMock(side_effect=RuntimeError("finalizer failed"))
    )
    monkeypatch.setattr(developer, "block_ticket", MagicMock())

    result = await developer._run_developer_path(ctx, tmp_path)

    assert result.disposition == "blocked"
