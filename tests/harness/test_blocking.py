"""Tests for harness.blocking -- block/fail helpers and error classes."""

from unittest.mock import patch

import pytest

from booley.harness.blocking import (
    AgentTimeoutError,
    BlockingError,
    FatalError,
    TransientAPIError,
    UsageLimitError,
    block_ticket,
    fail_ticket,
    is_usage_limit,
)

# -- Error classes ----------------------------------------------------


class TestTransientAPIError:
    def test_message_and_default_retry(self):
        err = TransientAPIError("rate limited")
        assert str(err) == "rate limited"
        assert err.retry_after is None

    def test_retry_after(self):
        err = TransientAPIError("slow down", retry_after=30)
        assert err.retry_after == 30


class TestAgentTimeoutError:
    def test_not_subclass_of_transient(self):
        assert not issubclass(AgentTimeoutError, TransientAPIError)


class TestBlockingError:
    def test_reason_only(self):
        err = BlockingError("need clarification")
        assert err.reason == "need clarification"
        assert err.questions is None

    def test_reason_with_questions(self):
        qs = ["What width?", "Which config?"]
        err = BlockingError("ambiguous spec", questions=qs)
        assert err.reason == "ambiguous spec"
        assert err.questions == qs


class TestUsageLimitError:
    def test_not_subclass_of_transient(self):
        assert not issubclass(UsageLimitError, TransientAPIError)

    def test_provider_field(self):
        err = UsageLimitError("you've hit your limit", provider="claude")
        assert err.provider == "claude"
        assert "you've hit your limit" in str(err)


class TestFatalError:
    def test_error_attr(self):
        err = FatalError("synthesis crashed")
        assert err.error == "synthesis crashed"


# -- block_ticket -----------------------------------------------------

FIXED_TS = "2026-04-03T12:00:00Z"


@patch("booley.harness.blocking.ticket_cli.block")
def test_block_ticket_without_questions(mock_block, sample_ctx):
    block_ticket(sample_ctx, reason="need info", step="analysis")

    mock_block.assert_called_once_with(
        sample_ctx.project_root,
        sample_ctx.slug,
        reason="need info",
        step="analysis",
    )
    # No questions file should be created
    assert not (sample_ctx.logs_dir / "questions.md").exists()


@patch("booley.harness.blocking.ticket_cli.block")
def test_block_ticket_with_questions(mock_block, sample_ctx):
    questions = ["What register width?", "Which config?"]
    block_ticket(
        sample_ctx,
        reason="ambiguous spec",
        step="implementation",
        questions=questions,
    )

    mock_block.assert_called_once_with(
        sample_ctx.project_root,
        sample_ctx.slug,
        reason="ambiguous spec",
        step="implementation",
    )

    bfile = sample_ctx.logs_dir / "blocked.md"
    assert bfile.exists()
    content = bfile.read_text(encoding="utf-8")

    assert "ambiguous spec" in content
    assert "implementation" in content
    assert "### Questions" in content
    assert "What register width?" in content
    assert "Which config?" in content


@patch("booley.harness.blocking.ticket_cli.block")
def test_block_ticket_with_run_index(mock_block, sample_ctx):
    block_ticket(sample_ctx, reason="stuck", step="developer", run_index=3)
    bfile = sample_ctx.logs_dir / "blocked.md"
    content = bfile.read_text(encoding="utf-8")
    assert "## Run 3 -- Blocked" in content


@patch("booley.harness.blocking.ticket_cli.block")
def test_block_ticket_setup_no_run_index(mock_block, sample_ctx):
    block_ticket(sample_ctx, reason="bad config", step="setup")
    bfile = sample_ctx.logs_dir / "blocked.md"
    content = bfile.read_text(encoding="utf-8")
    assert "## Setup -- Blocked" in content


# -- fail_ticket ------------------------------------------------------


@patch("booley.harness.blocking.ticket_cli.block")
def test_fail_ticket(mock_block, sample_ctx):
    fail_ticket(sample_ctx, error="synthesis OOM", step="synthesis")

    mock_block.assert_called_once_with(
        sample_ctx.project_root,
        sample_ctx.slug,
        reason="synthesis OOM",
        step="synthesis",
    )

    bfile = sample_ctx.logs_dir / "blocked.md"
    assert bfile.exists()
    content = bfile.read_text(encoding="utf-8")

    assert "### Error" in content
    assert "synthesis OOM" in content
    assert "## Setup -- Failed" in content


@patch("booley.harness.blocking.ticket_cli.block")
def test_fail_ticket_crashed(mock_block, sample_ctx):
    fail_ticket(sample_ctx, error="context blown", step="developer", run_index=2, crashed=True)
    bfile = sample_ctx.logs_dir / "blocked.md"
    content = bfile.read_text(encoding="utf-8")
    assert "## Run 2 -- Crashed" in content
    assert "context blown" in content


# -- append-only behavior ---------------------------------------------


@patch("booley.harness.blocking.ticket_cli.block")
def test_blocked_md_is_append_only(mock_block, sample_ctx):
    """block -> fail -> produces chronological log with both entries."""
    block_ticket(sample_ctx, reason="first issue", step="setup")
    fail_ticket(sample_ctx, error="second issue", step="developer", run_index=1)

    bfile = sample_ctx.logs_dir / "blocked.md"
    content = bfile.read_text(encoding="utf-8")

    assert content.index("first issue") < content.index("second issue")
    assert "## Setup -- Blocked" in content
    assert "## Run 1 -- Failed" in content


# -- is_usage_limit ---------------------------------------------------


class TestIsUsageLimit:
    @pytest.mark.parametrize(
        "text",
        [
            "you've hit your limit",
            "usage limit exceeded",
            "Codex error: You've hit your usage limit. Upgrade to Pro",
            "SubscriptionLimitError: too many requests",
            "subscription limit reached",
        ],
    )
    def test_matches(self, text):
        assert is_usage_limit(text)

    @pytest.mark.parametrize(
        "text",
        [
            "syntax error in module",
            "compilation failed",
            "assertion failed at time 100ns",
            "rate limit will reset at 8pm (UTC)",  # transient, not a usage cap
        ],
    )
    def test_no_match(self, text):
        assert not is_usage_limit(text)
