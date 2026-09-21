from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from booley.flows.builtin_cli import parse_request
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.mcp.flow_adapter import flow_schema


def test_repeatable_test_is_exact_and_preserves_order() -> None:
    request = parse_request(SimulateFlow(), ["--target", "sim", "--test", "b", "--test", "a"])
    assert request.test == ("b", "a")
    assert request.mode is None


def test_omitted_test_preserves_absence() -> None:
    request = parse_request(SimulateFlow(), ["--target", "sim"])
    assert request.test is None


@pytest.mark.parametrize("explicit_empty", [(), []])
def test_typed_request_rejects_explicit_empty_test_selection(
    explicit_empty: Any,
) -> None:
    with pytest.raises(ValueError, match="non-empty array"):
        SimRequest(target="sim", test=explicit_empty)


def test_tests_file_ignores_comments_and_blanks(tmp_path) -> None:
    selected = tmp_path / "selected.txt"
    selected.write_text("# campaign\nsmoke\n\nedge\n", encoding="utf-8")
    request = parse_request(SimulateFlow(), ["--target", "sim", "--tests-file", str(selected)])
    assert request.test == ("smoke", "edge")


def test_resume_preserves_omitted_mode_and_requires_no_target(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    request = parse_request(SimulateFlow(), ["--resume-from", str(manifest)])
    assert request.target == ""
    assert request.mode is None
    assert request.resume_from == manifest


@pytest.mark.parametrize(
    "conflict",
    [
        ["--target", "sim"],
        ["--test", "smoke"],
        ["--mode", "simulate"],
        ["--coverage"],
        ["--trace"],
    ],
)
def test_resume_rejects_selection_conflicts(tmp_path, conflict) -> None:
    with pytest.raises(SystemExit):
        parse_request(
            SimulateFlow(),
            ["--resume-from", str(tmp_path / "manifest.json"), *conflict],
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["--target", "sim", "--test", "a", "--test", "a"],
        ["--target", "sim", "--skip", "a"],
    ],
)
def test_invalid_selection_fails_in_cli_normalization(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_request(SimulateFlow(), argv)


def test_mcp_schema_accepts_only_array_test_shape() -> None:
    schema = flow_schema(SimulateFlow())
    assert schema["properties"]["test"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": "Run exact registered test names in caller order (CLI: repeat; MCP: array)",
        "minItems": 1,
        "uniqueItems": True,
    }
    assert "skip" not in schema["properties"]
    assert "tests_file" not in schema["properties"]


def test_mcp_schema_rejects_explicit_empty_test_selection() -> None:
    validator = Draft202012Validator(flow_schema(SimulateFlow()))
    assert not list(validator.iter_errors({"target": "sim"}))
    errors = list(validator.iter_errors({"target": "sim", "test": []}))
    assert len(errors) == 1
    assert errors[0].validator == "minItems"


def test_named_selection_requires_catalog_and_is_exact(tmp_path) -> None:
    flow = SimulateFlow()
    flow.parse_args(["--target", "sim", "--work-dir", str(tmp_path), "--test", "smoke"])
    assert flow._validate_test_selector(["sim"], {}) is not None
    assert flow._validate_test_selector(["sim"], {"sim": ["smoke_long"]}) is not None
    assert flow._validate_test_selector(["sim"], {"sim": ["smoke"]}) is None
