"""Agent-runtime errors and terminal limit classification."""

from __future__ import annotations

import logging
import re

from booley.config.gateway import GATEWAY_MODEL_ENV

logger = logging.getLogger(__name__)

LIMIT_PATTERNS = [
    re.compile(r"you.?ve hit your limit", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"subscription limit", re.IGNORECASE),
    re.compile(r"SubscriptionLimitError", re.IGNORECASE),
    re.compile(r"you.?ve reached your .* limit", re.IGNORECASE),
    re.compile(r"quota exceeded", re.IGNORECASE),
    re.compile(r"you hit your spend cap", re.IGNORECASE),
    re.compile(r"your workspace is out of credits", re.IGNORECASE),
]

CONTEXT_EXHAUSTION_PATTERNS = [
    re.compile(r"context.?length.?exceed", re.IGNORECASE),
    re.compile(r"maximum.?context.?length", re.IGNORECASE),
    re.compile(r"token.?limit.?exceed", re.IGNORECASE),
    re.compile(r"too many tokens", re.IGNORECASE),
    re.compile(r"prompt is too long", re.IGNORECASE),
    re.compile(r"max_tokens.*exceeded", re.IGNORECASE),
    re.compile(r"input.*too.*long", re.IGNORECASE),
    re.compile(r"input[_ -]?too[_ -]?large", re.IGNORECASE),
    re.compile(r"exceeds.*context.*window", re.IGNORECASE),
]

# The gateway (or provider) rejected the credential: Codex reports
# "unexpected status 401 Unauthorized". 403 is a policy decision, not expiry.
AUTH_FAILURE_PATTERNS = [
    re.compile(r"\b401\b"),
    re.compile(r"unauthori[sz]ed", re.IGNORECASE),
]

# The provider refused the requested model: it does not exist or the account
# cannot use it. Each phrase is what one backend/CLI is observed to print.
MODEL_REFUSAL_PATTERNS = [
    re.compile(r"model_not_found"),
    re.compile(r"model is not supported", re.IGNORECASE),  # Codex with a ChatGPT login
    re.compile(r"issue with the selected model", re.IGNORECASE),  # Claude Code
    re.compile(r"does not exist or you do not have access", re.IGNORECASE),  # OpenAI API key
    re.compile(r"not_found_error[^\n]*model", re.IGNORECASE),  # raw Anthropic 404 body
]


def is_usage_limit(text: str) -> bool:
    """Return whether *text* matches a usage or subscription limit."""
    return any(pattern.search(text) for pattern in LIMIT_PATTERNS)


def is_context_exhausted(text: str) -> bool:
    """Return whether *text* indicates context-window exhaustion."""
    return any(pattern.search(text) for pattern in CONTEXT_EXHAUSTION_PATTERNS)


def is_model_refusal(text: str) -> bool:
    """Return whether *text* reports that the provider refused the requested model."""
    return any(pattern.search(text) for pattern in MODEL_REFUSAL_PATTERNS)


def is_auth_failure(text: str) -> bool:
    """Return whether *text* reports a rejected credential (HTTP 401)."""
    return any(pattern.search(text) for pattern in AUTH_FAILURE_PATTERNS)


class TransientAPIError(Exception):
    """Transient API/network error that is safe to retry with backoff."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class AgentTimeoutError(Exception):
    """Local agent runtime deadline reached; not retryable."""


class AgentProviderError(RuntimeError):
    """Terminal provider-process failure not covered by a narrower error type."""

    def __init__(self, message: str, provider: str) -> None:
        self.provider = provider
        super().__init__(message)


class AgentAuthError(AgentProviderError):
    """The provider rejected the credential (HTTP 401); a fresh token may fix it."""


class ModelUnavailableError(AgentProviderError):
    """The provider refused the resolved model; retrying cannot help, a config change can.

    ``settings`` names every config key that resolves to ``model``; ``detail`` keeps
    the raw provider text, which stays out of the message shown to people.
    """

    def __init__(
        self, *, model: str, provider: str, settings: tuple[str, ...], detail: str
    ) -> None:
        self.model = model
        self.settings = settings
        self.detail = detail
        if settings:
            keys = " and ".join(f"`{key}`" for key in settings)
            fix = f"Set {keys} to a model the account can use."
        else:
            fix = (
                "It is Booley's built-in model for internal calls on that Agent CLI: use an "
                "account that can run it, or set `[agent] default` to another Agent CLI."
            )
        message = (
            f"The {provider} provider refused model '{model}': it does not exist or this "
            f"account cannot use it. {fix}"
        )
        if not settings or any(key != GATEWAY_MODEL_ENV for key in settings):
            message += " booley.toml is a Protected Input, so change it before Goal entry."
        super().__init__(message, provider)


def model_unavailable_error(model: str, provider: str, detail: str) -> ModelUnavailableError:
    """Build the actionable refusal error for *model*, logging the raw provider *detail*."""
    # Function-local: agent_config imports the backends, which import this module.
    from booley.config.agent import get_agent_settings

    logger.warning("Provider %s refused model %s: %s", provider, model, detail)
    settings = get_agent_settings().model_settings().get(model, ())
    return ModelUnavailableError(model=model, provider=provider, settings=settings, detail=detail)


class UsageLimitError(Exception):
    """Provider usage, spending, or credit cap reached; not retryable."""

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
