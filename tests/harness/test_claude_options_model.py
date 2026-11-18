"""The Claude backend's typed accessor for the model of its own SDK options."""

from __future__ import annotations

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from booley.runtime import _claude_backend


def test_options_model_rejects_options_without_a_model() -> None:
    with pytest.raises(RuntimeError, match="no model"):
        _claude_backend._options_model(ClaudeAgentOptions(model=None))
