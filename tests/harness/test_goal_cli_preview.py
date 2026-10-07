"""`booley goal` exists only behind the Goal Mode preview switch (ADR 0067 D13)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.goals.preview import GOAL_MODE_PREVIEW_ENV
from booley.harness import booley as tlr
from booley.runtime import runtime_context


def _top_level_subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )


@pytest.fixture
def preview_off(monkeypatch) -> None:
    monkeypatch.delenv(GOAL_MODE_PREVIEW_ENV, raising=False)


@pytest.fixture
def preview_on(monkeypatch) -> None:
    monkeypatch.setenv(GOAL_MODE_PREVIEW_ENV, "1")


@pytest.mark.usefixtures("preview_off")
def test_goal_is_absent_from_the_released_surface(capsys) -> None:
    parser = tlr._build_parser()

    assert "goal" not in _top_level_subparsers(parser).choices
    assert "goal" not in parser.format_help()
    assert "goal" not in parser.format_usage()
    assert "goal" not in tlr.command_locations()
    assert "goal" not in tlr.command_project_bindings()
    assert tlr.command_locations() == tlr.COMMAND_LOCATIONS
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["goal", "status"])
    assert exc.value.code == 2
    assert "invalid choice: 'goal'" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_the_exact_switch_value_exposes_goal(monkeypatch, value: str) -> None:
    monkeypatch.setenv(GOAL_MODE_PREVIEW_ENV, value)

    assert "goal" not in _top_level_subparsers(tlr._build_parser()).choices


def test_released_cheatsheet_never_mentions_goal() -> None:
    from booley.runtime.paths import cheatsheet_path

    assert "booley goal" not in cheatsheet_path().read_text(encoding="utf-8")


@pytest.mark.usefixtures("preview_on")
@pytest.mark.parametrize(
    "argv",
    [
        ["goal", "abandon"],
    ],
)
def test_goal_subcommands_parse_and_report_not_available(argv: list[str], capsys) -> None:
    parser = tlr._build_parser()
    args = parser.parse_args(argv)

    assert args.command == "goal"
    assert tlr._EARLY_COMMANDS["goal"](args, Path.cwd()) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"booley goal {argv[1]}: not available yet" in captured.err


@pytest.mark.usefixtures("preview_on")
def test_goal_status_detail_flags_are_mutually_exclusive(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        tlr._build_parser().parse_args(["goal", "status", "--short", "--long"])

    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.usefixtures("preview_on")
def test_goal_is_sandbox_only_and_listed_consistently(monkeypatch, capsys) -> None:
    parser = tlr._build_parser()
    sub = _top_level_subparsers(parser)

    assert tlr.command_locations()["goal"] is tlr.CommandLocation.SESSION_RUNTIME
    assert tlr.command_project_bindings()["goal"] is tlr.ProjectBinding.REQUIRED
    assert "goal" in str(sub.metavar)
    assert set(sub.choices) == set(tlr.command_project_bindings())
    assert "[Sandbox] Inspect or abandon" in parser.format_help()
    # The released catalogs stay untouched by the switch.
    assert "goal" not in tlr.COMMAND_LOCATIONS
    assert "goal" not in tlr.COMMAND_PROJECT_BINDINGS

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    with pytest.raises(SystemExit) as exc:
        tlr._enforce_runtime_location("goal")
    assert exc.value.code == 2
    assert "runs inside the Booley Sandbox" in capsys.readouterr().err

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    tlr._enforce_runtime_location("goal")  # inside the Sandbox: must not raise
