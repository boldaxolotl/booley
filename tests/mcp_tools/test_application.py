"""Tests for the Booley-owned MCP application interface."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from jsonschema.exceptions import SchemaError

from booley.mcp.application import McpApplication, UnknownMcpToolError


def _definition(name: str = "echo", **extra):
    return {
        "name": name,
        "description": "Echo text",
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        **extra,
    }


def _application(definitions, calls=None, errors=None):
    calls = calls if calls is not None else []
    errors = errors if errors is not None else []

    async def dispatch(name, arguments, source):
        calls.append((name, arguments, source))
        return [SimpleNamespace(text=arguments["text"])]

    return McpApplication(
        definitions,
        dispatch=dispatch,
        canonicalize=lambda name: {"run_echo": "echo"}.get(name, name),
        on_discovery_error=errors.append,
    )


def test_catalog_is_deterministic_and_dispatch_accepts_alias() -> None:
    calls: list[tuple] = []
    application = _application([_definition("zeta"), _definition()], calls=calls)

    assert [tool.name for tool in application.list_tools()] == ["echo", "zeta"]
    payload = asyncio.run(application.call_tool("run_echo", {"text": "hello"}))

    assert payload.content[0].text == "hello"
    assert calls[0][0:2] == ("echo", {"text": "hello"})


def test_invalid_arguments_return_tool_error_with_json_path() -> None:
    application = _application([_definition()])

    payload = asyncio.run(application.call_tool("echo", {"text": 7}))

    assert payload.is_error is True
    assert payload.content[0].text.startswith("Invalid tool arguments at $.text:")


def test_unknown_tool_is_a_distinct_protocol_error() -> None:
    application = _application([_definition()])

    with pytest.raises(UnknownMcpToolError, match="missing"):
        asyncio.run(application.call_tool("missing", {}))


def test_duplicate_canonical_alias_fails_catalog_construction() -> None:
    with pytest.raises(ValueError, match="alias"):
        _application([_definition("echo"), _definition("run_echo")])


def test_invalid_builtin_schema_fails_startup() -> None:
    broken = _definition()
    broken["schema"] = {"type": "definitely-not-a-json-schema-type"}

    with pytest.raises(SchemaError):
        _application([broken])


def test_invalid_custom_schema_is_excluded_with_actionable_error() -> None:
    errors: list[str] = []
    broken = _definition(is_custom=True)
    broken["schema"] = {"type": "definitely-not-a-json-schema-type"}

    application = _application([broken, _definition("valid")], errors=errors)

    assert [tool.name for tool in application.list_tools()] == ["valid"]
    assert errors and errors[0].startswith("INVALID CUSTOM MCP SCHEMA: echo:")


def test_explicit_older_schema_dialect_is_respected() -> None:
    definition = _definition()
    definition["schema"]["$schema"] = "http://json-schema.org/draft-07/schema#"
    application = _application([definition])

    payload = asyncio.run(application.call_tool("echo", {"text": "valid"}))

    assert payload.is_error is False
