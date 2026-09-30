"""Shared endpoint visibility configuration without MCP server imports."""

import logging
from pathlib import Path
from typing import Any

from booley.runtime.project_dir import resolve_checkout_project_dir, resolve_project_dir

logger = logging.getLogger(__name__)


def get_endpoint_config(project_root: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ``[mcp_tools]`` and ``[flows]`` from booley.toml."""
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
            legacy = cfg.get("tools")
            if legacy is not None:
                raise ValueError(
                    "booley.toml [tools] is retired; move deterministic settings "
                    "to [flows.*] and Specialist settings to [mcp_tools.*]"
                )
            mcp_tools = cfg.get("mcp_tools", {})
            flows = cfg.get("flows", {})
            return (
                mcp_tools if isinstance(mcp_tools, dict) else {},
                flows if isinstance(flows, dict) else {},
            )
    except ValueError:
        raise
    except Exception:  # unreadable config falls back to empty config
        logger.debug("Failed to load endpoint config from booley.toml", exc_info=True)
    return {}, {}
