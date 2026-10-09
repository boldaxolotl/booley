"""Goal Mode is the public workflow surface; retired commands give one pointer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from booley.harness import booley as cli


def test_goal_surface_matches_catalogs():
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == set(cli.COMMAND_PROJECT_BINDINGS)
    assert {"goal", "dashboard"} <= cli._CONTAINER_ONLY_COMMANDS
    assert not {"run", "board"} & set(sub.choices)
    assert "goal" in str(sub.metavar)
    assert "[Sandbox] Inspect or abandon" in parser.format_help()


def test_goal_status_flags_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args(["goal", "status", "--short", "--long"])
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_goal_abandon_refuses_without_occupying_record(capsys):
    args = cli._build_parser().parse_args(["goal", "abandon"])
    assert cli._EARLY_COMMANDS["goal"](args, Path.cwd()) == 2
    assert "Goal" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "board"])
@pytest.mark.parametrize("inside", [False, True])
@pytest.mark.parametrize("prefix", [[], ["--project", "/absent"], ["--project=/absent"]])
@pytest.mark.parametrize("tail", [[], ["--help"], ["-n", "2"], ["--ticket", "a"], ["show", "a"]])
def test_retired_command_pointer_precedes_all_runtime_work(
    command, inside, prefix, tail, monkeypatch, capsys
):
    def unexpected(*_args, **_kwargs):
        pytest.fail("retired command touched Project/runtime state")

    monkeypatch.setattr(sys, "argv", ["booley", *prefix, command, *tail])
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)
    monkeypatch.setattr(cli, "_command_project_binding", unexpected)
    monkeypatch.setattr(cli, "_selected_project_root", unexpected)
    monkeypatch.setattr(cli, "_enforce_runtime_location", unexpected)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", unexpected)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == cli.RETIRED_COMMAND_POINTERS[command] + "\n"


@pytest.mark.parametrize("flag", ["--slug", "-s", "--board", "-b"])
def test_removed_shortcuts_are_rejected(flag):
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args([flag, "example"])
    assert exc.value.code == 2


def test_project_value_is_not_treated_as_a_retired_command(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["booley", "--project", "board", "goal", "status"])
    assert cli._parse_cli().command == "goal"
