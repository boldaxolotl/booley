"""Compose live Runtime backends from validated Config settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.config.agent import (
    AgentSettings,
    get_agent_settings,
    load_agent_settings,
    set_agent_settings,
)

from .agent_backend import AgentBackend, ClaudeSDKBackend, CodexBackend


@dataclass(frozen=True, slots=True)
class BackendConfig:
    """A live backend paired with the settings from which Runtime built it."""

    settings: AgentSettings
    active_backend: AgentBackend


_backend_config: BackendConfig | None = None
_backend_injected = False


def _make_backend(settings: AgentSettings) -> AgentBackend:
    if settings.provider == "claude":
        return ClaudeSDKBackend(auth_mode=settings.auth)
    return CodexBackend(auth_mode=settings.auth)


def configure_backend(settings: AgentSettings) -> BackendConfig:
    """Install a freshly constructed backend for validated settings."""
    global _backend_config, _backend_injected
    _backend_config = BackendConfig(settings=settings, active_backend=_make_backend(settings))
    _backend_injected = False
    return _backend_config


def load_backend_config(project_root: Path, *, project_dir: Path | None = None) -> BackendConfig:
    """Load Config settings and atomically replace Runtime backend composition."""
    return configure_backend(load_agent_settings(project_root, project_dir=project_dir))


def get_backend_config() -> BackendConfig:
    """Return the live Runtime backend, constructing it lazily when necessary."""
    global _backend_config
    settings = get_agent_settings()
    if _backend_config is None or (
        not _backend_injected and _backend_config.settings is not settings
    ):
        _backend_config = configure_backend(settings)
    return _backend_config


def set_backend_config(config: BackendConfig | None) -> None:
    """Replace or clear live Runtime composition for deterministic tests."""
    global _backend_config, _backend_injected
    _backend_config = config
    _backend_injected = config is not None
    if config is None:
        set_agent_settings(None)
