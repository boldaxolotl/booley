#!/usr/bin/env python3
"""Unit tests for agent.py retry logic and rate limit handling.

Tests cover:
- _is_transient_error() classification
- _transcript_path_for_attempt() naming
- _handle_rate_limit_event() sleep + raise behavior
- call_agent() retry loop (mocked SDK)
"""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import from the scripts directory (one level up from unit/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reuse the stub SDK from conftest (already in sys.modules) to avoid
# replacing it with a different mock -- that breaks isinstance checks in
# agent.py which holds a reference to the original ProcessError class.
import claude_agent_sdk as _sdk


class _MockRateLimitInfo:
    def __init__(self, status, resets_at=None, rate_limit_type=None, utilization=None):
        self.status = status
        self.resets_at = resets_at
        self.rate_limit_type = rate_limit_type
        self.utilization = utilization


# Enrich the shared stub with rate-limit helpers if not already present
if not hasattr(_sdk, "_enriched_by_resilience"):
    _sdk.RateLimitEvent = type(
        "RateLimitEvent",
        (),
        {
            "__init__": lambda self, rate_limit_info: setattr(
                self, "rate_limit_info", rate_limit_info
            ),
        },
    )
    # Ensure ProcessError supports exit_code/stderr attributes
    _orig_pe = _sdk.ProcessError

    class _EnrichedProcessError(_orig_pe):
        def __init__(self, message, exit_code=None, stderr=None):
            self.exit_code = exit_code
            self.stderr = stderr
            if exit_code is not None:
                message = f"{message} (exit code: {exit_code})"
            if stderr:
                message = f"{message}\nError output: {stderr}"
            super().__init__(message)

    _sdk.ProcessError = _EnrichedProcessError
    _sdk._enriched_by_resilience = True
    # Also update agent.py's reference to ProcessError
    import booley.harness.agent as _agent_mod

    _agent_mod.ProcessError = _EnrichedProcessError

from booley.harness.agent import (
    _is_transient_error,
    _transcript_path_for_attempt,
)
from booley.harness.blocking import TransientAPIError

# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------


class TestIsTransientError:
    """Verify classification of exceptions as transient vs permanent."""

    def test_process_error_500(self):
        exc = _sdk.ProcessError(
            "Command failed", exit_code=1, stderr="API Error: 500 internal server error"
        )
        assert _is_transient_error(exc) is True

    def test_process_error_502(self):
        exc = _sdk.ProcessError("fail", exit_code=1, stderr="502 Bad Gateway")
        assert _is_transient_error(exc) is True

    def test_process_error_503(self):
        exc = _sdk.ProcessError("fail", exit_code=1, stderr="503 Service Unavailable")
        assert _is_transient_error(exc) is True

    def test_process_error_529_overloaded(self):
        exc = _sdk.ProcessError("fail", exit_code=1, stderr="529 overloaded")
        assert _is_transient_error(exc) is True

    def test_process_error_connection_reset(self):
        exc = _sdk.ProcessError("fail", exit_code=1, stderr="ECONNRESET")
        assert _is_transient_error(exc) is True

    def test_process_error_timeout(self):
        exc = _sdk.ProcessError("fail", exit_code=1, stderr="ETIMEDOUT")
        assert _is_transient_error(exc) is True

    def test_process_error_stream_disconnected(self):
        """Codex mid-stream drop (reconnect ladder exhausted) is transient."""
        exc = _sdk.ProcessError(
            "fail", exit_code=1, stderr="stream disconnected before completion"
        )
        assert _is_transient_error(exc) is True

    def test_process_error_non_transient(self):
        exc = _sdk.ProcessError(
            "permission denied", exit_code=1, stderr="Error: authentication failed"
        )
        assert _is_transient_error(exc) is False

    def test_process_error_none_stderr(self):
        exc = _sdk.ProcessError("fail", exit_code=1, stderr=None)
        assert _is_transient_error(exc) is False

    def test_connection_error(self):
        exc = ConnectionError("Connection refused")
        assert _is_transient_error(exc) is True

    def test_os_error(self):
        exc = OSError("Network unreachable")
        assert _is_transient_error(exc) is True

    def test_regular_exception(self):
        exc = ValueError("bad input")
        assert _is_transient_error(exc) is False

    def test_runtime_error(self):
        exc = RuntimeError("something broke")
        assert _is_transient_error(exc) is False

    def test_api_error_json_in_message(self):
        """Real-world pattern: API error JSON in the exception message."""
        exc = _sdk.ProcessError(
            "Command failed",
            exit_code=1,
            stderr='API Error: 500 {"type":"error","error":{"type":"api_error",'
            '"message":"Internal server error"}}',
        )
        assert _is_transient_error(exc) is True

    def test_plain_exception_command_failed(self):
        """SDK raises plain Exception when subprocess crashes (API 500 lost in stderr)."""
        exc = Exception(
            "Command failed with exit code 1 (exit code: 1)\n"
            "Error output: Check stderr output for details"
        )
        assert _is_transient_error(exc) is True

    def test_plain_exception_unrelated(self):
        """Plain Exception with non-transient message should not match."""
        exc = Exception("Invalid JSON in agent output")
        assert _is_transient_error(exc) is False


# ---------------------------------------------------------------------------
# _transcript_path_for_attempt
# ---------------------------------------------------------------------------


class TestTranscriptPathForAttempt:
    """Verify transcript file naming across retry attempts."""

    def test_none_returns_none(self):
        assert _transcript_path_for_attempt(None, 1) is None
        assert _transcript_path_for_attempt(None, 3) is None

    def test_first_attempt_unchanged(self):
        p = Path("/logs/02-planning-draft.jsonl")
        assert _transcript_path_for_attempt(p, 1) == p

    def test_retry_2_suffix(self):
        p = Path("/logs/02-planning-draft.jsonl")
        result = _transcript_path_for_attempt(p, 2)
        assert result == Path("/logs/02-planning-draft-retry2.jsonl")

    def test_retry_3_suffix(self):
        p = Path("/logs/07-sim-debug-r1.jsonl")
        result = _transcript_path_for_attempt(p, 3)
        assert result == Path("/logs/07-sim-debug-r1-retry3.jsonl")


# ---------------------------------------------------------------------------
# _handle_rate_limit_event (async)
# ---------------------------------------------------------------------------


class TestHandleRateLimitEvent:
    """Verify rate limit event handling -- sleep + raise behavior."""

    @pytest.mark.asyncio
    async def test_rejected_with_resets_at(self):
        """Rejected status with resets_at should sleep then raise TransientAPIError."""
        from booley.harness.agent_backend import _handle_rate_limit_event

        future_ts = int(time.time()) + 5  # 5 seconds from now
        info = _MockRateLimitInfo(
            status="rejected",
            resets_at=future_ts,
            rate_limit_type="five_hour",
        )
        event = _sdk.RateLimitEvent(rate_limit_info=info)

        with (
            patch("booley.harness._claude_backend.anyio") as mock_anyio,
            patch("booley.harness._claude_backend._notify_rate_limit") as mock_notify,
        ):
            mock_anyio.sleep = AsyncMock()

            with pytest.raises(TransientAPIError) as exc_info:
                await _handle_rate_limit_event(event)

            # Should have slept (5s + 60s buffer = ~65s)
            mock_anyio.sleep.assert_awaited_once()
            sleep_arg = mock_anyio.sleep.call_args[0][0]
            assert 60 <= sleep_arg <= 70  # 5 + 60 buffer, with timing tolerance

            # Should have notified
            mock_notify.assert_called_once()

            # retry_after=0 because we already slept
            assert exc_info.value.retry_after == 0

    @pytest.mark.asyncio
    async def test_rejected_without_resets_at(self):
        """Rejected without resets_at should use fallback backoff."""
        from booley.harness.agent_backend import _handle_rate_limit_event
        from booley.harness.config import RATE_LIMIT_FALLBACK_BACKOFF_S

        info = _MockRateLimitInfo(status="rejected", resets_at=None, rate_limit_type="seven_day")
        event = _sdk.RateLimitEvent(rate_limit_info=info)

        with (
            patch("booley.harness._claude_backend.anyio") as mock_anyio,
            patch("booley.harness._claude_backend._notify_rate_limit"),
        ):
            mock_anyio.sleep = AsyncMock()

            with pytest.raises(TransientAPIError):
                await _handle_rate_limit_event(event)

            mock_anyio.sleep.assert_awaited_once_with(RATE_LIMIT_FALLBACK_BACKOFF_S)

    @pytest.mark.asyncio
    async def test_allowed_warning_logs_only(self):
        """allowed_warning should just log, not sleep or raise."""
        from booley.harness.agent_backend import _handle_rate_limit_event

        info = _MockRateLimitInfo(
            status="allowed_warning", rate_limit_type="five_hour", utilization=0.85
        )
        event = _sdk.RateLimitEvent(rate_limit_info=info)

        # Should not raise
        with patch("booley.harness._claude_backend.anyio") as mock_anyio:
            mock_anyio.sleep = AsyncMock()
            await _handle_rate_limit_event(event)
            mock_anyio.sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejected_wait_pauses_developer_active_budget(self):
        from booley.harness.agent_backend import _handle_rate_limit_event

        info = _MockRateLimitInfo(status="rejected", resets_at=None, rate_limit_type="seven_day")
        event = _sdk.RateLimitEvent(rate_limit_info=info)
        budget = MagicMock()

        async def complete_wait(awaitable, _budget):
            await awaitable

        with (
            patch("booley.harness._claude_backend.anyio.sleep", new_callable=AsyncMock),
            patch(
                "booley.harness._claude_backend.run_with_developer_budget",
                side_effect=complete_wait,
            ),
            patch("booley.harness._claude_backend._notify_rate_limit"),
            pytest.raises(TransientAPIError),
        ):
            await _handle_rate_limit_event(event, budget)

        budget.pause.assert_called_once_with("claude-rate-limit", "provider rate limit")
        budget.resume.assert_called_once_with("claude-rate-limit")


# ---------------------------------------------------------------------------
# TransientAPIError
# ---------------------------------------------------------------------------


class TestTransientAPIError:
    """Verify exception attributes."""

    def test_with_retry_after(self):
        e = TransientAPIError("rate limited", retry_after=120.0)
        assert e.retry_after == 120.0
        assert "rate limited" in str(e)

    def test_without_retry_after(self):
        e = TransientAPIError("server error")
        assert e.retry_after is None

    def test_zero_retry_after(self):
        e = TransientAPIError("already slept", retry_after=0)
        assert e.retry_after == 0


# ---------------------------------------------------------------------------
# _notify_rate_limit (fire-and-forget)
# ---------------------------------------------------------------------------


class TestNotifyRateLimit:
    """Verify notification helper is a silent no-op when ntfy unavailable."""

    def test_no_crash_on_missing_ntfy(self):
        """Should silently return if ticket_board.notifications not importable."""
        from booley.harness.agent_backend import _notify_rate_limit

        with patch.dict(
            sys.modules, {"booley.ticket_board": None, "booley.ticket_board.notifications": None}
        ):
            # Should not raise
            _notify_rate_limit("five_hour", 300.0, int(time.time()) + 300)

    def test_calls_ntfy_send(self):
        """Should call ntfy_send with appropriate title/body."""
        from booley.harness.agent_backend import _notify_rate_limit

        mock_ntfy = MagicMock()
        mock_ntfy.ntfy_send = MagicMock()
        mock_tb = MagicMock()
        mock_tb.notifications = mock_ntfy

        with patch.dict(
            sys.modules,
            {
                "booley.ticket_board": mock_tb,
                "booley.ticket_board.notifications": mock_ntfy,
            },
        ):
            _notify_rate_limit("five_hour", 300.0, int(time.time()) + 300)
            mock_ntfy.ntfy_send.assert_called_once()
            call_kwargs = mock_ntfy.ntfy_send.call_args
            assert (
                "rate-limited" in call_kwargs[1]["title"].lower()
                or "rate-limited" in call_kwargs[0][0].lower()
            )
