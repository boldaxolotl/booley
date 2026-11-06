"""Compose live Runtime backends from validated Config settings."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    """The live backends for one settings value, one per Agent CLI the Project may run.

    ``active_backend`` serves the default CLI; any other listed CLI's backend is built
    on first use by :meth:`backend_for`.
    """

    settings: AgentSettings
    active_backend: AgentBackend
    _others: dict[str, AgentBackend] = field(default_factory=dict, repr=False)

    def backend_for(self, cli: str) -> AgentBackend:
        """The backend that runs *cli*, built once with that CLI's auth policy."""
        if cli == self.settings.default_cli:
            return self.active_backend
        if cli not in self._others:
            self._others[cli] = _make_backend(cli, self.settings.auth_for(cli))
        return self._others[cli]


_backend_config: BackendConfig | None = None
_backend_injected = False


def _make_backend(cli: str, auth: str) -> AgentBackend:
    if cli == "claude":
        return ClaudeSDKBackend(auth_mode=auth)
    return CodexBackend(auth_mode=auth)


def configure_backend(settings: AgentSettings) -> BackendConfig:
    """Install a freshly constructed backend for validated settings."""
    global _backend_config, _backend_injected
    _backend_config = BackendConfig(
        settings=settings,
        active_backend=_make_backend(
            settings.default_cli, settings.auth_for(settings.default_cli)
        ),
    )
    _backend_injected = False
    return _backend_config


def load_backend_config(project_root: Path, *, booley_dir: Path | None = None) -> BackendConfig:
    """Load Config settings and atomically replace Runtime backend composition."""
    return configure_backend(load_agent_settings(project_root, booley_dir=booley_dir))


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
    set_agent_settings(None if config is None else config.settings)
