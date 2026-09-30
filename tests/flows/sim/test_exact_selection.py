from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from booley.flows.builtin_cli import build_parser, parse_request
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


def test_typed_tests_file_uses_the_same_exact_selection(tmp_path) -> None:
    selected = tmp_path / "selected.txt"
    selected.write_text("# campaign\ntail\n\nquick\n", encoding="utf-8")

    request = SimRequest(target="sim", tests_file=selected)

    assert request.test == ("tail", "quick")
    assert request.tests_file is None


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
        ["--no-waivers"],
    ],
)
def test_resume_rejects_selection_conflicts(tmp_path, conflict, capsys) -> None:
    with pytest.raises(SystemExit):
        parse_request(
            SimulateFlow(),
            ["--resume-from", str(tmp_path / "manifest.json"), *conflict],
        )
    assert conflict[0] in capsys.readouterr().err


def test_typed_resume_rejects_no_waivers(tmp_path) -> None:
    with pytest.raises(ValueError, match="no_waivers"):
        SimRequest(resume_from=tmp_path / "manifest.json", no_waivers=True)


def test_no_waivers_requires_coverage_and_is_recorded_on_request(capsys) -> None:
    request = parse_request(SimulateFlow(), ["--target", "sim", "--coverage", "--no-waivers"])
    assert request.no_waivers is True
    assert parse_request(SimulateFlow(), ["--target", "sim", "--coverage"]).no_waivers is False
    with pytest.raises(SystemExit):
        parse_request(SimulateFlow(), ["--target", "sim", "--no-waivers"])
    assert "--no-waivers requires --coverage" in capsys.readouterr().err
    with pytest.raises(ValueError, match="no_waivers requires coverage"):
        SimRequest(target="sim", no_waivers=True)
    with pytest.raises(ValueError, match="no_waivers must be boolean"):
        SimRequest(target="sim", coverage=True, no_waivers="yes")


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
        "description": "Select exact registered tests (coverage: deterministic sorted order; plain: input order; CLI: repeat; MCP: array)",
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


def test_help_distinguishes_coverage_and_plain_selection_order() -> None:
    help_text = " ".join(build_parser(SimulateFlow()).format_help().split())
    assert "coverage: deterministic sorted order; plain: input order" in help_text


def test_plain_selection_preserves_input_order(tmp_path) -> None:
    flow = SimulateFlow()
    flow.parse_args(
        ["--target", "sim", "--work-dir", str(tmp_path), "--test", "gap", "--test", "full"]
    )
    assert flow._resolve_tests_to_run("sim", {"sim": ["full", "gap"]}) == ["gap", "full"]
