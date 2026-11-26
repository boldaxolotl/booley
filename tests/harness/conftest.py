"""Test fixtures for booley tests."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

# Stub claude_agent_sdk before any booley imports touch it
if "claude_agent_sdk" not in sys.modules:
    _sdk = ModuleType("claude_agent_sdk")
    _sdk.AssistantMessage = type("AssistantMessage", (), {})
    _sdk.ClaudeAgentOptions = type(
        "ClaudeAgentOptions",
        (),
        {
            "__init__": lambda self, **kw: self.__dict__.update(kw),
        },
    )
    _sdk.ClaudeSDKError = type("ClaudeSDKError", (Exception,), {})
    _sdk.ProcessError = type("ProcessError", (_sdk.ClaudeSDKError,), {})
    _sdk.ResultError = type("ResultError", (_sdk.ProcessError,), {})
    _sdk.RateLimitEvent = type("RateLimitEvent", (), {})
    _sdk.ResultMessage = type(
        "ResultMessage",
        (),
        {
            "__init__": lambda self, **kw: self.__dict__.update(kw),
        },
    )
    _sdk.UserMessage = type("UserMessage", (), {})
    _sdk.query = AsyncMock()
    sys.modules["claude_agent_sdk"] = _sdk


@pytest.fixture(autouse=True)
def _set_booley_dir(tmp_path, monkeypatch):
    """Prevent resolve_booley_dir() from failing in harness tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
