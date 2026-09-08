"""MCP schema and legacy extension adaptation for deterministic Flows."""

from typing import Any

from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_parser
from booley.mcp.schema_extractor import extract_schema


def flow_schema(endpoint: Any) -> dict[str, Any]:
    """Keep MCP schema policy out of deterministic Flow implementations."""
    if not isinstance(endpoint, BuiltinFlow):
        hook = getattr(endpoint, "mcp_schema", None)
        return hook() if callable(hook) else extract_schema(endpoint._parser)
    schema = extract_schema(build_parser(endpoint))
    schema["additionalProperties"] = False
    properties = schema["properties"]
    properties.pop("_legacy_timeout_ms", None)
    properties["timeout_ms"].update(type="integer", minimum=1)
    if endpoint.name == "sim":
        properties.pop("coverage", None)  # #213: keep collection hidden until release gate
        properties.pop("_legacy_elab_only", None)
        properties.pop("_legacy_standalone", None)
        properties["mode"]["default"] = "simulate"
    return schema
