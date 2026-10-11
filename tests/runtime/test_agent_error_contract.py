"""Agent runtime errors and usage-limit classification."""

import pytest

from booley.runtime.agent_errors import (
    AgentTimeoutError,
    BlockingError,
    TransientAPIError,
    UsageLimitError,
    is_usage_limit,
)


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
