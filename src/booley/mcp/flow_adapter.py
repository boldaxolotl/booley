"""MCP schema and legacy extension adaptation for deterministic Flows."""

from typing import Any

from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_parser
from booley.mcp.schema_extractor import extract_schema

_PUBLIC_SPECIALIST_DESTS = frozenset({"model", "max_turns"})


def _specialist_schema(endpoint: Any) -> dict[str, Any]:
    """Expose supported per-call controls while keeping infrastructure private."""
    hook = getattr(endpoint, "mcp_schema", None)
    schema = (
        hook()
        if callable(hook)
        else extract_schema(endpoint._parser, public_dests=_PUBLIC_SPECIALIST_DESTS)
    )
    extracted = extract_schema(endpoint._parser, public_dests=_PUBLIC_SPECIALIST_DESTS)
    properties = schema.setdefault("properties", {})
    for dest in _PUBLIC_SPECIALIST_DESTS:
        properties[dest] = extracted["properties"][dest]
    properties["max_turns"].update(type="integer", minimum=1)
    schema["additionalProperties"] = False
    return schema


def flow_schema(endpoint: Any) -> dict[str, Any]:
    """Keep MCP schema policy out of deterministic Flow implementations."""
    from booley.specialists.specialist import Specialist

    if isinstance(endpoint, Specialist):
        return _specialist_schema(endpoint)
    if not isinstance(endpoint, BuiltinFlow):
        hook = getattr(endpoint, "mcp_schema", None)
        return hook() if callable(hook) else extract_schema(endpoint._parser)
    schema = extract_schema(build_parser(endpoint))
    schema["additionalProperties"] = False
    properties = schema["properties"]
    properties.pop("_legacy_timeout_ms", None)
    properties["timeout_ms"].update(type="integer", minimum=1)
    if endpoint.name == "sim":
        properties.pop("_legacy_elab_only", None)
        properties.pop("_legacy_standalone", None)
        # Omission is semantic for manifest resume: even an explicit
        # ``simulate`` conflicts. The Flow defaults only after selecting the
        # non-resume path.
        properties["mode"].pop("default", None)
        # A local file selector is CLI-only. MCP sends exact names directly.
        properties.pop("tests_file", None)
        properties["test"].update(minItems=1, uniqueItems=True)
        properties["test"]["items"]["minLength"] = 1
    return schema
