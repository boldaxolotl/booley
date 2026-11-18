"""Per-endpoint validation of custom MCP endpoints."""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.runtime.booley_dir import resolve_checkout_booley_dir

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkippedEndpoint:
    """A custom endpoint file validation skipped, with why and how to fix it."""

    file: str
    reason: str
    fix: str


def validate_custom_endpoints(project_root: Path) -> list[SkippedEndpoint]:
    """Validate custom MCP endpoints.

    Loads the endpoint config, then checks each custom endpoint file: a file with
    a syntax error, with no endpoint subclass, or whose name collides with a
    built-in endpoint is skipped and returned; a retired ``sandbox`` attribute
    logs a warning.
    """
    from booley.mcp.registry import discover_mcp_tools

    # --- Load endpoint config ---
    specialist_config, flow_config = _load_endpoint_config(project_root)
    custom_mcp_tools_dir = resolve_checkout_booley_dir(project_root) / "mcp_tools"
    # --- Per-endpoint validation (warn + skip on error) ---
    if not custom_mcp_tools_dir.is_dir():
        return []

    builtin_names = {t.name for t in discover_mcp_tools()}
    skipped: list[SkippedEndpoint] = []

    for py_file in sorted(custom_mcp_tools_dir.glob("*.py")):
        if py_file.stem.startswith("_"):
            continue
        outcome = _validate_single_endpoint(py_file, specialist_config, flow_config, builtin_names)
        if outcome is not None:
            skipped.append(outcome)

    logger.debug("Custom MCP endpoint validation complete")
    return skipped


def _warn_retired_sandbox_attr(
    py_file: Path,
    tree: ast.Module,
) -> None:
    """Warn when a custom MCP endpoint retains retired ``sandbox`` metadata."""
    sandbox_val = _extract_sandbox_attr(tree)
    if sandbox_val is None:
        return
    logger.warning(
        "CUSTOM MCP ENDPOINT WARNING: %s — class attribute sandbox=%r is retired and ignored; "
        "delete the retired sandbox metadata; endpoints run in the Sandbox",
        py_file.name,
        sandbox_val,
    )


def _validate_single_endpoint(
    py_file: Path,
    specialist_config: dict[str, Any],
    flow_config: dict[str, Any],
    builtin_names: set[str],
) -> SkippedEndpoint | None:
    """Validate one custom MCP endpoint file; return it when it is skipped."""
    from booley.mcp.registry import extract_mcp_tool_info

    # Check 1: Parse errors
    try:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError as e:
        return SkippedEndpoint(
            py_file.name,
            f"Python syntax error on line {e.lineno}: {e.msg}",
            "fix the syntax error",
        )

    # Check 3: No recognized MCP endpoint subclass
    info = extract_mcp_tool_info(py_file, builtin=False)
    if info is None:
        return SkippedEndpoint(
            py_file.name,
            "no McpTool, BooleyFlow or Specialist subclass with plain string "
            "literal name and description",
            "make `name` and `description` plain string literals on an McpTool, "
            "BooleyFlow or Specialist subclass",
        )

    from booley.mcp.endpoint_config import endpoint_is_enabled

    if not endpoint_is_enabled(info.name, info.kind, specialist_config, flow_config):
        return None

    # Check 4: Name collision with builtin
    if info.name in builtin_names:
        return SkippedEndpoint(
            py_file.name,
            f"name '{info.name}' collides with the built-in endpoint '{info.name}'",
            "rename the class's `name` attribute",
        )

    _warn_retired_sandbox_attr(py_file, tree)
    return None


def _load_endpoint_config(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the ``[specialists]`` and ``[flows]`` sections from booley.toml."""
    from booley.mcp.endpoint_config import read_endpoint_config

    toml_path = resolve_checkout_booley_dir(project_root) / "booley.toml"
    return read_endpoint_config(toml_path)


def _extract_sandbox_attr(tree: ast.Module) -> str | None:
    """Extract sandbox class attribute value from an AST tree."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            attr_name = None
            val = None
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                attr_name = item.target.id
                val = item.value
            elif isinstance(item, ast.Assign) and len(item.targets) == 1:
                if isinstance(item.targets[0], ast.Name):
                    attr_name = item.targets[0].id
                    val = item.value
            if (
                attr_name == "sandbox"
                and isinstance(val, ast.Constant)
                and isinstance(val.value, str)
            ):
                return val.value
    return None
