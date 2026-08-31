"""Harness configuration — model routing, MCP capability sets, and re-exports.

Submodules own their concerns; this facade re-exports for backward compat:
  - _retry: API resilience constants
  - _limits: iteration caps
  - _backend_config: BackendConfig, SandboxConfig, load_models_config
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import BoundaryError, as_positive_int, is_str_list, require_dict
from booley.core.config_paths import resolve_booley_toml

logger = logging.getLogger(__name__)

# --- Re-exports: API resilience (owned by _retry.py) ---
# --- Re-exports: backend config (owned by _backend_config.py) ---
from booley.runtime._retry import (  # noqa: F401 — legacy settings facade
    API_RETRY_BACKOFF_MULTIPLIER,
    API_RETRY_INITIAL_BACKOFF_S,
    API_RETRY_JITTER_FRACTION,
    API_RETRY_MAX_BACKOFF_S,
    MAX_API_RETRIES,
    RATE_LIMIT_FALLBACK_BACKOFF_S,
    RATE_LIMIT_SLEEP_BUFFER_S,
)

from .agent import (  # noqa: F401 — re-exported as public API of configuration settings
    _DEFAULT_PROVIDER,
    _DEFAULT_TIER_MODELS,
    _PROVIDER_TIER_MODELS,
    SANDBOX_IMAGE,
    BackendConfig,
    BackendConfigError,
    SandboxConfig,
    _parse_sandbox_config,
    get_backend_config,
    load_models_config,
    set_backend_config,
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

# --- Model assignments (developer-era) ---
MODEL_MAP: dict[str, str] = {
    "developer": "claude-fable-5",
    "recovery": "claude-sonnet-5",
    "triage_report": "claude-opus-4-8",
}

# --- Step-to-tier mapping (developer-era) ---
STEP_TIERS: dict[str, str] = {
    "developer": "heavy",
    "recovery": "light",
    "triage_report": "standard",
}


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
    from booley.eda.config import EdaConfigError, parse_eda_config, retired_config_error

    migration = retired_config_error(data)
    if migration:
        raise EdaConfigError(migration)
    parse_eda_config(data.get("eda"))
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


# --- Host-owned [interactive] config compatibility exports ---

from .host_config import (
    DEFAULT_IDLE_TIMEOUT_SECONDS as DEFAULT_INTERACTIVE_IDLE_TIMEOUT_S,  # noqa: F401 - compatibility re-export
)
from .host_config import (  # noqa: F401 - compatibility re-export
    DEFAULT_MAX_SESSIONS as DEFAULT_INTERACTIVE_MAX_SESSIONS,
)
from .host_config import InteractiveHostPolicy as InteractiveConfig
from .host_config import load_host_policy


def load_interactive_config(project_root: Path | None = None) -> InteractiveConfig:
    """Load global policy; *project_root* is ignored for compatibility."""
    del project_root
    return load_host_policy()


# --- [developer.limits] config ---

DEFAULT_DEVELOPER_ACTIVE_TIMEOUT_S = 30 * 60
DEFAULT_DEVELOPER_WALL_TIMEOUT_S = 12 * 60 * 60


@dataclass(frozen=True)
class DeveloperLimitsConfig:
    """Developer Agent budgets resolved from ``[developer.limits]``."""

    active_timeout_seconds: int = DEFAULT_DEVELOPER_ACTIVE_TIMEOUT_S
    wall_timeout_seconds: int = DEFAULT_DEVELOPER_WALL_TIMEOUT_S


def _developer_limit(raw: object, default: int, field_name: str) -> int:
    value = as_positive_int(raw, default)
    if value is raw:
        return value
    if raw is not None:
        logger.warning(
            "[developer.limits] %s=%r must be a positive integer; using %d",
            field_name,
            raw,
            default,
        )
    return value


def load_developer_limits_config(project_root: Path) -> DeveloperLimitsConfig:
    """Load validated Developer Agent active and wall-clock budgets."""
    developer = _load_booley_toml(project_root).get("developer", {})
    if not isinstance(developer, dict):
        logger.warning("[developer] is not a table; using limit defaults")
        developer = {}
    section = developer.get("limits", {})
    if not isinstance(section, dict):
        logger.warning("[developer.limits] is not a table; using defaults")
        section = {}

    active = _developer_limit(
        section.get("active_timeout_seconds"),
        DEFAULT_DEVELOPER_ACTIVE_TIMEOUT_S,
        "active_timeout_seconds",
    )
    wall = _developer_limit(
        section.get("wall_timeout_seconds"),
        DEFAULT_DEVELOPER_WALL_TIMEOUT_S,
        "wall_timeout_seconds",
    )
    return DeveloperLimitsConfig(active_timeout_seconds=active, wall_timeout_seconds=wall)
