"""MCP schema and legacy extension adaptation for deterministic Flows."""

from typing import Any

from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_parser
from booley.flows.test_runs import MAX_SEED, SEED_REQUEST_RANDOM
from booley.mcp.schema_extractor import SPECIALIST_CLI_ONLY_DESTS, extract_schema, negative_flags


def _specialist_schema(endpoint: Any) -> dict[str, Any]:
    """Keep Specialist runtime controls private, including custom schema hooks."""
    hook = getattr(endpoint, "mcp_schema", None)
    extracted = extract_schema(endpoint._parser)
    if not callable(hook):
        return extracted
    schema = hook()
    properties = schema.setdefault("properties", {})
    for dest in SPECIALIST_CLI_ONLY_DESTS:
        properties.pop(dest, None)
    if "required" in schema:
        schema["required"] = [dest for dest in schema["required"] if dest in properties]
    schema["additionalProperties"] = False
    return schema


def flow_negative_flags(endpoint: Any) -> dict[str, str]:
    """Return the negative option string of each paired boolean the endpoint parses.

    Taken from the parser that parses the call, so ``false`` over MCP reaches
    the endpoint as ``--no-x`` even when a custom schema hook shapes the schema.
    """
    parser = (
        build_parser(endpoint)
        if isinstance(endpoint, BuiltinFlow)
        else getattr(endpoint, "_parser", None)
    )
    return negative_flags(parser) if parser is not None else {}


def flow_schema(endpoint: Any) -> dict[str, Any]:
    """Keep MCP schema policy out of deterministic Flow implementations."""
    if getattr(endpoint, "endpoint_kind", None) == "specialist":
        return _specialist_schema(endpoint)
    if not isinstance(endpoint, BuiltinFlow):
        hook = getattr(endpoint, "mcp_schema", None)
        return hook() if callable(hook) else extract_schema(endpoint._parser)
    schema = extract_schema(build_parser(endpoint))
    schema["additionalProperties"] = False
    properties = schema["properties"]
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
        # One seed for every selected Test Run, or a fresh draw per Test Run.
        properties["seed"] = {
            "description": properties["seed"]["description"],
            "oneOf": [
                {"type": "integer", "minimum": 1, "maximum": MAX_SEED},
                {"type": "string", "enum": [SEED_REQUEST_RANDOM]},
            ],
        }
    return schema
