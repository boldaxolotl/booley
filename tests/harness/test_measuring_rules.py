"""The measuring rules: ``booley measure`` and the cheatsheet state them the same way (#4133)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from booley.harness import cheatsheet
from booley.mcp.measure_tool import measure_tool_def
from booley.measurement.measuring_rules import ERROR_RULE, MEASURING_RULES, UNMET_RULE
from tests.harness.test_cheat_metrics import run_cli, sandboxed_cli  # noqa: F401
from tests.harness.test_measure_cli import (
    ONE_LINT_TABLE,
    _measuring_environment,  # noqa: F401
    committed_project,
    outcome_of,
    run_command,
    script_children,
)
from tests.measurement.metrics_project_support import write_listing_project

HEADING = "Metric-Driven Development"


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
    section = _section_source()
    bullets = [_squash(line[2:]) for line in section.splitlines() if line.startswith("- ")]
    assert bullets == [_squash(rule) for rule in MEASURING_RULES]


@pytest.mark.parametrize(
    ("warnings", "arguments", "code", "rule", "other"),
    [
        pytest.param(3, [], 1, UNMET_RULE, ERROR_RULE, id="unmet-text"),
        pytest.param(3, ["--json"], 1, UNMET_RULE, ERROR_RULE, id="unmet-json"),
        pytest.param(None, [], 2, ERROR_RULE, UNMET_RULE, id="error"),
    ],
)
def test_an_unmet_or_errored_measurement_ends_with_its_rule(
    warnings: int | None,
    arguments: list[str],
    code: int,
    rule: str,
    other: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [warnings]))

    assert run_command(monkeypatch, project, *arguments) == code

    captured = capsys.readouterr()
    assert captured.err.rstrip().splitlines()[-1] == rule
    assert other not in captured.err
    if arguments:
        assert isinstance(json.loads(captured.out), dict)


def test_no_main_booley_directory_ends_with_the_exit_2_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    monkeypatch.setattr(
        "booley.harness.measure_command.StandaloneJob.prepare", lambda *_a, **_k: None
    )

    assert run_command(monkeypatch, project) == 2

    err = capsys.readouterr().err
    assert "no Main Booley Directory" in err
    assert err.rstrip().splitlines()[-1] == ERROR_RULE


def test_cheat_metrics_prints_the_section_then_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_listing_project(tmp_path)

    assert run_cli(monkeypatch, project, "cheat", "--metrics") == 0

    out = _squash(capsys.readouterr().out).replace("`", "")
    positions = [out.index(HEADING)]
    for rule in MEASURING_RULES:
        positions.append(out.index(_squash(rule).replace("`", "")))
    positions.append(out.index("[sim_core.sim]"))
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


def test_the_measure_tool_description_states_every_measuring_rule() -> None:
    """The MCP tool, the cheatsheet and the exit 1 and exit 2 output carry one text (#4134)."""
    description = measure_tool_def()["description"]

    _, marker, listed = description.partition("Rules:\n")
    assert marker, "the description has no 'Rules:' list"
    bullets = [_squash(line[2:]) for line in listed.splitlines() if line.startswith("- ")]
    assert bullets == [_squash(rule) for rule in MEASURING_RULES]
