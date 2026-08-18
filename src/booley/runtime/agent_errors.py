"""Agent-runtime errors and terminal limit classification."""

from __future__ import annotations

import re

LIMIT_PATTERNS = [
    re.compile(r"you.?ve hit your limit", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"subscription limit", re.IGNORECASE),
    re.compile(r"SubscriptionLimitError", re.IGNORECASE),
    re.compile(r"you.?ve reached your .* limit", re.IGNORECASE),
    re.compile(r"quota exceeded", re.IGNORECASE),
]

CONTEXT_EXHAUSTION_PATTERNS = [
    re.compile(r"context.?length.?exceed", re.IGNORECASE),
    re.compile(r"maximum.?context.?length", re.IGNORECASE),
    re.compile(r"token.?limit.?exceed", re.IGNORECASE),
    re.compile(r"too many tokens", re.IGNORECASE),
    re.compile(r"prompt is too long", re.IGNORECASE),
    re.compile(r"max_tokens.*exceeded", re.IGNORECASE),
    re.compile(r"input.*too.*long", re.IGNORECASE),
    re.compile(r"exceeds.*context.*window", re.IGNORECASE),
]


def is_usage_limit(text: str) -> bool:
    """Return whether *text* matches a usage or subscription limit."""
    return any(pattern.search(text) for pattern in LIMIT_PATTERNS)


def is_context_exhausted(text: str) -> bool:
    """Return whether *text* indicates context-window exhaustion."""
    return any(pattern.search(text) for pattern in CONTEXT_EXHAUSTION_PATTERNS)


class TransientAPIError(Exception):
    """Transient API/network error that is safe to retry with backoff."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class AgentTimeoutError(Exception):
    """Local agent runtime deadline reached; not retryable."""


class UsageLimitError(Exception):
    """Daily or subscription usage cap reached; not retryable."""

    def __init__(self, message: str, provider: str) -> None:
        self.provider = provider
        super().__init__(message)


class ContextExhaustedError(Exception):
    """Context window overflow; retrying would reproduce the failure."""

    def __init__(self, message: str, provider: str) -> None:
        self.provider = provider
        super().__init__(message)


class BlockingError(Exception):
    """A workflow step cannot proceed without corrective action."""

    def __init__(self, reason: str, questions: list[str] | None = None) -> None:
        self.reason = reason
        self.questions = questions
        super().__init__(reason)
