"""MCP tool registry for built-in and project-defined endpoints.

Scans endpoint directories for McpTool subclasses via AST parsing (no imports),
applies endpoint enablement, and returns metadata for prompt
construction.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpToolInfo:
    """Metadata for a discovered MCP endpoint."""

    name: str
    path: str
    description: str
    code_modifying: bool = False
    satisfies: tuple[str, ...] = ()
    satisfies_args: dict[str, str] | None = None
    # Canonical endpoint family: deterministic Flow, Specialist, or a direct
    # non-Flow MCP endpoint.
    kind: str = "mcp_tool"


# Modules in endpoint packages that must be excluded from discovery even though (or
# because) they parse as endpoint modules. Public: consumed by mcp_server.py and
# discover_mcp_tools() below.
#
# Support modules without an endpoint subclass do not belong here: discovery
# paths gate on extract_mcp_tool_info() (import-free AST check), so a helper
# module is skipped automatically. This set is only for modules that WOULD
# pass that check but must stay unregistered, plus endpoint infrastructure that
# should never be considered a candidate in the first place.
SKIP_MODULES = frozenset(
    {
        "__init__",
        "base",
        "specialist",
        "registry",
        # Hidden: not yet mature / not proven effective. Code is retained;
        # re-enable by removing the name from this set (see docs/ROADMAP.md).
        # tb_coder is de-registered too: TB is authored by the developer directly for now.
        "coverage_analyst",
        "tb_coder",
    }
)

BUILTIN_FLOW_PACKAGES = ("sim", "lint", "synth", "elab", "fpga")


def discover_mcp_tools(
    *,
    booley_src: Path | None = None,
    project_mcp_tools_dir: Path | None = None,
    mcp_tool_config: dict[str, Any] | None = None,
    flow_config: dict[str, Any] | None = None,
) -> list[McpToolInfo]:
    """Discover and filter available MCP endpoints.

    Args:
        booley_src: Path to the installed ``booley`` package directory.
            Defaults to the parent of this file's package.
        project_mcp_tools_dir: Project custom MCP tool directory.
        mcp_tool_config: ``booley.toml [mcp_tools]`` Specialist/endpoint config.
        flow_config: ``booley.toml [flows]`` deterministic Flow config.

    Returns:
        Enabled MCP endpoints, built-ins first and then project-defined ones.
    """
    if booley_src is None:
        booley_src = Path(__file__).resolve().parent.parent

    mcp_tool_config = mcp_tool_config or {}
    flow_config = flow_config or {}

    discovered: list[McpToolInfo] = []
    discovered.extend(_scan_builtin_flows(booley_src / "flows", flow_config))
    discovered.extend(
        _scan_directory(
            booley_src / "mcp",
            mcp_tool_config,
            flow_config,
            builtin=True,
            package="mcp",
        )
    )
    discovered.extend(
        _scan_directory(
            booley_src / "specialists",
            mcp_tool_config,
            flow_config,
            builtin=True,
            package="specialists",
        )
    )

    if project_mcp_tools_dir and project_mcp_tools_dir.is_dir():
        discovered.extend(
            _scan_directory(
                project_mcp_tools_dir,
                mcp_tool_config,
                flow_config,
                builtin=False,
                package="",
            )
        )

    return discovered


def _scan_builtin_flows(
    flows_dir: Path,
    flow_config: dict[str, Any],
) -> list[McpToolInfo]:
    """Discover only the explicitly registered built-in Flow packages."""
    results: list[McpToolInfo] = []
    for package_name in BUILTIN_FLOW_PACKAGES:
        flow_file = flows_dir / package_name / "flow.py"
        if not flow_file.is_file():
            logger.warning("Built-in Flow implementation missing: %s", flow_file)
            continue
        info = extract_mcp_tool_info(
            flow_file,
            builtin=True,
            package=f"flows/{package_name}",
        )
        if info is None:
            logger.warning("Built-in Flow metadata missing: %s", flow_file)
            continue
        endpoint_entry = flow_config.get(info.name)
        if isinstance(endpoint_entry, dict) and endpoint_entry.get("enabled") is False:
            logger.debug("MCP endpoint %s disabled via config", info.name)
            continue
        results.append(info)
    return results


def _scan_directory(
    endpoint_dir: Path,
    mcp_tool_config: dict[str, Any],
    flow_config: dict[str, Any] | None = None,
    *,
    builtin: bool,
    package: str = "",
) -> list[McpToolInfo]:
    """Scan a directory for enabled MCP endpoint subclasses via AST."""
    results: list[McpToolInfo] = []
    flow_config = flow_config or {}
    if not endpoint_dir.is_dir():
        return results

    for py_file in sorted(endpoint_dir.glob("*.py")):
        module_name = py_file.stem
        if module_name.startswith("_") or module_name in SKIP_MODULES:
            continue

        info = extract_mcp_tool_info(py_file, builtin=builtin, package=package)
        if info is None:
            continue

        namespace = flow_config if info.kind == "flow" else mcp_tool_config
        endpoint_entry = namespace.get(info.name)
        if isinstance(endpoint_entry, dict) and endpoint_entry.get("enabled") is False:
            logger.debug("MCP endpoint %s disabled via config", info.name)
            continue

        results.append(info)

    return results


def extract_mcp_tool_info(
    py_file: Path,
    *,
    builtin: bool,
    package: str = "",
) -> McpToolInfo | None:
    """Extract endpoint metadata from a Python file via AST (no import).

    Public API: peer modules (mcp_server, preflight) depend on this name rather
    than reaching for a private helper (principle 9 — depend on abstractions).
    """
    try:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
    except (SyntaxError, OSError) as e:
        logger.warning("Failed to parse %s: %s", py_file, e)
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        base_names = _get_base_names(node)
        if not base_names & {"McpTool", "BooleyFlow", "Specialist"}:
            continue

        attrs = _extract_endpoint_metadata_attrs(node)
        if attrs["name"] and attrs["description"]:
            return _build_mcp_tool_info(
                py_file,
                builtin=builtin,
                package=package,
                base_names=base_names,
                attrs=attrs,
            )

    return None


def _extract_endpoint_metadata_attrs(node: ast.ClassDef) -> dict[str, Any]:
    """Pull endpoint metadata class attributes (name/description/etc.) from a ClassDef."""
    name = ""
    description = ""
    code_modifying = False
    satisfies: list[str] | None = None
    satisfies_args: dict[str, str] | None = None

    for item in node.body:
        # Handle both `name = "x"` (Assign) and `name: str = "x"` (AnnAssign)
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            attr_name = item.target.id
            val = _get_constant_value(item.value) if item.value else None
        elif isinstance(item, ast.Assign):
            if len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name):
                continue
            attr_name = item.targets[0].id
            val = _get_constant_value(item.value)
        else:
            continue

        if attr_name == "name" and isinstance(val, str):
            name = val
        elif attr_name == "description" and isinstance(val, str):
            description = val
        elif attr_name == "code_modifying" and isinstance(val, bool):
            code_modifying = val
        elif attr_name == "satisfies" and isinstance(val, list):
            satisfies = val
        elif attr_name == "satisfies_args" and isinstance(val, dict):
            satisfies_args = val

    return {
        "name": name,
        "description": description,
        "code_modifying": code_modifying,
        "satisfies": satisfies,
        "satisfies_args": satisfies_args,
    }


def _build_mcp_tool_info(
    py_file: Path,
    *,
    builtin: bool,
    package: str,
    base_names: set[str],
    attrs: dict[str, Any],
) -> McpToolInfo:
    """Assemble MCP endpoint metadata from extracted class attributes."""
    rel_path = f"{package}/{py_file.name}" if builtin and package else str(py_file)
    if "BooleyFlow" in base_names:
        kind = "flow"
    elif "Specialist" in base_names:
        kind = "specialist"
    else:
        kind = "mcp_tool"
    satisfies = attrs["satisfies"]
    satisfies_args = attrs["satisfies_args"]
    return McpToolInfo(
        name=attrs["name"],
        path=rel_path,
        description=attrs["description"],
        code_modifying=attrs["code_modifying"],
        satisfies=tuple(satisfies) if satisfies else (),
        satisfies_args=satisfies_args if satisfies_args else None,
        kind=kind,
    )


def _get_base_names(classdef: ast.ClassDef) -> set[str]:
    """Extract base class names from a ClassDef AST node."""
    names: set[str] = set()
    for base in classdef.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _get_constant_value(node: ast.expr) -> Any:  # noqa: PLR0911 — one early return per AST literal kind (Constant/List/Dict/...)
    """Extract constant value from an AST node.

    Handles Constant, List/Tuple (of Constants), and Dict (of Constant
    keys/values.
    Returns None for non-literal or computed expressions.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        items = []
        for elt in node.elts:
            v = _get_constant_value(elt)
            if v is None:
                return None
            items.append(v)
        return items
    if isinstance(node, ast.Tuple):
        items = []
        for elt in node.elts:
            v = _get_constant_value(elt)
            if v is None:
                return None
            items.append(v)
        return tuple(items)
    if isinstance(node, ast.Dict):
        result = {}
        for k, v in zip(node.keys, node.values, strict=False):
            if k is None:
                return None
            kv = _get_constant_value(k)
            vv = _get_constant_value(v)
            if kv is None or vv is None:
                return None
            result[kv] = vv
        return result
    return None


def build_criterion_endpoint_map(
    criteria_defs: dict[str, Any],
    endpoints: list[McpToolInfo],
) -> dict[str, tuple[str, str]]:
    """Auto-build criterion -> (endpoint_command, workflow_region) map from criteria defs and endpoint metadata.

    Args:
        criteria_defs: expanded criterion name -> CriterionDef (from expand_criteria_defs)
        endpoints: discovered endpoint metadata with satisfies/satisfies_args

    Returns:
        Dict mapping criterion_name_prefix -> (endpoint_command, workflow_region).
    """
    result: dict[str, tuple[str, str]] = {}
    for endpoint in endpoints:
        for crit_name in endpoint.satisfies:
            # Find the CriterionDef for this base criterion name
            # Match by exact name or by prefix (expanded per_target entries
            # share the same CriterionDef)
            crit_def = criteria_defs.get(crit_name)
            if crit_def is None:
                # Try to find via any expanded key that starts with this name
                for _expanded_key, cdef in criteria_defs.items():
                    if cdef.name == crit_name:
                        crit_def = cdef
                        break
            if crit_def is None:
                logger.warning(
                    "MCP endpoint %r claims satisfies=%r but no matching criterion def found",
                    endpoint.name,
                    crit_name,
                )
                continue

            # Build endpoint command string
            args_str = ""
            if endpoint.satisfies_args and crit_name in endpoint.satisfies_args:
                args_str = " " + endpoint.satisfies_args[crit_name]
            endpoint_command = endpoint.name + args_str

            result[crit_name] = (endpoint_command, crit_def.workflow_region)

    return result
