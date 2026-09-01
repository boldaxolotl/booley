"""Booley-owned MCP tool catalog, validation, and dispatch interface.

The MCP SDK is a transport adapter.  This module owns the application-level
contract that both the stdio and HTTP adapters expose: one deterministic tool
catalog and one validated call path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for


@dataclass(frozen=True, slots=True)
class McpToolDefinition:
    """One tool advertised on the MCP wire."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class McpTextBlock:
    """SDK-independent text returned by a Booley MCP tool."""

    text: str


@dataclass(frozen=True, slots=True)
class McpToolPayload:
    """Normalized result of one application-level MCP tool call."""

    content: tuple[McpTextBlock, ...]
    structured_content: dict[str, Any] | None = None
    is_error: bool = False


class UnknownMcpToolError(ValueError):
    """The caller named a tool outside the advertised catalog."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


Dispatch = Callable[[str, dict[str, Any], Mapping[str, Any]], Awaitable[object]]
Canonicalize = Callable[[str], str]
DiscoveryError = Callable[[str], None]


class McpApplication:
    """Deep MCP application module shared by every transport adapter.

    The public interface is deliberately two methods.  Catalog construction,
    alias collision detection, schema dialect selection, input validation,
    and result normalization stay local to this implementation.
    """

    def __init__(
        self,
        definitions: Iterable[Mapping[str, Any]],
        *,
        dispatch: Dispatch,
        canonicalize: Canonicalize,
        on_discovery_error: DiscoveryError,
    ) -> None:
        self._dispatch = dispatch
        self._canonicalize = canonicalize
        self._definitions: dict[str, McpToolDefinition] = {}
        self._sources: dict[str, Mapping[str, Any]] = {}
        self._validators: dict[str, Validator] = {}
        for source in definitions:
            try:
                self._register(source)
            except SchemaError as exc:
                if not source.get("is_custom"):
                    raise
                name = str(source.get("name") or "<unnamed>")
                on_discovery_error(f"INVALID CUSTOM MCP SCHEMA: {name}: {exc.message}")

    def list_tools(self) -> list[McpToolDefinition]:
        """Return the advertised catalog in deterministic wire-name order."""
        return [self._definitions[name] for name in sorted(self._definitions)]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolPayload:
        """Validate and dispatch one tool call through the advertised catalog."""
        canonical_name = self._canonicalize(name)
        definition = self._definitions.get(canonical_name)
        if definition is None:
            raise UnknownMcpToolError(canonical_name)
        error = next(self._validators[canonical_name].iter_errors(arguments), None)
        if error is not None:
            return McpToolPayload(
                content=(McpTextBlock(_format_validation_error(error)),),
                is_error=True,
            )
        result = await self._dispatch(canonical_name, arguments, self._sources[canonical_name])
        return _normalize_payload(result)

    def _register(self, source: Mapping[str, Any]) -> None:
        name = str(source["name"])
        canonical_name = self._canonicalize(name)
        if name != canonical_name:
            raise ValueError(
                f"MCP tool {name!r} is an alias of {canonical_name!r}; advertise only canonical names"
            )
        if canonical_name in self._definitions:
            raise ValueError(f"Duplicate MCP wire name or canonical alias: {canonical_name!r}")
        schema = dict(source["schema"])
        validator_type = validator_for(schema, default=Draft202012Validator)
        validator_type.check_schema(schema)
        output_schema = source.get("output_schema")
        if output_schema is not None:
            output_schema = dict(output_schema)
            output_validator = validator_for(output_schema, default=Draft202012Validator)
            output_validator.check_schema(output_schema)
        self._definitions[canonical_name] = McpToolDefinition(
            name=canonical_name,
            description=str(source.get("description") or ""),
            input_schema=schema,
            output_schema=output_schema,
        )
        self._sources[canonical_name] = source
        self._validators[canonical_name] = validator_type(schema)


def _format_validation_error(error: ValidationError) -> str:
    """Render one rejected argument without echoing its possibly-sensitive value."""
    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return f"Invalid tool arguments at {path}: {error.message}"


def _normalize_payload(result: object) -> McpToolPayload:
    """Normalize the server's historical list/tuple results at the application seam."""
    structured: dict[str, Any] | None = None
    blocks = result
    if isinstance(result, tuple):
        if len(result) != 2 or not isinstance(result[1], dict):
            raise TypeError("MCP tool returned an invalid structured result")
        blocks, structured = result
    if not isinstance(blocks, list):
        raise TypeError("MCP tool returned content that is not a list")
    normalized: list[McpTextBlock] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            raise TypeError("Booley MCP tools currently support text content only")
        normalized.append(McpTextBlock(text))
    return McpToolPayload(content=tuple(normalized), structured_content=structured)
