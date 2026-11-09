"""Agent runtime errors and usage-limit classification."""

import pytest

from booley.runtime.agent_errors import (
    AgentTimeoutError,
    TransientAPIError,
    UsageLimitError,
    is_usage_limit,
)


@pytest.mark.parametrize("error_type", [AgentTimeoutError, UsageLimitError])
def test_terminal_errors_are_not_retried_as_transient(error_type):
    assert not issubclass(error_type, TransientAPIError)


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
