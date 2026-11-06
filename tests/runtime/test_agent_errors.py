"""Terminal model-limit diagnostic classification."""

import pytest

from booley.runtime.agent_errors import is_context_exhausted


def test_codex_input_too_large_is_context_exhaustion():
    assert is_context_exhausted(
        "turn/start input_too_large: max_chars=1048576, actual_chars=57231875"
    )


@pytest.mark.parametrize(
    "text",
    [
        '{"status":400,"error":{"message":"The model does not exist","code":"model_not_found"}}',
        "The 'gpt-x' model is not supported when using Codex with a ChatGPT account.",
        "There's an issue with the selected model (claude-x). It may not exist or you may not have access to it.",
        "The model `gpt-x` does not exist or you do not have access to it.",
        '{"type":"error","error":{"type":"not_found_error","message":"model: claude-x"}}',
    ],
    ids=["issue-json", "chatgpt-text", "claude-code-text", "openai-text", "anthropic-body"],
)
def test_model_refusal_text_is_classified(text):
    from booley.runtime.agent_errors import is_model_refusal

    assert is_model_refusal(text)


@pytest.mark.parametrize(
    "text",
    [
        "Model metadata for `gpt-x` not found. Defaulting to fallback metadata.",
        '{"status":400,"error":{"message":"Invalid request: tool schema rejected"}}',
        "unexpected status 401 Unauthorized: invalid token",
    ],
    ids=["metadata-item", "bad-request", "auth-failure"],
)
def test_other_provider_text_is_not_a_model_refusal(text):
    from booley.runtime.agent_errors import is_model_refusal

    assert not is_model_refusal(text)
