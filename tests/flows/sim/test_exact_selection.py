from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from booley.flows.builtin_cli import build_parser, parse_request
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.mode import SimulationMode
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


def test_no_waivers_requires_coverage_and_is_recorded_on_request() -> None:
    request = parse_request(SimulateFlow(), ["--target", "sim", "--coverage", "--no-waivers"])
    assert request.no_waivers is True
    assert parse_request(SimulateFlow(), ["--target", "sim", "--coverage"]).no_waivers is False
    with pytest.raises(ValueError, match="no_waivers requires coverage"):
        SimRequest(target="sim", no_waivers=True)
    with pytest.raises(ValueError, match="no_waivers must be boolean"):
        SimRequest(target="sim", coverage=True, no_waivers="yes")


def test_cli_skip_is_not_an_option() -> None:
    with pytest.raises(SystemExit):
        parse_request(SimulateFlow(), ["--target", "sim", "--skip", "a"])


def test_duplicate_test_names_share_one_message(tmp_path, capsys) -> None:
    """One rule, one text: whether the names came from ``test`` or a tests file."""
    listed = tmp_path / "listed.txt"
    listed.write_text("a\n# note\nb\na\n", encoding="utf-8")
    message = "duplicate exact test name(s): a"

    for argv in (["--test", "a", "--test", "a"], ["--tests-file", str(listed)]):
        with pytest.raises(SystemExit):
            parse_request(SimulateFlow(), argv)
        assert message in capsys.readouterr().err
    with pytest.raises(ValueError, match=re.escape(message)):
        SimRequest(test=("a", "a"))
    with pytest.raises(ValueError, match=re.escape(message)):
        SimRequest(tests_file=listed)


def _tests_file(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "selected.txt"
    path.write_text(text, encoding="utf-8")
    return ["--tests-file", str(path)]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--test", "a", "--tests-file", "f.txt"], "--test and --tests-file are mutually"),
        (["--resume-from", "m.json", "--mode", "elab-only"], "--resume-from cannot be combined"),
        (
            ["--resume-from", "m.json", "--elab-only"],
            "--resume-from cannot be combined with --mode",
        ),
        (["--no-waivers"], "--no-waivers requires --coverage"),
        (
            ["--resume-from", "m.json", "--seed", "3", "--mode", "elab-only"],
            "--seed applies to simulation",
        ),
        (["--tests-file", "missing.txt"], "cannot read --tests-file"),
    ],
)
def test_cli_rule_errors_name_flags(argv, message, capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # the relative file names above never exist
    with pytest.raises(SystemExit) as exit_info:
        parse_request(SimulateFlow(), argv)
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_empty_tests_file_names_the_flag(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_request(SimulateFlow(), _tests_file(tmp_path, "# only\n\n"))
    assert exit_info.value.code == 2
    assert "--tests-file contains no test names" in capsys.readouterr().err


def test_typed_rule_errors_name_fields(tmp_path) -> None:
    with pytest.raises(ValueError, match="test and tests_file are mutually exclusive"):
        SimRequest(target="sim", test=("a",), tests_file=tmp_path / "f.txt")
    with pytest.raises(ValueError, match="seed applies to simulation"):
        SimRequest(target="sim", seed=3, mode=SimulationMode.ELAB_ONLY)
    with pytest.raises(ValueError, match="resume_from cannot be combined with mode"):
        SimRequest(resume_from=tmp_path / "m.json", mode=SimulationMode.ELAB_ONLY)


def test_mcp_schema_accepts_only_array_test_shape() -> None:
    schema = flow_schema(SimulateFlow())
    assert schema["properties"]["test"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": "Select Test Runs: test, test+variant, test@seed, test+variant@seed, or test[+variant]@xN for N drawn seeds; a bare test means its default Test Variant (coverage: deterministic sorted order; plain: input order; CLI: repeat; MCP: array)",
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


@pytest.mark.parametrize("dry_run", [False, True])
def test_resume_verbose_parser_to_request_is_output_only(tmp_path, dry_run):
    argv = ["--resume-from", str(tmp_path / "manifest.json"), "--verbose"]
    if dry_run:
        argv.append("--dry-run")
    request = parse_request(SimulateFlow(), argv)
    assert request.verbose is True
    assert request.dry_run is dry_run
    assert request.target == ""
