"""Harness configuration — MCP capability sets and re-exports.

Submodules own their concerns; this facade re-exports for backward compat:
  - _limits: iteration caps
  - agent: AgentSettings, SandboxConfig, and validated config loading
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import BoundaryError, is_str_list, require_dict
from booley.core.config_paths import resolve_booley_toml

logger = logging.getLogger(__name__)

# --- Re-exports: validated agent settings (owned by agent.py) ---
from .agent import (  # noqa: F401 — re-exported as public API of configuration settings
    _DEFAULT_PROVIDER,
    _DEFAULT_TIER_MODELS,
    _PROVIDER_TIER_MODELS,
    SANDBOX_IMAGE,
    AgentSettings,
    BackendConfigError,
    SandboxConfig,
    _parse_sandbox_config,
    get_agent_settings,
    load_agent_settings,
    set_agent_settings,
)

# --- Re-exports: editor for Console clickable links (always VS Code) ---
from .editor import (  # noqa: F401 — re-exported as public API of configuration settings
    VSCODE_EDITOR,
    ResolvedEditor,
    resolve_editor,
)

# --- Re-exports: harness limits (owned by _limits.py) ---
from .limits import (  # noqa: F401 — re-exported as public API of configuration settings
    HARD_MAX_COMPILE_RETRIES,
    HARD_MAX_DEBUG_ROUNDS,
    HARD_MAX_MUTATION_ITERATIONS,
    MAX_COMPILE_RETRIES,
    MAX_DEBUG_ROUNDS,
    MAX_FIX_LINES,
    MAX_JUDGE_RETRIES,
    MAX_LINT_ITERATIONS,
    MAX_MUTATION_ITERATIONS,
    MAX_POST_REVIEW_SIM_ITERATIONS,
    MUTATION_TESTING_PASS_THRESHOLD,
)

# --- booley.toml loader ---


def _load_booley_toml(project_root: Path) -> dict:
    """Load ``booley.toml`` (or legacy ``pipeline.toml``) and return the full parsed dict."""
    toml_path = resolve_booley_toml(project_root)
    if not toml_path.exists():
        return {}
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        logger.warning("tomllib unavailable; ignoring %s", toml_path)
        return {}
    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Failed to load %s: %s", toml_path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    from booley.config.eda import EdaConfigError, parse_eda_config
    from booley.config.flow_enablement import retired_config_error

    migration = retired_config_error(data)
    if migration:
        raise EdaConfigError(migration)
    parse_eda_config(data.get("eda"))
    from booley.config.protected_inputs import parse_protected_inputs

    parse_protected_inputs(data)
    return data


class SubmoduleConfigError(ValueError):
    """The Project's submodule selection configuration is invalid."""


@dataclass(frozen=True)
class SubmoduleConfig:
    """Configured top-level submodule allowlist; ``None`` selects every gitlink."""

    paths: tuple[str, ...] | None = None


def load_submodule_config(project_root: Path) -> SubmoduleConfig:
    """Load and strictly validate the Project's ``[submodules]`` configuration."""
    raw_section = _load_booley_toml(project_root).get("submodules")
    if raw_section is None:
        return SubmoduleConfig()
    try:
        section = require_dict(raw_section, field="[submodules]")
    except BoundaryError as exc:
        raise SubmoduleConfigError(str(exc)) from exc
    if "paths" not in section:
        return SubmoduleConfig()
    raw_paths = section["paths"]
    if not is_str_list(raw_paths):
        raise SubmoduleConfigError("[submodules].paths must be an array of strings")
    return SubmoduleConfig(paths=tuple(raw_paths))


# --- Host-owned [sandbox] config compatibility exports ---

from .host_config import (
    DEFAULT_IDLE_TIMEOUT_SECONDS as DEFAULT_INTERACTIVE_IDLE_TIMEOUT_S,  # noqa: F401 - compatibility re-export
)
from .host_config import (  # noqa: F401 - compatibility re-export
    DEFAULT_MAX_SESSIONS as DEFAULT_INTERACTIVE_MAX_SESSIONS,
)
from .host_config import (
    SandboxHostPolicy as InteractiveConfig,  # noqa: F401 - compatibility re-export
)
