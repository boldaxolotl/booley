"""Tests for harness.auto_retry — transient-crash detection and auto-requeue.

Covers the three guardrails the design rests on: a conservative signature
match, a bounded per-ticket budget, and an audit trail (incident + transition)
on every auto-retry.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.harness.auto_retry import (
    DEFAULT_MAX_ATTEMPTS,
    _crashes_path,
    _load_crashes,
    maybe_auto_retry,
    record_crash,
)
from booley.harness.blocking import classify_transient_crash
from booley.harness.models import TicketContext

STALL = "Developer Agent error: APIError: API Error: Response stalled mid-stream"
CANCELLED = "Developer Agent cancelled: CancelledError: "


def _make_ctx(tmp_path: Path, monkeypatch) -> TicketContext:
    monkeypatch.delenv("TICKETS_DIR", raising=False)
    ctx = TicketContext(
        slug="t-test-0001",
        ticket_path=tmp_path / "t-test-0001.md",
        ticket_type="feature",
        branch="main",
        summary="test ticket",
        project_root=tmp_path,
    )
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
    return ctx


@pytest.fixture
def ops(tmp_path):
    """Patch the ticket_cli calls auto_retry makes; ticket reads as blocked."""
    mock = MagicMock()
    mock.ticket_status.return_value = "blocked"
    mock.unblock.return_value = True
    with patch("booley.harness.auto_retry.ticket_cli", mock):
        yield mock


# -- Signature classification -----------------------------------------


class TestClassifyTransientCrash:
    def test_stream_stall_matches(self):
        assert classify_transient_crash(STALL) == "api_stream_stall"

    def test_hyphenless_variant_matches(self):
        assert classify_transient_crash("Response stalled midstream") == "api_stream_stall"

    def test_developer_cancellation_is_resumable(self):
        assert classify_transient_crash(CANCELLED) == "developer_cancelled"

    def test_unscoped_cancelled_error_is_not_resumable(self):
        assert classify_transient_crash("MCP tool failed: CancelledError") is None

    def test_ordinary_crash_does_not_match(self):
        assert classify_transient_crash("Developer Agent error: ValueError: bad shape") is None

    def test_usage_limit_never_transient(self):
        # Retrying a subscription cap just reproduces it.
        assert (
            classify_transient_crash("You've hit your limit; response stalled mid-stream") is None
        )

    def test_context_exhaustion_never_transient(self):
        assert classify_transient_crash("prompt is too long — response stalled mid-stream") is None


# -- Crash sidecar -----------------------------------------------------


class TestRecordCrash:
    def test_records_classified_entry(self, tmp_path, monkeypatch):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=2, reason=STALL)

        crashes = _load_crashes(ctx.logs_dir)
        assert len(crashes) == 1
        assert crashes[0]["run_index"] == 2
        assert crashes[0]["incident_type"] == "api_stream_stall"
        assert crashes[0]["auto_retried"] is False

    def test_non_transient_recorded_without_type(self, tmp_path, monkeypatch):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason="Developer Agent error: ValueError: x")
        assert _load_crashes(ctx.logs_dir)[0]["incident_type"] is None

    def test_appends_across_runs(self, tmp_path, monkeypatch):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        record_crash(ctx.logs_dir, run_index=2, reason=STALL)
        assert [c["run_index"] for c in _load_crashes(ctx.logs_dir)] == [1, 2]

    def test_corrupt_sidecar_is_ignored(self, tmp_path, monkeypatch):
        ctx = _make_ctx(tmp_path, monkeypatch)
        path = _crashes_path(ctx.logs_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        assert len(_load_crashes(ctx.logs_dir)) == 1


# -- Retry decision ----------------------------------------------------


class TestMaybeAutoRetry:
    def test_requeues_on_transient_crash(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)

        assert maybe_auto_retry(ctx, tmp_path, 1) is True
        ops.unblock.assert_called_once()
        assert ops.unblock.call_args.kwargs["actor"] == "auto-retry"

    def test_requeues_cancelled_developer_for_safe_resume(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=CANCELLED)

        assert maybe_auto_retry(ctx, tmp_path, 1) is True
        assert ops.log_incident.call_args.kwargs["incident_type"] == "developer_cancelled"
        feedback = ops.unblock.call_args.kwargs["feedback"]
        assert "Cancellation interrupted the session" in feedback
        assert "`submit_run_report`" in feedback

    def test_logs_incident_for_audit(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        maybe_auto_retry(ctx, tmp_path, 1)

        kwargs = ops.log_incident.call_args.kwargs
        assert kwargs["incident_type"] == "api_stream_stall"
        assert kwargs["step"] == "developer"
        assert kwargs["resolution"] == f"auto-retried (1/{DEFAULT_MAX_ATTEMPTS})"

    def test_marks_budget_consumed(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        maybe_auto_retry(ctx, tmp_path, 1)
        assert _load_crashes(ctx.logs_dir)[0]["auto_retried"] is True

    def test_budget_is_bounded(self, tmp_path, monkeypatch, ops):
        """A stall recurring on the same ticket goes to a human, not a loop."""
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 1) is True

        record_crash(ctx.logs_dir, run_index=2, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 2) is False
        assert ops.unblock.call_count == 1

    def test_ignores_crash_from_an_earlier_run(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 2) is False
        ops.unblock.assert_not_called()

    def test_no_retry_without_a_crash(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        assert maybe_auto_retry(ctx, tmp_path, 1) is False
        ops.unblock.assert_not_called()

    def test_no_retry_for_ordinary_crash(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason="Developer Agent error: ValueError: x")
        assert maybe_auto_retry(ctx, tmp_path, 1) is False
        ops.unblock.assert_not_called()

    def test_skips_ticket_that_is_not_blocked(self, tmp_path, monkeypatch, ops):
        """A recovered run can still reach review/ — never redo finished work."""
        ctx = _make_ctx(tmp_path, monkeypatch)
        ops.ticket_status.return_value = "review"
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)

        assert maybe_auto_retry(ctx, tmp_path, 1) is False
        ops.unblock.assert_not_called()
        ops.log_incident.assert_not_called()

    def test_failed_requeue_leaves_budget_unspent(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        ops.unblock.return_value = False
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)

        assert maybe_auto_retry(ctx, tmp_path, 1) is False
        assert _load_crashes(ctx.logs_dir)[0]["auto_retried"] is False

    def test_never_raises(self, tmp_path, monkeypatch, ops):
        """Auto-retry is best-effort — a broken board must not kill the run."""
        ctx = _make_ctx(tmp_path, monkeypatch)
        ops.ticket_status.side_effect = RuntimeError("board unavailable")
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 1) is False


# -- Configuration -----------------------------------------------------


def _write_toml(tmp_path: Path, body: str) -> None:
    (tmp_path / ".booley_project").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".booley_project" / "booley.toml").write_text(body, encoding="utf-8")


class TestConfig:
    def test_disabled_falls_through_to_triage(self, tmp_path, monkeypatch, ops):
        _write_toml(tmp_path, "[developer.auto_retry]\nmax_attempts = 0\n")
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)

        assert maybe_auto_retry(ctx, tmp_path, 1) is False
        ops.unblock.assert_not_called()

    def test_raised_cap_allows_a_second_retry(self, tmp_path, monkeypatch, ops):
        _write_toml(tmp_path, "[developer.auto_retry]\nmax_attempts = 2\n")
        ctx = _make_ctx(tmp_path, monkeypatch)

        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 1) is True
        record_crash(ctx.logs_dir, run_index=2, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 2) is True
        record_crash(ctx.logs_dir, run_index=3, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 3) is False

    def test_bad_cap_value_uses_default(self, tmp_path, monkeypatch, ops):
        _write_toml(tmp_path, '[developer.auto_retry]\nmax_attempts = "lots"\n')
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 1) is True

    def test_absent_config_enables_with_default_cap(self, tmp_path, monkeypatch, ops):
        ctx = _make_ctx(tmp_path, monkeypatch)
        record_crash(ctx.logs_dir, run_index=1, reason=STALL)
        assert maybe_auto_retry(ctx, tmp_path, 1) is True


# -- Sidecar shape -----------------------------------------------------


def test_sidecar_lives_beside_the_run_transcripts(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, monkeypatch)
    record_crash(ctx.logs_dir, run_index=1, reason=STALL)
    path = _crashes_path(ctx.logs_dir)

    assert path.name == "crashes.json"
    assert path.parent.name == "developer"
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), list)
