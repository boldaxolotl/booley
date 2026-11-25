"""Declarative built-in Flow enablement from Project configuration."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.config import flow_knobs
from booley.core.booley_dir import resolve_booley_dir
from booley.core.boundary import BoundaryError, as_dict
from booley.core.config_paths import (
    BooleyTomlError,
    main_repository_root,
    resolve_booley_toml,
    resolve_toml,
)
from booley.targets.flow_names import config_section

_RETIRED_EXECUTION_KEYS = ("venue", "backend", "host_setup_commands")


class FlowConfigError(BooleyTomlError):
    """Project Flow configuration contains a retired key or an invalid ``enabled``."""


def flow_enabled(flow_name: str, work_dir: Path | None) -> bool:
    """Read ``[flows.<name>].enabled`` (default true); a non-boolean value raises."""
    path, cfg = _load_config(work_dir)
    try:
        return flow_enabled_from_config(flow_name, cfg)
    except FlowConfigError as exc:
        raise FlowConfigError(str(exc), path=path) from exc


def flow_enabled_from_config(flow_name: str, cfg: object) -> bool:
    """Resolve enablement from parsed config and reject retired execution keys."""
    cfg = as_dict(cfg, default={}) or {}
    migration = retired_config_error(cfg)
    if migration:
        raise FlowConfigError(migration)
    flows = as_dict(cfg.get("flows"), default={}) or {}
    section = config_section(flows, flow_name)
    try:
        return flow_knobs.enabled(flow_name, section)
    except BoundaryError as exc:
        raise FlowConfigError(str(exc)) from exc


def retired_config_error(raw: Mapping[str, Any]) -> str | None:
    """Return the first hard-migration error for a removed execution surface."""
    errors = retired_config_errors(raw)
    return errors[0] if errors else None


def retired_config_errors(raw: Mapping[str, Any]) -> list[str]:
    """Return every hard-migration error for removed execution surfaces, in order."""
    errors: list[str] = []
    flows = raw.get("flows", {})
    if isinstance(flows, dict):
        errors.extend(_retired_flows_errors(flows))
    sandbox = raw.get("sandbox", {})
    if isinstance(sandbox, dict) and "passthrough_env" in sandbox:
        errors.append(
            "booley.toml [sandbox].passthrough_env is retired; use a host License Profile"
        )
    return errors


def _retired_flows_errors(flows: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for retired in ("elab", "elaborate"):
        if retired in flows:
            errors.append(
                f"booley.toml [flows.{retired}] is retired; use "
                "`booley flow sim --mode elab-only` and delete "
                "standalone_frontend"
            )
    errors.extend(
        _retired_flow_key_error("flows", key, flows[key])
        for key in _RETIRED_EXECUTION_KEYS
        if key in flows
    )
    for name, section in flows.items():
        if not isinstance(section, dict):
            continue
        if "standalone_frontend" in section:
            errors.append(
                f"booley.toml [flows.{name}].standalone_frontend is retired; "
                "delete the key — the standalone module probe now follows "
                "each Target's simulator"
            )
        errors.extend(
            _retired_flow_key_error(f"flows.{name}", key, section[key])
            for key in _RETIRED_EXECUTION_KEYS
            if key in section
        )
    return errors


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
            "the Sandbox; delete the key"
        )
    return f"booley.toml [{section}].{key} is retired; delete the key"


def _load_config(work_dir: Path | None) -> tuple[Path | None, dict[str, Any]]:
    try:
        if work_dir is None:
            path = resolve_toml(resolve_booley_dir())
            return _parsed(path, _read_toml(path))
        root = work_dir.resolve()
        path = resolve_booley_toml(root)
        config = _read_toml(path)
        if config is not None:
            return path, config
        main_root = main_repository_root(root)
        if main_root is not None:
            path = resolve_booley_toml(main_root)
            return _parsed(path, _read_toml(path))
    except (OSError, ValueError):
        # Compatibility contract: unreadable or malformed documents are
        # absent, so only an explicit parsed ``false`` disables a Flow.
        return None, {}
    return None, {}


def _parsed(path: Path, config: dict[str, Any] | None) -> tuple[Path | None, dict[str, Any]]:
    return (path, config) if config is not None else (None, {})


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        return tomllib.load(stream)
