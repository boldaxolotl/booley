"""Terminal provider diagnostics: usage limits, context exhaustion, refused models."""

import pytest

from booley.runtime.agent_errors import is_context_exhausted, is_model_refusal, is_usage_limit

# Each text a provider prints, and whether the classifier must recognise it.
_TEXTS = {
    is_usage_limit: {
        "you've hit your limit": True,
        "usage limit exceeded": True,
        "SubscriptionLimitError: too many requests": True,
        "subscription limit reached": True,
        "assertion failed at time 100ns": False,
        "rate limit will reset at 8pm (UTC)": False,  # transient, not a usage cap
    },
    is_context_exhausted: {
        "turn/start input_too_large: max_chars=1048576, actual_chars=57231875": True,
    },
    is_model_refusal: {
        '{"status":400,"error":{"message":"The model does not exist","code":"model_not_found"}}': True,
        "The 'gpt-x' model is not supported when using Codex with a ChatGPT account.": True,
        "There's an issue with the selected model (claude-x). It may not exist or you may not have access to it.": True,
        "The model `gpt-x` does not exist or you do not have access to it.": True,
        '{"type":"error","error":{"type":"not_found_error","message":"model: claude-x"}}': True,
        "Model metadata for `gpt-x` not found. Defaulting to fallback metadata.": False,
        '{"status":400,"error":{"message":"Invalid request: tool schema rejected"}}': False,
        "unexpected status 401 Unauthorized: invalid token": False,
    },
}


@pytest.mark.parametrize(
    ("classify", "text", "expected"),
    [
        (classify, text, expected)
        for classify, texts in _TEXTS.items()
        for text, expected in texts.items()
    ],
)
def test_provider_text_classification(classify, text, expected):
    assert classify(text) is expected
