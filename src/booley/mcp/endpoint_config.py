"""Shared endpoint visibility configuration without MCP server imports."""

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.runtime.project_dir import resolve_checkout_project_dir, resolve_project_dir

logger = logging.getLogger(__name__)


def parse_endpoint_config(data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select capability configuration and reject retired visibility tables."""
    for retired in ("tools", "mcp_tools"):
        if retired in data:
            raise ValueError(
                f"booley.toml [{retired}] is retired; move deterministic settings "
                "to [flows.*] and Specialist settings to [specialists.*]; "
                "remove protocol utility settings (utilities have no project enable switch)"
            )
    specialists = data.get("specialists", {})
    flows = data.get("flows", {})
    return (
        specialists if isinstance(specialists, dict) else {},
        flows if isinstance(flows, dict) else {},
    )


def get_endpoint_config(project_root: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ``[specialists]`` and ``[flows]`` from booley.toml."""
    try:
        import tomllib

        project_dir = (
            resolve_checkout_project_dir(project_root)
            if project_root is not None
            else resolve_project_dir()
        )
        toml_path = project_dir / "booley.toml"
        if toml_path.exists():
            with toml_path.open("rb") as f:
                cfg = tomllib.load(f)
            return parse_endpoint_config(cfg)
    except ValueError:
        raise
    except Exception:  # unreadable config falls back to empty config
        logger.debug("Failed to load endpoint config from booley.toml", exc_info=True)
    return {}, {}
