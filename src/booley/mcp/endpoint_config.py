"""Shared endpoint visibility configuration without MCP server imports."""

import logging
import os
from pathlib import Path
from typing import Any

from booley.runtime.project_dir import PROJECT_DIR_NAME

logger = logging.getLogger(__name__)


def get_endpoint_config(project_root: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ``[mcp_tools]`` and ``[flows]`` from booley.toml."""
    try:
        import tomllib

        project_dir = os.environ.get("BOOLEY_PROJECT_DIR", "")
        toml_path = (
            Path(project_dir) / "booley.toml"
            if project_dir
            else (project_root or Path.cwd()) / PROJECT_DIR_NAME / "booley.toml"
        )
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
