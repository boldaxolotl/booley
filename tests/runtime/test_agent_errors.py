"""Terminal model-limit diagnostic classification."""

from booley.runtime.agent_errors import is_context_exhausted


def test_codex_input_too_large_is_context_exhaustion():
    assert is_context_exhausted(
        "turn/start input_too_large: max_chars=1048576, actual_chars=57231875"
    )
