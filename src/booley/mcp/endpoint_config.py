"""Shared endpoint visibility configuration without MCP server imports."""

import tomllib
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from booley.audit.project_schema import audit_specialist_table
from booley.core.boundary import BoundaryError, as_dict, require_dict
from booley.runtime.project_dir import resolve_checkout_project_dir, resolve_project_dir


class EndpointConfigError(ValueError):
    """Project capability settings cannot safely be used for execution."""


def parse_specialist_config(
    config: Any, specialist_names: Collection[str] | None = None
) -> dict[str, Any]:
    """Apply the same Specialist schema at audit and execution boundaries."""
    audit = audit_specialist_table({"specialists": config}, specialist_names)
    if not audit.is_valid:
        raise EndpointConfigError(
            "; ".join(f"{item.message}; {item.fix}" for item in audit.findings)
        )
    return as_dict(config) or {}


def parse_endpoint_config(data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select capability configuration and reject retired visibility tables."""
    for retired in ("tools", "mcp_tools"):
        if retired in data:
            raise EndpointConfigError(
                f"booley.toml [{retired}] is retired; move deterministic settings "
                "to [flows.*] and Specialist settings to [specialists.*]; "
                "remove direct MCP endpoint settings (these have no Project enable switch); "
                "prefix a custom direct endpoint's filename with '_' to disable its discovery"
            )
    specialists = parse_specialist_config(data.get("specialists", {}))
    try:
        flows = require_dict(data.get("flows", {}), field="booley.toml [flows]")
    except BoundaryError as exc:
        raise EndpointConfigError(str(exc)) from exc
    return specialists, flows


def read_endpoint_config(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read optional configuration, failing closed on unreadable or invalid content."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}, {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EndpointConfigError(f"Cannot read {path}: {exc}") from exc
    specialists, flows = parse_endpoint_config(data)
    if specialists:
        from booley.mcp.registry import discover_mcp_tools

        discover_mcp_tools(
            project_mcp_tools_dir=path.parent / "mcp_tools",
            specialist_config=specialists,
            flow_config=flows,
        )
    return specialists, flows


def get_endpoint_config(project_root: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ``[specialists]`` and ``[flows]`` from booley.toml."""
    try:
        project_dir = (
            resolve_checkout_project_dir(project_root)
            if project_root is not None
            else resolve_project_dir()
        )
    except FileNotFoundError:
        return {}, {}
    return read_endpoint_config(project_dir / "booley.toml")


def endpoint_is_enabled(
    name: str, kind: str, specialist_config: Mapping[str, Any], flow_config: Mapping[str, Any]
) -> bool:
    """Apply capability opt-outs while keeping direct MCP endpoints available."""
    namespace = {"flow": flow_config, "specialist": specialist_config}.get(kind, {})
    entry = as_dict(namespace.get(name)) or {}
    return entry.get("enabled") is not False
