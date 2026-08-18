"""Tests for endpoints.schema_extractor — argparse-to-JSON-schema conversion."""

from __future__ import annotations

import argparse
from pathlib import Path

from booley.mcp.schema_extractor import extract_schema

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
        # Only --keep-me should survive
        assert "keep_me" in schema["properties"]
        for filtered in (
            "report_dir",
            "model",
            "instruction",
            "transcript_dir",
            "max_turns",
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
