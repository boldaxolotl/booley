"""Extract JSON schemas from argparse parsers for MCP tool registration.

Runs inside the MCP server (Docker runtime) where full imports are safe.
The existing registry uses AST-only parsing (no imports) for host-side
prompt building — this module is separate and complementary.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Args injected by McpTool._add_common_args() and Specialist._add_args() —
# infrastructure-only, not agent-facing.
_FILTERED_DESTS = frozenset(
    {
        "report_dir",  # McpTool._add_common_args()
        "model",
        "instruction",  # Specialist._add_args()
        "transcript_dir",
        "max_turns",  # Specialist._add_args()
        "timeout",  # Specialist._add_args()
        "help",  # standard argparse
    }
)

# Agent-facing schema for the shared --work-dir arg. Curated rather than
# extracted: the argparse default is Path.cwd() evaluated at server import —
# a non-JSON-serializable Path baked to whatever directory the server started
# in — and the CLI help text lacks the worktree guidance agents need. Omitting
# the property keeps the default behavior (the endpoint uses its own cwd).
_WORK_DIR_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Run the endpoint against this checkout instead of the session "
        "workspace. Must be the root of a linked git worktree of the "
        "workspace repo — create one under .booley_project/worktrees/ "
        "with worktree_create.sh. Omit to use the current workspace."
    ),
}


def _argparse_type_to_schema(action: argparse.Action) -> dict[str, Any]:
    """Map a single argparse action to a JSON schema property."""
    if isinstance(action, argparse._StoreTrueAction):
        return {"type": "boolean", "default": False}
    if isinstance(action, argparse._StoreFalseAction):
        return {"type": "boolean", "default": True}
    if isinstance(action, argparse._AppendAction):
        return {"type": "array", "items": {"type": "string"}}

    # nargs produces arrays
    if action.nargs in ("+", "*", argparse.REMAINDER):
        item_type = _map_type_func(action.type)
        return {"type": "array", "items": {"type": item_type}}

    # choices → enum
    if action.choices is not None:
        return {"type": "string", "enum": [str(c) for c in action.choices]}

    # Scalar type mapping
    return {"type": _map_type_func(action.type)}


def _map_type_func(type_func: Any) -> str:
    """Map an argparse type= callable to a JSON schema type string."""
    if type_func is None:
        return "string"
    if type_func is int:
        return "integer"
    if type_func is float:
        return "number"
    if type_func is str:
        return "string"
    if type_func is Path:
        return "string"
    # Fallback for unrecognized callables
    return "string"


def extract_schema(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Extract a JSON schema from an argparse ArgumentParser.

    Returns:
        ``{"type": "object", "properties": {...}, "required": [...]}``
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue

        dest = action.dest
        if dest in _FILTERED_DESTS:
            continue

        if dest == "work_dir":
            # Curated schema (see _WORK_DIR_PROPERTY) — the extracted default
            # would leak the server's import-time cwd as a Path object.
            properties[dest] = dict(_WORK_DIR_PROPERTY)
            continue

        prop = _argparse_type_to_schema(action)

        # Add description from help text
        if action.help and action.help != argparse.SUPPRESS:
            prop["description"] = action.help

        # Add default if present and not None/SUPPRESS
        if (
            action.default is not None
            and action.default is not argparse.SUPPRESS
            and "default" not in prop  # don't override store_true/false defaults
        ):
            prop["default"] = action.default

        properties[dest] = prop

        # Required: no default and not optional (positional args)
        if action.required or (not action.option_strings and action.default is None):
            required.append(dest)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
