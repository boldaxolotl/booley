"""Test fixtures for booley tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

# Ensure package is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

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


TICKETS_REL = Path(".booley") / "tickets"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Create a minimal project structure for testing."""
    # Create tickets directory structure (canonical layout)
    tickets = tmp_path / TICKETS_REL
    for subdir in ["board", "state", "logs"]:
        (tickets / subdir).mkdir(parents=True)
    # Create .booley scripts dir
    (tmp_path / ".booley" / "src").mkdir(parents=True, exist_ok=True)
    # Create .git marker
    (tmp_path / ".git").write_text("gitdir: fake", encoding="utf-8")
    return tmp_path
