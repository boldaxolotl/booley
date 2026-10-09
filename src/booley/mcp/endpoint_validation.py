"""Structural and per-endpoint validation of custom MCP endpoints and Criteria."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

from booley.runtime.project_dir import resolve_checkout_project_dir

logger = logging.getLogger(__name__)


class EndpointValidationError(Exception):
    """Structural endpoint or Criteria errors that prevent execution."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        super().__init__("Endpoint validation failed:\n  " + "\n  ".join(failures))


def validate_custom_endpoints_and_criteria(project_root: Path) -> None:
    """Validate custom MCP endpoints and project criteria.

    Load order: criteria TOML first, then endpoint scan, then cross-validation.

    Per-endpoint errors (checks 1-5, 7, 9) → skip endpoint + warn.
    Structural errors (checks 6, 8) → hard-fail.
    """
    try:
        # Probe importability up front: if any of these are unavailable, skip
        # the whole validation (structural checks below re-import the same,
        # now-cached, names themselves rather than taking them as params).
        from booley.criteria.templates import (  # noqa: F401
            load_base_criteria,
            load_project_criteria,
            merge_criteria_defs,
        )
        from booley.mcp.registry import (  # noqa: F401
            discover_mcp_tools,
            extract_mcp_tool_info,
        )
    except ImportError:
        logger.debug("Custom MCP endpoint validation skipped (imports unavailable)")
        return

    # --- Load criteria and validate structural integrity ---
    all_criteria_names = _validate_criteria_structure(project_root)

    # --- Load endpoint config and check structural endpoint errors ---
    specialist_config, flow_config = _load_endpoint_config(project_root)
    custom_mcp_tools_dir = resolve_checkout_project_dir(project_root) / "mcp_tools"
    # --- Per-endpoint validation (warn + skip on error) ---
    if not custom_mcp_tools_dir.is_dir():
        return

    builtin_names = {t.name for t in discover_mcp_tools()}

    for py_file in sorted(custom_mcp_tools_dir.glob("*.py")):
        if py_file.stem.startswith("_"):
            continue
        _validate_single_endpoint(
            py_file, specialist_config, flow_config, builtin_names, all_criteria_names
        )

    logger.debug("Custom MCP endpoint validation complete")


def _validate_criteria_structure(project_root: Path) -> set[str]:
    """Load and cross-validate criteria definitions. Returns all criteria names.

    Raises EndpointValidationError on criteria conflicts (check 8).
    """
    from booley.criteria.templates import (
        load_base_criteria,
        load_project_criteria,
        merge_criteria_defs,
    )

    base_criteria = load_base_criteria()
    base_criteria_names = {c.name for c in base_criteria}

    project_criteria_path = resolve_checkout_project_dir(project_root) / "criteria.toml"
    project_criteria = load_project_criteria(project_criteria_path)

    _merged, merge_errors = merge_criteria_defs(base_criteria, project_criteria)
    structural_errors = [f"CRITERIA CONFLICT: {e}" for e in merge_errors]
    if structural_errors:
        raise EndpointValidationError(structural_errors)

    return base_criteria_names | {c.name for c in project_criteria}


def _check_satisfies_refs(
    py_file: Path,
    info: Any,
    all_criteria_names: set[str],
) -> None:
    """Warn when satisfies references unknown criteria names."""
    if not info.satisfies:
        return
    for crit_name in info.satisfies:
        if crit_name not in all_criteria_names:
            logger.warning(
                "CUSTOM MCP ENDPOINT WARNING: %s — satisfies references unknown "
                "criterion '%s'. Define it in the Project criteria.toml "
                "or check for typos.",
                py_file.name,
                crit_name,
            )


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
    all_criteria_names: set[str],
) -> None:
    """Validate one custom MCP endpoint file; warn and skip on local errors."""
    from booley.mcp.registry import extract_mcp_tool_info

    # Check 1: Parse errors
    try:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError as e:
        logger.warning(
            "CUSTOM MCP ENDPOINT SKIPPED: %s — Python syntax error: %s", py_file.name, e
        )
        return

    # Check 3: No recognized MCP endpoint subclass
    info = extract_mcp_tool_info(py_file, builtin=False)
    if info is None:
        logger.warning(
            "CUSTOM MCP TOOL SKIPPED: %s — no McpTool/BooleyFlow/Specialist "
            "subclass found, or missing name/description",
            py_file.name,
        )
        return

    from booley.mcp.endpoint_config import endpoint_is_enabled

    if not endpoint_is_enabled(info.name, info.kind, specialist_config, flow_config):
        return

    # Check 4: Name collision with builtin
    if info.name in builtin_names:
        logger.warning(
            "CUSTOM MCP ENDPOINT SKIPPED: %s — name '%s' conflicts with a built-in endpoint. "
            "Rename it in the class definition.",
            py_file.name,
            info.name,
        )
        return

    _check_satisfies_refs(py_file, info, all_criteria_names)
    _warn_retired_sandbox_attr(py_file, tree)

    # Check 9: Empty satisfies warning for enabled endpoints.
    if not info.satisfies:
        logger.warning(
            "CUSTOM MCP ENDPOINT WARNING: %s — enabled endpoint '%s' has "
            "satisfies=[] (no criteria declared). This may be an AST extraction "
            "limitation if satisfies uses computed values.",
            py_file.name,
            info.name,
        )


def _load_endpoint_config(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the ``[specialists]`` and ``[flows]`` sections from booley.toml."""
    from booley.mcp.endpoint_config import read_endpoint_config

    toml_path = resolve_checkout_project_dir(project_root) / "booley.toml"
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
