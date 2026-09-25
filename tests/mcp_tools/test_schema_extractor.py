"""Tests for endpoints.schema_extractor — argparse-to-JSON-schema conversion."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from booley.mcp.application import McpApplication
from booley.mcp.flow_adapter import flow_schema
from booley.mcp.schema_extractor import extract_schema
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from booley.specialists.mutation_tester import MutationTesterSpecialist
from booley.specialists.reviewer import ReviewerSpecialist
from booley.specialists.specialist import VALID_TIERS

# --- Helpers ---


def _make_parser(**kwargs) -> argparse.ArgumentParser:
    """Create a minimal parser for testing."""
    return argparse.ArgumentParser(prog="test", **kwargs)


# --- Type mapping tests ---


class TestTypeMapping:
    def test_str_default(self):
        p = _make_parser()
        p.add_argument("--name")
        schema = extract_schema(p)
        assert schema["properties"]["name"]["type"] == "string"

    def test_str_explicit(self):
        p = _make_parser()
        p.add_argument("--name", type=str)
        schema = extract_schema(p)
        assert schema["properties"]["name"]["type"] == "string"

    def test_int(self):
        p = _make_parser()
        p.add_argument("--count", type=int)
        schema = extract_schema(p)
        assert schema["properties"]["count"]["type"] == "integer"

    def test_float(self):
        p = _make_parser()
        p.add_argument("--ratio", type=float)
        schema = extract_schema(p)
        assert schema["properties"]["ratio"]["type"] == "number"

    def test_path(self):
        p = _make_parser()
        p.add_argument("--dir", type=Path)
        schema = extract_schema(p)
        assert schema["properties"]["dir"]["type"] == "string"

    def test_store_true(self):
        p = _make_parser()
        p.add_argument("--verbose", action="store_true")
        schema = extract_schema(p)
        prop = schema["properties"]["verbose"]
        assert prop["type"] == "boolean"
        assert prop["default"] is False

    def test_store_false(self):
        p = _make_parser()
        p.add_argument("--no-cache", action="store_false", dest="cache")
        schema = extract_schema(p)
        prop = schema["properties"]["cache"]
        assert prop["type"] == "boolean"
        assert prop["default"] is True

    def test_choices(self):
        p = _make_parser()
        p.add_argument("--level", choices=["low", "med", "high"])
        schema = extract_schema(p)
        prop = schema["properties"]["level"]
        assert prop["type"] == "string"
        assert prop["enum"] == ["low", "med", "high"]

    def test_append(self):
        p = _make_parser()
        p.add_argument("--tag", action="append")
        schema = extract_schema(p)
        prop = schema["properties"]["tag"]
        assert prop["type"] == "array"
        assert prop["items"]["type"] == "string"

    def test_nargs_plus(self):
        p = _make_parser()
        p.add_argument("--files", nargs="+")
        schema = extract_schema(p)
        prop = schema["properties"]["files"]
        assert prop["type"] == "array"
        assert prop["items"]["type"] == "string"

    def test_nargs_star(self):
        p = _make_parser()
        p.add_argument("--args", nargs="*")
        schema = extract_schema(p)
        assert schema["properties"]["args"]["type"] == "array"

    def test_nargs_remainder(self):
        p = _make_parser()
        p.add_argument("extra", nargs=argparse.REMAINDER)
        schema = extract_schema(p)
        assert schema["properties"]["extra"]["type"] == "array"

    def test_unrecognized_type_fallback(self):
        p = _make_parser()
        p.add_argument("--custom", type=lambda s: s.upper())
        schema = extract_schema(p)
        assert schema["properties"]["custom"]["type"] == "string"


# --- Filtering tests ---


class TestFiltering:
    def test_filtered_dests(self):
        p = _make_parser()
        p.add_argument("--report-dir", type=Path)
        p.add_argument("--model", choices=["a", "b"])
        p.add_argument("--instruction")
        p.add_argument("--transcript-dir", type=Path)
        p.add_argument("--max-turns", type=int)
        p.add_argument("--timeout", type=int)
        p.add_argument("--keep-me")
        schema = extract_schema(p)
        assert {"keep_me", "model", "max_turns"} <= schema["properties"].keys()
        assert schema["additionalProperties"] is False
        for filtered in (
            "report_dir",
            "instruction",
            "transcript_dir",
            "timeout",
            "help",
        ):
            assert filtered not in schema["properties"]

    def test_work_dir_exposed_with_curated_schema(self):
        """work_dir is agent-facing (worktree retargeting) but its extracted
        form would bake the server's import-time cwd — a Path object — into
        the JSON schema, so a curated property replaces extraction."""
        p = _make_parser()
        p.add_argument("--work-dir", type=Path, default=Path.cwd(), help="Working directory")
        schema = extract_schema(p)
        prop = schema["properties"]["work_dir"]
        assert prop["type"] == "string"
        assert "worktree" in prop["description"]
        # No default: absent means "use the endpoint's own cwd", and a baked
        # Path.cwd() would not be JSON-serializable anyway.
        assert "default" not in prop
        assert "work_dir" not in schema.get("required", [])

    def test_target_not_filtered(self):
        p = _make_parser()
        p.add_argument("--target", default="")
        schema = extract_schema(p)
        assert "target" in schema["properties"]


# --- Required field tests ---


class TestRequired:
    def test_positional_required(self):
        p = _make_parser()
        p.add_argument("input_file")
        schema = extract_schema(p)
        assert "input_file" in schema.get("required", [])

    def test_optional_not_required(self):
        p = _make_parser()
        p.add_argument("--verbose", action="store_true")
        schema = extract_schema(p)
        assert "required" not in schema or "verbose" not in schema["required"]

    def test_required_flag(self):
        p = _make_parser()
        p.add_argument("--name", required=True)
        schema = extract_schema(p)
        assert "name" in schema.get("required", [])

    def test_default_preserves_value(self):
        p = _make_parser()
        p.add_argument("--count", type=int, default=42)
        schema = extract_schema(p)
        assert schema["properties"]["count"]["default"] == 42


@pytest.mark.parametrize(
    ("specialist", "required", "removed"),
    [
        (
            ReviewerSpecialist,
            {"scope", "category", "focus"},
            {"target", "diff_ref", "ticket"},
        ),
        (
            MutationTesterSpecialist,
            {"target", "scope"},
            {"tb_top", "dut_top", "dut_files"},
        ),
    ],
)
def test_specialist_input_contracts_are_unified(specialist, required, removed) -> None:
    schema = extract_schema(specialist()._parser)
    properties = schema["properties"]

    assert schema["additionalProperties"] is False
    assert required <= set(schema["required"])
    assert properties["max_turns"]["type"] == "integer"
    assert properties["max_turns"]["minimum"] == 1
    assert "model" in properties
    assert {"scope", "steer", "dry_run"} <= set(properties)
    assert properties["steer"]["type"] == "array"
    assert properties["dry_run"]["type"] == "boolean"
    assert removed.isdisjoint(properties)


@pytest.mark.parametrize(
    "specialist",
    [ReviewerSpecialist, MutationTesterSpecialist, CoverageAnalystSpecialist],
)
def test_specialist_schemas_expose_bounded_agent_controls(
    specialist: type[Any],
) -> None:
    schema = flow_schema(specialist())
    properties = schema["properties"]

    assert schema["additionalProperties"] is False
    assert properties["model"] == {
        "type": "string",
        "enum": list(VALID_TIERS),
        "description": properties["model"]["description"],
    }
    assert "default" not in properties["model"]
    assert properties["max_turns"]["type"] == "integer"
    assert properties["max_turns"]["minimum"] == 1
    assert {"report_dir", "transcript_dir", "timeout"}.isdisjoint(properties)


def test_specialist_custom_schema_keeps_public_agent_controls() -> None:
    class CustomSchemaReviewer(ReviewerSpecialist):
        def mcp_schema(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"custom": {"type": "string"}},
            }

    schema = flow_schema(CustomSchemaReviewer())

    assert schema["additionalProperties"] is False
    assert {"custom", "model", "max_turns"} <= schema["properties"].keys()


@pytest.mark.parametrize(
    "specialist",
    [ReviewerSpecialist, MutationTesterSpecialist, CoverageAnalystSpecialist],
)
@pytest.mark.parametrize("undeclared", ["report_dir", "transcript_dir", "timeout", "policy"])
def test_specialist_schema_rejects_undeclared_arguments(
    specialist: type[Any],
    undeclared: str,
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


@pytest.mark.parametrize(
    "specialist",
    [ReviewerSpecialist, MutationTesterSpecialist, CoverageAnalystSpecialist],
)
def test_specialist_schema_accepts_public_agent_controls(specialist: type[Any]) -> None:
    schema = flow_schema(specialist())
    arguments = {
        name: prop.get("enum", ["value"])[0]
        for name in schema.get("required", [])
        if isinstance((prop := schema["properties"][name]), dict)
    }
    arguments.update(model="light", max_turns=1)
    calls: list[dict[str, Any]] = []

    async def dispatch(
        _name: str,
        received: dict[str, Any],
        _source: object,
    ) -> list[object]:
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


# --- Description tests ---


class TestDescription:
    def test_help_becomes_description(self):
        p = _make_parser()
        p.add_argument("--name", help="Your name")
        schema = extract_schema(p)
        assert schema["properties"]["name"]["description"] == "Your name"

    def test_no_help_no_description(self):
        p = _make_parser()
        p.add_argument("--name")
        schema = extract_schema(p)
        assert "description" not in schema["properties"]["name"]


def test_coverage_analyst_accepts_only_exact_campaign_and_instruction():
    schema = flow_schema(CoverageAnalystSpecialist())
    assert "campaign" in schema["required"]
    assert "instruction" in schema["properties"]
    assert {"target", "scope", "steer", "criteria", "reset_waivers", "tb_top"}.isdisjoint(
        schema["properties"]
    )
