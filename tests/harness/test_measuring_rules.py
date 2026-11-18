"""The measuring rules: ``booley measure`` and the cheatsheet state them the same way (#4133)."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

from booley.harness import cheatsheet
from tests.harness.test_cheat_metrics import run_cli, sandboxed_cli  # noqa: F401
from tests.harness.test_measure_cli import (
    NOTHING_IN_SCOPE,
    ONE_LINT_TABLE,
    _measuring_environment,  # noqa: F401
    committed_project,
    outcome_of,
    run_command,
    script_children,
)
from tests.measurement.metrics_project_support import write_listing_project

HEADING = "Metric-Driven Development"


def _rules() -> ModuleType:
    """The rules module, imported inside the test so a missing module fails the test body."""
    try:
        return importlib.import_module("booley.measurement.measuring_rules")
    except ModuleNotFoundError:
        pytest.fail("booley.measurement.measuring_rules does not exist")


def _squash(text: str) -> str:
    return " ".join(text.split())


def _section_source() -> str:
    """The raw cheatsheet text of the Metric-Driven Development section."""
    text = (
        Path(cheatsheet.__file__)
        .parent.parent.joinpath("data", "cheatsheet.md")
        .read_text(encoding="utf-8")
    )
    match = re.search(rf"^### {HEADING}\n(.*?)(?=^### )", text, re.DOTALL | re.MULTILINE)
    assert match, f"cheatsheet.md has no '### {HEADING}' section"
    return match.group(1)


def test_cheatsheet_section_states_every_measuring_rule() -> None:
    rules = _rules()
    section = _section_source()
    bullets = [_squash(line[2:]) for line in section.splitlines() if line.startswith("- ")]
    assert bullets == [_squash(rule) for rule in rules.MEASURING_RULES]


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_an_unmet_measurement_ends_with_the_exit_1_rule(
    as_json: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rules = _rules()
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [3]))

    code = run_command(monkeypatch, project, *(["--json"] if as_json else []))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.rstrip().splitlines()[-1] == rules.UNMET_RULE
    assert rules.ERROR_RULE not in captured.err
    if as_json:
        assert isinstance(json.loads(captured.out), dict)


def test_an_errored_measurement_ends_with_the_exit_2_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = _rules()
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [None]))

    code = run_command(monkeypatch, project)

    err = capsys.readouterr().err
    assert code == 2
    assert err.rstrip().splitlines()[-1] == rules.ERROR_RULE
    assert rules.UNMET_RULE not in err


def test_a_refused_measurement_ends_with_the_exit_2_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = _rules()
    project = committed_project(tmp_path, "[defaults.sim]\ntests_failed_max = 0\n")
    script_children(monkeypatch, lambda *_: pytest.fail("no child may run"))

    code = run_command(monkeypatch, project)

    lines = capsys.readouterr().err.rstrip().splitlines()
    assert code == 2
    assert NOTHING_IN_SCOPE in lines[-2]
    assert lines[-1] == rules.ERROR_RULE


def test_a_failed_measurement_and_a_missing_store_end_with_the_exit_2_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = _rules()
    project = committed_project(tmp_path, ONE_LINT_TABLE)

    def crash(*_args: object, **_options: object) -> list[object]:
        raise RuntimeError("boom")

    script_children(monkeypatch, lambda *_: pytest.fail("no child may run"))
    monkeypatch.setattr("booley.harness.measure_command.prepare_children", crash)

    assert run_command(monkeypatch, project) == 2

    err = capsys.readouterr().err
    assert "measurement failed: boom" in err
    assert err.rstrip().splitlines()[-1] == rules.ERROR_RULE


def test_no_main_booley_directory_ends_with_the_exit_2_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = _rules()
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    monkeypatch.setattr(
        "booley.harness.measure_command.StandaloneJob.prepare", lambda *_a, **_k: None
    )

    assert run_command(monkeypatch, project) == 2

    err = capsys.readouterr().err
    assert "no Main Booley Directory" in err
    assert err.rstrip().splitlines()[-1] == rules.ERROR_RULE


def test_met_and_cancelled_measurements_print_no_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = _rules()
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    assert run_command(monkeypatch, project) == 0
    err = capsys.readouterr().err
    assert rules.UNMET_RULE not in err
    assert rules.ERROR_RULE not in err

    def cancel(*_args: object, **_options: object) -> list[object]:
        raise KeyboardInterrupt

    monkeypatch.setattr("booley.harness.measure_command.run_children", cancel)
    with pytest.raises(KeyboardInterrupt):
        run_command(monkeypatch, project, "--again")
    err = capsys.readouterr().err
    assert rules.UNMET_RULE not in err
    assert rules.ERROR_RULE not in err


def test_exit_rule_names_the_rule_of_each_exit_code() -> None:
    rules = _rules()
    assert rules.exit_rule(1) == rules.UNMET_RULE
    assert rules.exit_rule(2) == rules.ERROR_RULE
    assert rules.exit_rule(0) is None
    assert rules.exit_rule(130) is None


def test_cheat_metrics_prints_the_section_then_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = _rules()
    project = write_listing_project(tmp_path)

    assert run_cli(monkeypatch, project, "cheat", "--metrics") == 0

    out = _squash(capsys.readouterr().out).replace("`", "")
    positions = [out.index(HEADING)]
    for rule in rules.MEASURING_RULES:
        positions.append(out.index(_squash(rule).replace("`", "")))
    positions.append(out.index("[sim_core.sim]"))
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


def test_the_measure_tool_description_states_every_measuring_rule() -> None:
    """The MCP tool, the cheatsheet and the exit 1 and exit 2 output carry one text (#4134)."""
    rules = _rules()
    try:
        tool = importlib.import_module("booley.mcp.measure_tool")
    except ModuleNotFoundError:
        pytest.fail("booley.mcp.measure_tool does not exist")

    description = tool.measure_tool_def()["description"]

    _, marker, listed = description.partition("Rules:\n")
    assert marker, "the description has no 'Rules:' list"
    bullets = [_squash(line[2:]) for line in listed.splitlines() if line.startswith("- ")]
    assert bullets == [_squash(rule) for rule in rules.MEASURING_RULES]
    # The two rules the command prints after exit 1 and exit 2 are in that list.
    assert _squash(rules.exit_rule(1)) in bullets
    assert _squash(rules.exit_rule(2)) in bullets
