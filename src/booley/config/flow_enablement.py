"""Declarative built-in Flow enablement from Project configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict
from booley.core.config_paths import resolve_booley_toml, resolve_toml
from booley.core.project_dir import resolve_project_dir
from booley.targets.flow_names import config_section


class FlowConfigError(ValueError):
    """Project Flow configuration contains a retired execution surface."""


def flow_enabled(flow_name: str, work_dir: Path | None) -> bool:
    """Read ``[flows.<name>].enabled`` with the existing enabled default."""
    cfg = _load_config(work_dir)
    return flow_enabled_from_config(flow_name, cfg)


def flow_enabled_from_config(flow_name: str, cfg: object) -> bool:
    """Resolve enablement from parsed config and reject retired execution keys."""
    cfg = as_dict(cfg, default={}) or {}
    migration = retired_config_error(cfg)
    if migration:
        raise FlowConfigError(migration)
    flows = as_dict(cfg.get("flows"), default={}) or {}
    section = config_section(flows, flow_name)
    return section.get("enabled", True) is not False


def retired_config_error(raw: dict[str, Any]) -> str | None:
    """Return the first hard-migration error for a removed execution surface."""
    flows = raw.get("flows", {})
    if isinstance(flows, dict):
        for retired in ("elab", "elaborate"):
            if retired in flows:
                return (
                    f"booley.toml [flows.{retired}] is retired; use "
                    "`booley flow sim --mode elab-only` and move "
                    "standalone_frontend to [flows.sim].standalone_frontend"
                )
        for key in ("venue", "backend", "host_setup_commands"):
            if key in flows:
                return _retired_flow_key_error("flows", key, flows[key])
        for name, section in flows.items():
            if not isinstance(section, dict):
                continue
            for key in ("venue", "backend", "host_setup_commands"):
                if key in section:
                    return _retired_flow_key_error(f"flows.{name}", key, section[key])
    sandbox = raw.get("sandbox", {})
    if isinstance(sandbox, dict) and "passthrough_env" in sandbox:
        return "booley.toml [sandbox].passthrough_env is retired; use a host License Profile"
    return None


def _retired_flow_key_error(section: str, key: str, value: object) -> str:
    if key == "backend" and str(value).strip() == "none" and section != "flows":
        flow_name = section.removeprefix("flows.")
        return (
            f'booley.toml [{section}].backend = "none" is retired; write instead:\n'
            f"  [flows.{flow_name}]\n  enabled = false"
        )
    if key == "backend":
        return (
            f"booley.toml [{section}].backend is retired; all Flows run inside "
            "the Session Runtime; delete the key"
        )
    return f"booley.toml [{section}].{key} is retired; delete the key"


def _load_config(work_dir: Path | None) -> dict[str, Any]:
    try:
        if work_dir is None:
            return _read_toml(resolve_toml(resolve_project_dir())) or {}
        root = work_dir.resolve()
        config = _read_toml(resolve_booley_toml(root))
        if config is not None:
            return config
        main_root = _resolve_main_repo_root(root)
        if main_root is not None:
            return _read_toml(resolve_booley_toml(main_root)) or {}
    except (OSError, ValueError):
        # Compatibility contract: unreadable or malformed documents are
        # absent, so only an explicit parsed ``false`` disables a Flow.
        return {}
    return {}


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _resolve_main_repo_root(work_dir: Path) -> Path | None:
    """Follow a linked worktree's Git marker to its main repository root."""
    marker = work_dir / ".git"
    if not marker.is_file():
        return None
    try:
        content = marker.read_text(encoding="utf-8").strip()
        if not content.startswith("gitdir:"):
            return None
        git_dir = Path(content.split(":", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = (work_dir / git_dir).resolve()
        for parent in git_dir.parents:
            if parent.name == ".git":
                return parent.parent
    except (OSError, ValueError):
        return None
    return None
