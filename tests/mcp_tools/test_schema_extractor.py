"""Tests for endpoints.schema_extractor — argparse-to-JSON-schema conversion."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from booley.flows.mutation.flow import MutationFlow
from booley.mcp.application import McpApplication
from booley.mcp.base import McpTool, McpToolResult
from booley.mcp.flow_adapter import flow_schema
from booley.mcp.schema_extractor import extract_schema
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from booley.specialists.reviewer import ReviewerSpecialist

# --- Helpers ---


def _make_parser(**kwargs) -> argparse.ArgumentParser:
    """Create a minimal parser for testing."""
    return argparse.ArgumentParser(prog="test", **kwargs)


def test_custom_flow_keeps_its_model_and_turn_arguments() -> None:
    from booley.mcp.server import (  # no public entry short of a running server
        _params_to_argv,
    )

    class CustomFlow(McpTool):
        name = "custom_flow"
        endpoint_kind = "flow"

        def _add_args(self, parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--model", required=True)
            parser.add_argument("--max-turns", type=int, default=7)

        def _run(self) -> McpToolResult:
            return McpToolResult(exit_code=0)

    endpoint = CustomFlow()
    schema = flow_schema(endpoint)
    assert schema["properties"]["model"]["type"] == "string"
    assert schema["properties"]["max_turns"] == {"type": "integer", "default": 7}
    assert "model" in schema["required"]
    received = []

    async def dispatch(_name, arguments, _source):
        # The conversion the server applies to MCP arguments before the endpoint parses them.
        args = endpoint.parse_args(_params_to_argv(arguments))
        received.append((args.model, args.max_turns))
        return []

    application = McpApplication(
        [{"name": endpoint.name, "description": "", "schema": schema}],
        dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=lambda _message: None,
    )
    result = asyncio.run(application.call_tool(endpoint.name, {"model": "logic", "max_turns": 3}))
    assert result.is_error is False
    assert received == [("logic", 3)]


# --- Argparse forms ---


@pytest.mark.parametrize(
    ("args", "kwargs", "dest", "expected", "required"),
    [
        (("--name",), {}, "name", {"type": "string"}, False),
        (("--name",), {"type": str}, "name", {"type": "string"}, False),
        (("--count",), {"type": int}, "count", {"type": "integer"}, False),
        (("--ratio",), {"type": float}, "ratio", {"type": "number"}, False),
        (("--dir",), {"type": Path}, "dir", {"type": "string"}, False),
        (
            ("--verbose",),
            {"action": "store_true"},
            "verbose",
            {"type": "boolean", "default": False},
            False,
        ),
        (
            ("--no-cache",),
            {"action": "store_false", "dest": "cache"},
            "cache",
            {"type": "boolean", "default": True},
            False,
        ),
        (
            ("--level",),
            {"choices": ["low", "med", "high"]},
            "level",
            {"type": "string", "enum": ["low", "med", "high"]},
            False,
        ),
        (
            ("--tag",),
            {"action": "append"},
            "tag",
            {"type": "array", "items": {"type": "string"}},
            False,
        ),
        (
            ("--files",),
            {"nargs": "+"},
            "files",
            {"type": "array", "items": {"type": "string"}},
            False,
        ),
        (("--args",), {"nargs": "*"}, "args", {"type": "array"}, False),
        (("extra",), {"nargs": argparse.REMAINDER}, "extra", {"type": "array"}, True),
        (("--custom",), {"type": lambda s: s.upper()}, "custom", {"type": "string"}, False),
        (("input_file",), {}, "input_file", {"type": "string"}, True),
        (("--name",), {"required": True}, "name", {"type": "string"}, True),
        (("--count",), {"type": int, "default": 42}, "count", {"default": 42}, False),
        (("--name",), {"help": "Your name"}, "name", {"description": "Your name"}, False),
        (
            ("--target",),
            {"action": "append", "required": True},
            "target",
            {"type": "string"},  # one Target, never an array
            True,
        ),
    ],
)
def test_extract_schema_maps_each_argparse_form(args, kwargs, dest, expected, required):
    p = _make_parser()
    p.add_argument(*args, **kwargs)

    schema = extract_schema(p)

    prop = schema["properties"][dest]
    assert {key: prop[key] for key in expected} == expected
    assert (dest in schema.get("required", [])) is required
    if "description" not in expected:
        assert "description" not in prop
    if dest == "target":
        assert "items" not in prop


def test_extract_schema_hides_the_agent_owned_arguments():
    p = _make_parser()
    for flag in (
        "--report-dir",
        "--instruction",
        "--transcript-dir",
        "--max-turns",
        "--timeout-ms",
    ):
        p.add_argument(flag)
    p.add_argument("--keep-me")

    schema = extract_schema(p)

    assert set(schema["properties"]) == {"keep_me"}
    assert schema["additionalProperties"] is False


def test_work_dir_exposed_with_curated_schema():
    """work_dir is agent-facing (worktree retargeting) but its extracted form would bake the
    server's import-time cwd, a Path object, into the JSON schema, so a curated property
    replaces extraction."""
    p = _make_parser()
    p.add_argument("--work-dir", type=Path, default=Path.cwd(), help="Working directory")

    schema = extract_schema(p)

    prop = schema["properties"]["work_dir"]
    assert prop["type"] == "string"
    assert "worktree" in prop["description"]
    # No default: absent means "use the endpoint's own cwd", and a baked Path.cwd() would not
    # be JSON-serializable anyway.
    assert "default" not in prop
    assert "work_dir" not in schema.get("required", [])


def test_reviewer_input_contract_is_unified() -> None:
    properties = flow_schema(ReviewerSpecialist())["properties"]

    assert {"scope", "dry_run"} <= set(properties)
    assert properties["dry_run"]["type"] == "boolean"
    assert "seed" not in properties
    assert properties["steer"]["type"] == "array"
    assert {"target", "diff_ref", "ticket"}.isdisjoint(properties)


def test_mutation_flow_input_contract() -> None:
    schema = flow_schema(MutationFlow())
    properties = schema["properties"]

    assert schema["additionalProperties"] is False
    assert {"target"} <= set(schema.get("required", []))
    assert {"model", "max_turns"}.isdisjoint(properties)
    assert {"scope", "dry_run"} <= set(properties)
    assert properties["dry_run"]["type"] == "boolean"
    assert properties["seed"]["type"] == "integer"
    assert {"tb_top", "dut_top", "dut_files", "steer", "regen_lock"}.isdisjoint(properties)


def test_mutation_target_schema_remains_single_required_scalar() -> None:
    schema = flow_schema(MutationFlow())
    target = schema["properties"]["target"]

    assert target["type"] == "string"
    assert "items" not in target
    assert "target" in schema["required"]
    assert "Exactly one Simulation Target" in target["description"]


@pytest.mark.parametrize("specialist", [ReviewerSpecialist, CoverageAnalystSpecialist])
def test_specialist_schemas_expose_bounded_agent_controls(specialist: type[Any]) -> None:
    schema = flow_schema(specialist())
    properties = schema["properties"]

    assert schema["additionalProperties"] is False
    assert {"max_turns", "timeout_ms", "timeout", "report_dir", "transcript_dir"}.isdisjoint(
        properties
    )


def test_specialist_custom_schema_keeps_public_agent_controls() -> None:
    class CustomSchemaReviewer(ReviewerSpecialist):
        def mcp_schema(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "custom": {"type": "string"},
                    "max_turns": {"type": "integer"},
                    "timeout_ms": {"type": "integer"},
                },
                "required": ["custom", "max_turns", "timeout_ms"],
            }

    schema = flow_schema(CustomSchemaReviewer())

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"custom"}
    assert schema["required"] == ["custom"]


@pytest.mark.parametrize("specialist", [ReviewerSpecialist, CoverageAnalystSpecialist])
@pytest.mark.parametrize(
    "undeclared",
    ["report_dir", "transcript_dir", "timeout", "timeout_ms", "model", "max_turns", "policy"],
)
def test_specialist_schema_rejects_undeclared_arguments(
    specialist: type[Any], undeclared: str
) -> None:
    schema = flow_schema(specialist())
    arguments = {
        name: prop.get("enum", ["value"])[0]
        for name in schema.get("required", [])
        if isinstance((prop := schema["properties"][name]), dict)
    }
    arguments[undeclared] = "injected"
    dispatched = False

    async def dispatch(*_args: object) -> list[object]:
        nonlocal dispatched
        dispatched = True
        return []

    application = McpApplication(
        [{"name": specialist.name, "description": "", "schema": schema}],
        dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=lambda _message: None,
    )

    result = asyncio.run(application.call_tool(specialist.name, arguments))

    assert result.is_error is True
    assert dispatched is False


@pytest.mark.parametrize("specialist", [ReviewerSpecialist, CoverageAnalystSpecialist])
def test_specialist_schema_accepts_omitted_agent_controls(specialist: type[Any]) -> None:
    schema = flow_schema(specialist())
    arguments = {
        name: prop.get("enum", ["value"])[0]
        for name in schema.get("required", [])
        if isinstance((prop := schema["properties"][name]), dict)
    }
    calls: list[dict[str, Any]] = []

    async def dispatch(_name: str, received: dict[str, Any], _source: object) -> list[object]:
        calls.append(received)
        return []

    application = McpApplication(
        [{"name": specialist.name, "description": "", "schema": schema}],
        dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=lambda _message: None,
    )

    invalid = dict(arguments, max_turns=0)
    invalid_result = asyncio.run(application.call_tool(specialist.name, invalid))
    result = asyncio.run(application.call_tool(specialist.name, arguments))

    assert invalid_result.is_error is True
    assert result.is_error is False
    assert calls == [arguments]


def test_coverage_analyst_accepts_only_exact_simulation_record_and_instruction():
    schema = flow_schema(CoverageAnalystSpecialist())
    assert "record" in schema["required"]
    assert "instruction" in schema["properties"]
    assert {"target", "scope", "steer", "criteria", "reset_waivers", "tb_top"}.isdisjoint(
        schema["properties"]
    )
