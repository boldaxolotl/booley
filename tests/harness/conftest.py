"""Test fixtures for booley tests."""

from __future__ import annotations

import subprocess
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest


class _ReleaseImageDocker:
    """Configurable Docker boundary adapter for release-image tests."""

    def __init__(
        self,
        *,
        fingerprints: dict[str, str | None],
        oci_versions: dict[str, str | None] | None = None,
        pull_returncode: int = 0,
    ) -> None:
        self.fingerprints = fingerprints
        self.oci_versions = oci_versions or {}
        self.pull_returncode = pull_returncode
        self.commands: list[list[str]] = []

    def run(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"] and len(command) == 4:
            return subprocess.CompletedProcess(
                command,
                0 if command[3] in self.fingerprints else 1,
                "",
                "",
            )
        if command[:3] == ["docker", "image", "inspect"]:
            image = command[-1]
            is_fingerprint = any("booley.build-fingerprint" in part for part in command)
            labels = self.fingerprints if is_fingerprint else self.oci_versions
            value = labels.get(image)
            return subprocess.CompletedProcess(command, 0, f"{value or '<no value>'}\n", "")
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, self.pull_returncode, "", "not found")
        if command[:3] == ["docker", "system", "df"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def release_image_docker() -> type[_ReleaseImageDocker]:
    """Build a Docker adapter with per-image release provenance."""
    return _ReleaseImageDocker


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
