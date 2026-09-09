"""Compose live Runtime backends from validated Config settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.config.agent import (
    AgentSettings,
    SandboxConfig,
    get_agent_settings,
    load_agent_settings,
    set_agent_settings,
)
from booley.config.jobs import SlotCaps

from .agent_backend import ClaudeSDKBackend, CodexBackend


@dataclass(frozen=True, slots=True, init=False)
class BackendConfig:
    """A live backend paired with the settings from which Runtime built it."""

    settings: AgentSettings
    active_backend: Any

    def __init__(
        self,
        active_backend: Any = None,
        tier_models: Mapping[str, str] | None = None,
        role_models: Mapping[str, str] | None = None,
        auth: str = "auto",
        provider: str = "claude",
        sandbox: SandboxConfig | None = None,
        jobs: SlotCaps | None = None,
        *,
        settings: AgentSettings | None = None,
    ) -> None:
        """Build Runtime composition directly or from validated settings.

        The explicit fields retain the former test/extension construction shape
        at its new Runtime-owned import path.
        """
        defaults = AgentSettings()
        selected = settings or AgentSettings(
            tier_models=tier_models if tier_models is not None else defaults.tier_models,
            role_models=role_models or {},
            auth=auth,
            provider=provider,
            sandbox=sandbox or SandboxConfig(),
            jobs=jobs or SlotCaps(),
        )
        object.__setattr__(self, "settings", selected)
        object.__setattr__(self, "active_backend", active_backend)

    @property
    def tier_models(self) -> Mapping[str, str]:
        return self.settings.tier_models

    @property
    def role_models(self) -> Mapping[str, str]:
        return self.settings.role_models

    @property
    def auth(self) -> str:
        return self.settings.auth

    @property
    def provider(self) -> str:
        return self.settings.provider

    @property
    def sandbox(self) -> SandboxConfig:
        return self.settings.sandbox

    @property
    def jobs(self) -> SlotCaps:
        return self.settings.jobs

    def model_for_tier(self, tier: str) -> str:
        return self.settings.model_for_tier(tier)

    def model_for_role(self, role: str, tier: str) -> str:
        return self.settings.model_for_role(role, tier)

    def effort_for_tier(self, tier: str) -> str | None:
        return self.settings.effort_for_tier(tier)

    def backend_for_tier(self, tier: str) -> Any:
        del tier
        return self.active_backend


_backend_config: BackendConfig | None = None
_backend_injected = False


def _make_backend(settings: AgentSettings) -> Any:
    if settings.provider == "claude":
        return ClaudeSDKBackend(auth_mode=settings.auth)
    return CodexBackend(auth_mode=settings.auth)


def configure_backend(settings: AgentSettings) -> BackendConfig:
    """Install a freshly constructed backend for validated settings."""
    global _backend_config, _backend_injected
    _backend_config = BackendConfig(_make_backend(settings), settings=settings)
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
