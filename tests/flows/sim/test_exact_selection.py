from __future__ import annotations

import pytest

from booley.flows.builtin_cli import parse_request
from booley.flows.sim.flow import SimulateFlow
from booley.mcp.flow_adapter import flow_schema


def test_repeatable_test_is_exact_and_preserves_order() -> None:
    request = parse_request(SimulateFlow(), ["--target", "sim", "--test", "b", "--test", "a"])
    assert request.test == ("b", "a")
    assert request.mode is None


def test_tests_file_ignores_comments_and_blanks(tmp_path) -> None:
    selected = tmp_path / "selected.txt"
    selected.write_text("# campaign\nsmoke\n\nedge\n", encoding="utf-8")
    request = parse_request(SimulateFlow(), ["--target", "sim", "--tests-file", str(selected)])
    assert request.test == ("smoke", "edge")


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
        "items": {"type": "string"},
        "description": "Run one exact registered test (repeat for multiple tests)",
        "default": [],
    }
    assert "skip" not in schema["properties"]
    assert "tests_file" not in schema["properties"]


def test_named_selection_requires_catalog_and_is_exact(tmp_path) -> None:
    flow = SimulateFlow()
    flow.parse_args(["--target", "sim", "--work-dir", str(tmp_path), "--test", "smoke"])
    assert flow._validate_test_selector(["sim"], {}) is not None
    assert flow._validate_test_selector(["sim"], {"sim": ["smoke_long"]}) is not None
    assert flow._validate_test_selector(["sim"], {"sim": ["smoke"]}) is None
