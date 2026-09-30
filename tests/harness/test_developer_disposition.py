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

from booley.criteria.state import DevelopmentState
from booley.harness.blocking import AgentTimeoutError
from booley.harness.developer import (
    PostDeveloperFinding,
    _resolve_ticket_disposition,
    _run_post_developer_hook,
    _transition_post_developer_finding,
)
from booley.harness.models import OnSuccess, TicketContext
from booley.ticket_board.criteria_acceptance import CriteriaVerdict
from booley.ticket_board.review_lifecycle import ReviewPrepError, ReviewPrepOutcome
from tests.criterion_endpoint_support import builtin_endpoint_catalog

_ENDPOINTS = builtin_endpoint_catalog()


def _make_ctx(tmp_path: Path) -> TicketContext:
    """Minimal TicketContext for disposition routing tests."""
    return TicketContext(
        slug="t-test-0001",
        ticket_path=tmp_path / "t-test-0001.md",
        ticket_type="feature",
        branch="main",
        summary="test ticket",
        project_root=tmp_path,
        on_success=OnSuccess(destination="review"),
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
            "booley.ticket_board.review_lifecycle.prepare_review",
            new_callable=AsyncMock,
        ),
        "verify_review": patch(
            "booley.ticket_board.review_lifecycle.verify_review_handoff",
        ),
        "basis": patch(
            "booley.harness.developer._block_changed_ticket_baseline",
            return_value=False,
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
    mocks["handoff"].return_value = None
    mocks["prepare_review"].return_value = ReviewPrepOutcome(
        "ready",
        "prepared",
        package_path=Path("/tmp/review-package.json"),
    )
    mocks["verify_review"].return_value = ReviewPrepOutcome(
        "fresh",
        "current",
        package_path=Path("/tmp/review-package.json"),
    )
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
            "ready",
            "HTML explanation prepared",
            tmp_path / "report.html",
            tmp_path / "review-package.json",
        )
        mocks["verify_review"].return_value = mocks["prepare_review"].return_value
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 0, _ENDPOINTS
            )
            assert mocks["handoff"].call_count == 1
            assert mocks["prepare_review"].await_count == 1
            assert mocks["block"].call_count == 0
            assert mocks["fail"].call_count == 0
            assert mocks["archive"].call_count == 0
            assert result is not None
            assert result.slug == ctx.slug
            assert result.review_package_path == tmp_path / "review-package.json"
            assert result.html_path == tmp_path / "report.html"
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_changed_baseline_returns_blocked_result(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        mocks, patches = _patch_disposition_collaborators(CriteriaVerdict(disposition="review"))
        mocks["basis"].return_value = True
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 0, _ENDPOINTS
            )
            assert result.disposition == "blocked"
            mocks["handoff"].assert_not_called()
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_review_handoff_exception_is_not_reclassified(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        mocks, patches = _patch_disposition_collaborators(CriteriaVerdict(disposition="review"))
        mocks["handoff"].side_effect = RuntimeError("handoff failed")
        try:
            with pytest.raises(RuntimeError, match="handoff failed"):
                await _resolve_ticket_disposition(
                    ctx, tmp_path / "state.json", tmp_path, 0, _ENDPOINTS
                )
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
            return ReviewPrepOutcome(
                "ready",
                "prepared",
                tmp_path / "report.html",
                tmp_path / "review-package.json",
            )

        mocks["prepare_review"].side_effect = prepare
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 3, _ENDPOINTS
            )
            assert result.disposition == "review"
            assert mocks["handoff"].call_count == 1
            assert mocks["block"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_review_preparation_exception_returns_blocked(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        mocks, patches = _patch_disposition_collaborators(CriteriaVerdict(disposition="review"))
        mocks["prepare_review"].side_effect = ReviewPrepError("inputs disappeared")
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 3, _ENDPOINTS
            )
            assert result.disposition == "blocked"
            mocks["block"].assert_called_once()
            mocks["handoff"].assert_not_called()
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_review_verification_exception_returns_blocked(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        mocks, patches = _patch_disposition_collaborators(CriteriaVerdict(disposition="review"))
        mocks["verify_review"].side_effect = ReviewPrepError("package changed")
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 3, _ENDPOINTS
            )
            assert result.disposition == "blocked"
            mocks["block"].assert_called_once()
            mocks["handoff"].assert_not_called()
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_html_omission_does_not_block_review_handoff(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        verdict = CriteriaVerdict(disposition="review")
        mocks, patches = _patch_disposition_collaborators(verdict)
        mocks["prepare_review"].return_value = ReviewPrepOutcome(
            "ready",
            "review briefing prepared; HTML explanation unavailable",
            package_path=tmp_path / "review-package.json",
        )
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 3, _ENDPOINTS
            )
            assert result.disposition == "review"
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
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 3, _ENDPOINTS
            )
            assert result.disposition == "blocked"
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
        ("on_success", "expected_preparations", "expected_disposition"),
        [
            (OnSuccess(destination="review", triage_report=False), 1, "review"),
            (OnSuccess(destination="done", triage_report=True), 0, "done"),
        ],
    )
    async def test_review_prepares_package_only_when_landing_in_review(
        self,
        tmp_path: Path,
        on_success: OnSuccess,
        expected_preparations: int,
        expected_disposition: str,
    ):
        ctx = _make_ctx(tmp_path)
        ctx.on_success = on_success
        mocks, patches = _patch_disposition_collaborators(CriteriaVerdict(disposition="review"))
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 0, _ENDPOINTS
            )
            assert mocks["handoff"].call_count == 1
            assert mocks["prepare_review"].await_count == expected_preparations
            assert result.disposition == expected_disposition
            if expected_disposition == "done":
                assert result.review_package_path is None
                assert result.html_path is None
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
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 0, _ENDPOINTS
            )
            assert result.disposition == "blocked"
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
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 0, _ENDPOINTS
            )
            assert result.disposition == "failed"
            assert mocks["fail"].call_count == 1
            assert mocks["block"].call_count == 0
            assert mocks["handoff"].call_count == 0
            # Core invariant: failed verdict must NOT auto-archive the ticket.
            assert mocks["archive"].call_count == 0
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    async def test_verified_review_without_package_path_blocks_handoff(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        mocks, patches = _patch_disposition_collaborators(CriteriaVerdict(disposition="review"))
        mocks["verify_review"].return_value = ReviewPrepOutcome("fresh", "current")
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 0, _ENDPOINTS
            )
            assert result.disposition == "blocked"
            mocks["block"].assert_called_once()
            mocks["handoff"].assert_not_called()
        finally:
            _stop_all(patches)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "expected", "expected_failures"),
        [("running", "failed", 1), ("review", "failed", 0), ("done", "done", 0)],
    )
    async def test_done_handoff_reconciles_partial_completion(
        self, tmp_path: Path, status: str, expected: str, expected_failures: int
    ):
        ctx = _make_ctx(tmp_path)
        ctx.on_success = OnSuccess(destination="done")
        mocks, patches = _patch_disposition_collaborators(CriteriaVerdict(disposition="review"))
        mocks["handoff"].side_effect = RuntimeError("completion failed")
        status_patch = patch(
            "booley.harness.developer.ticket_cli.ticket_status", return_value=status
        )
        status_mock = status_patch.start()
        try:
            result = await _resolve_ticket_disposition(
                ctx, tmp_path / "state.json", tmp_path, 0, _ENDPOINTS
            )
            assert result.disposition == expected
            status_mock.assert_called_once_with(tmp_path, ctx.slug)
            mocks["block"].assert_not_called()
            assert mocks["fail"].call_count == expected_failures
        finally:
            status_patch.stop()
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
                    _ENDPOINTS,
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
                    _ENDPOINTS,
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
        finding = _run_post_developer_hook(
            ctx,
            tmp_path / "state.json",
            tmp_path / "logs",
            run_index=4,
        )

    assert finding is not None
    assert finding.reason == "post-developer hook: bad rtl"
    block.assert_not_called()


def test_post_developer_hook_state_mutation_is_reprojected(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    ctx.worktree_path = tmp_path / "worktree"
    ctx.worktree_path.mkdir()
    hook = tmp_path / ".booley_project" / "hooks" / "post-developer.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("raise SystemExit(2)\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria({"implementation_complete": True, "_blocked_reason": False})
    state.set_criterion("_blocked_reason", True, detail={"reason": "Stale pre-hook reason."})
    state.save()

    def clear_reason(*_args, **_kwargs):
        updated = DevelopmentState.load(state_path)
        updated.set_criterion("_blocked_reason", False)
        updated.save()
        return MagicMock(returncode=2, stdout="", stderr="hook failed")

    with (
        patch("booley.harness.developer.subprocess.run", side_effect=clear_reason),
        patch.dict(os.environ, {"BOOLEY_PROJECT_DIR": ""}, clear=False),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        finding = _run_post_developer_hook(
            ctx,
            state_path,
            tmp_path / "logs",
            run_index=5,
        )
        assert finding is not None
        _transition_post_developer_finding(ctx, state_path, finding, 5)

    block.assert_called_once_with(
        ctx,
        "post-developer hook: hook failed",
        "developer",
        run_index=5,
    )


def test_transition_projection_error_uses_guard_reason(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    finding = PostDeveloperFinding("handoff failed", "handoff context")
    with (
        patch(
            "booley.ticket_board.criteria_acceptance.project_active_declared_block_reason",
            side_effect=UnicodeDecodeError("utf-8", b"x", 0, 1, "invalid"),
        ),
        patch("booley.harness.developer.block_ticket") as block,
    ):
        _transition_post_developer_finding(ctx, tmp_path / "state.json", finding, 5)

    block.assert_called_once_with(ctx, "handoff failed", "developer", run_index=5)


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
