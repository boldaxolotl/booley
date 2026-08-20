"""Tests for harness.developer._resolve_ticket_disposition.

Locks in the verdict → board-status mapping and the invariant that the
harness MUST NOT auto-archive (delete) tickets. Archive is a human-only
operation — see booley.ticket_board.archive.op_archive, invoked only by
ticket-triage.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from booley.harness.blocking import AgentTimeoutError
from booley.harness.developer import _resolve_ticket_disposition, _run_post_developer_hook
from booley.harness.models import OnSuccess, TicketContext
from booley.harness.review_prep import ReviewPrepOutcome
from booley.ticket_board.criteria_acceptance import CriteriaVerdict


def _make_ctx(tmp_path: Path) -> TicketContext:
    """Minimal TicketContext for disposition routing tests."""
    return TicketContext(
        slug="t-test-0001",
        ticket_path=tmp_path / "t-test-0001.md",
        ticket_type="feature",
        branch="main",
        summary="test ticket",
        project_root=tmp_path,
    )


def _patch_disposition_collaborators(verdict: CriteriaVerdict):
    """Patch every side-effecting collaborator of _resolve_ticket_disposition.

    Returns (mocks, patches). Note that ``check_criteria_acceptance`` and
    ``build_criteria_summary_lines`` are imported lazily *inside* the function,
    so they must be patched at their source module rather than on
    ``booley.harness.developer``.

    Critically includes op_archive so we can prove it is never invoked from
    the developer-side disposition path.
    """
    patches = {
        "verdict": patch(
            "booley.ticket_board.criteria_acceptance.check_criteria_acceptance",
            return_value=verdict,
        ),
        "summary": patch(
            "booley.ticket_board.criteria_acceptance.build_criteria_summary_lines",
            return_value=([], ""),
        ),
        "block": patch("booley.harness.developer.block_ticket"),
        "fail": patch("booley.harness.developer.fail_ticket"),
        "handoff": patch("booley.harness.developer.ticket_cli.handoff"),
        "prepare_review": patch(
            "booley.harness.review_prep.prepare_review",
            new_callable=AsyncMock,
        ),
        "verify_review": patch(
            "booley.harness.review_prep.verify_review_handoff",
        ),
        # Silence terminal output so tests don't spam stdout.
        "terminal_raw": patch("booley.harness.developer.terminal.raw"),
        "terminal_crit": patch(
            "booley.harness.developer.terminal.criteria_summary",
        ),
        # Auto-archive guard: any direct call to op_archive from the
        # disposition path is a regression of the user-consent invariant.
        "archive": patch("booley.ticket_board.archive.op_archive"),
    }
    mocks = {name: p.start() for name, p in patches.items()}
    return mocks, patches


def _stop_all(patches: dict) -> None:
    for p in patches.values():
        p.stop()


class TestResolveTicketDisposition:
    """Verdict → action mapping in _resolve_ticket_disposition."""

    @pytest.mark.asyncio
    async def test_review_calls_handoff_only(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        verdict = CriteriaVerdict(disposition="review")
        mocks, patches = _patch_disposition_collaborators(verdict)
        mocks["prepare_review"].return_value = ReviewPrepOutcome(
            "ready", "HTML explanation prepared", tmp_path / "report.html"
        )
        try:
            await _resolve_ticket_disposition(ctx, tmp_path / "state.json", tmp_path, 0)
            assert mocks["handoff"].call_count == 1
            assert mocks["prepare_review"].await_count == 1
            assert mocks["block"].call_count == 0
            assert mocks["fail"].call_count == 0
            assert mocks["archive"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_post_processing_runs_before_review_handoff(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        verdict = CriteriaVerdict(disposition="review")
        mocks, patches = _patch_disposition_collaborators(verdict)

        async def prepare(*_args, **_kwargs):
            status_path = ctx.logs_dir / ".runtime" / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            assert status["step"] == "post-processing"
            assert mocks["handoff"].call_count == 0
            return ReviewPrepOutcome("ready", "prepared", tmp_path / "report.html")

        mocks["prepare_review"].side_effect = prepare
        try:
            await _resolve_ticket_disposition(ctx, tmp_path / "state.json", tmp_path, 3)
            assert mocks["handoff"].call_count == 1
            assert mocks["block"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_html_omission_does_not_block_review_handoff(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        verdict = CriteriaVerdict(disposition="review")
        mocks, patches = _patch_disposition_collaborators(verdict)
        mocks["prepare_review"].return_value = ReviewPrepOutcome(
            "ready", "review briefing prepared; HTML explanation unavailable"
        )
        try:
            await _resolve_ticket_disposition(ctx, tmp_path / "state.json", tmp_path, 3)
            assert mocks["handoff"].call_count == 1
            assert mocks["block"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_post_processing_failure_blocks_handoff(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        verdict = CriteriaVerdict(disposition="review")
        mocks, patches = _patch_disposition_collaborators(verdict)
        mocks["prepare_review"].return_value = ReviewPrepOutcome(
            "changed", "live review inputs changed concurrently"
        )
        try:
            await _resolve_ticket_disposition(ctx, tmp_path / "state.json", tmp_path, 3)
            assert mocks["block"].call_count == 1
            assert (
                mocks["block"]
                .call_args.args[1]
                .startswith("Review post-processing did not complete:")
            )
            assert mocks["block"].call_args.args[2] == "post-processing"
            assert mocks["handoff"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "on_success",
        [
            OnSuccess(triage_report=False),
            OnSuccess(destination="done", triage_report=True),
        ],
    )
    async def test_review_skips_preparation_when_not_applicable(
        self, tmp_path: Path, on_success: OnSuccess
    ):
        ctx = _make_ctx(tmp_path)
        ctx.on_success = on_success
        mocks, patches = _patch_disposition_collaborators(CriteriaVerdict(disposition="review"))
        try:
            await _resolve_ticket_disposition(ctx, tmp_path / "state.json", tmp_path, 0)
            assert mocks["handoff"].call_count == 1
            assert mocks["prepare_review"].await_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_blocked_calls_block_only(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        verdict = CriteriaVerdict(
            disposition="blocked",
            blocked_reason="needs human input",
        )
        mocks, patches = _patch_disposition_collaborators(verdict)
        try:
            await _resolve_ticket_disposition(ctx, tmp_path / "state.json", tmp_path, 0)
            assert mocks["block"].call_count == 1
            assert mocks["handoff"].call_count == 0
            assert mocks["fail"].call_count == 0
            assert mocks["archive"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_failed_calls_fail_only(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        verdict = CriteriaVerdict(
            disposition="failed",
            unmet_mandatory=["sim_pass", "lint_clean"],
        )
        mocks, patches = _patch_disposition_collaborators(verdict)
        try:
            await _resolve_ticket_disposition(ctx, tmp_path / "state.json", tmp_path, 0)
            assert mocks["fail"].call_count == 1
            assert mocks["block"].call_count == 0
            assert mocks["handoff"].call_count == 0
            # Core invariant: failed verdict must NOT auto-archive the ticket.
            assert mocks["archive"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_unknown_disposition_raises(self, tmp_path: Path):
        """Defensive: an unrecognised disposition must fail loudly, not
        silently default to one of the existing branches (which was the
        original c-vga-controller-0001 bug — 'archived' fell into else)."""
        ctx = _make_ctx(tmp_path)
        verdict = CriteriaVerdict(disposition="archived")  # legacy/unknown
        mocks, patches = _patch_disposition_collaborators(verdict)
        try:
            with pytest.raises(ValueError, match="Unknown criteria verdict"):
                await _resolve_ticket_disposition(
                    ctx,
                    tmp_path / "state.json",
                    tmp_path,
                    0,
                )
            assert mocks["fail"].call_count == 0
            assert mocks["block"].call_count == 0
            assert mocks["handoff"].call_count == 0
            assert mocks["archive"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_no_disposition_path_invokes_op_archive(self, tmp_path: Path):
        """Sweep every valid disposition; op_archive must never be called.

        Encodes the project rule: archiving (= deleting) tickets requires
        explicit human consent via ticket-triage. The harness, on its own,
        never touches op_archive.
        """
        verdicts = [
            CriteriaVerdict(disposition="review"),
            CriteriaVerdict(disposition="blocked", blocked_reason="x"),
            CriteriaVerdict(disposition="failed", unmet_mandatory=["sim_pass"]),
        ]
        for verdict in verdicts:
            ctx = _make_ctx(tmp_path)
            mocks, patches = _patch_disposition_collaborators(verdict)
            try:
                await _resolve_ticket_disposition(
                    ctx,
                    tmp_path / "state.json",
                    tmp_path,
                    0,
                )
                assert mocks["archive"].call_count == 0, (
                    f"op_archive was called for disposition={verdict.disposition!r} "
                    f"— harness must never auto-archive tickets"
                )
            finally:
                _stop_all(patches)


def test_post_developer_hook_failure_returns_blocked(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    hook = tmp_path / ".booley_project" / "hooks" / "post-developer.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("raise SystemExit(2)\n", encoding="utf-8")

    with (
        patch(
            "booley.harness.developer.subprocess.run",
            return_value=MagicMock(returncode=2, stdout="", stderr="bad rtl"),
        ),
        patch.dict(os.environ, {"BOOLEY_PROJECT_DIR": ""}, clear=False),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_developer_hook(
            ctx,
            tmp_path / "state.json",
            tmp_path / "logs",
            run_index=4,
        )

    assert blocked is True
    block.assert_called_once()
    assert block.call_args.args[1] == "post-developer hook: bad rtl"


def test_post_developer_hook_is_bounded_by_remaining_wall_time(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    hook = tmp_path / ".booley_project" / "hooks" / "post-developer.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("pass\n", encoding="utf-8")
    budget = MagicMock()
    budget.remaining_wall_seconds.return_value = 12.5
    budget.timeout_error.return_value = AgentTimeoutError("wall limit reached")

    with (
        patch(
            "booley.harness.developer.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["python", str(hook)], 12.5),
        ) as run,
        patch.dict(os.environ, {"BOOLEY_PROJECT_DIR": ""}, clear=False),
        pytest.raises(AgentTimeoutError, match="wall limit reached"),
    ):
        _run_post_developer_hook(
            ctx,
            tmp_path / "state.json",
            tmp_path / "logs",
            run_index=4,
            budget=budget,
        )

    assert run.call_args.kwargs["timeout"] == 12.5


def test_expired_wall_budget_does_not_create_hook_failure(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    hook = tmp_path / ".booley_project" / "hooks" / "post-developer.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("pass\n", encoding="utf-8")
    budget = MagicMock()
    budget.raise_if_exhausted.side_effect = AgentTimeoutError("wall limit reached")

    with (
        patch("booley.harness.developer.subprocess.run") as run,
        patch("booley.harness.developer.block_ticket") as block,
        patch.dict(os.environ, {"BOOLEY_PROJECT_DIR": ""}, clear=False),
        pytest.raises(AgentTimeoutError, match="wall limit reached"),
    ):
        _run_post_developer_hook(
            ctx,
            tmp_path / "state.json",
            tmp_path / "logs",
            run_index=4,
            budget=budget,
        )

    run.assert_not_called()
    block.assert_not_called()
