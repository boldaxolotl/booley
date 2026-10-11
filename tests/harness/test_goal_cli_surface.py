"""Goal Mode is the public workflow surface; retired commands give one pointer."""

from __future__ import annotations

import argparse
import sys

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


def test_goal_abandon_refuses_without_occupying_record(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    args = cli._build_parser().parse_args(["goal", "abandon"])
    assert cli._EARLY_COMMANDS["goal"](args, tmp_path) == 2
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


@pytest.mark.parametrize(
    "nargs,values", [("?", []), ("?", ["value"]), ("*", []), ("*", ["a", "b"]), ("+", ["a", "b"])]
)
@pytest.mark.parametrize("command", ["run", "board"])
def test_retired_command_after_variable_global_values(nargs, values, command, monkeypatch, capsys):
    globals_parser = cli._global_options_parser()
    globals_parser.add_argument("--extra", nargs=nargs)
    monkeypatch.setattr(cli, "_global_options_parser", lambda: globals_parser)
    monkeypatch.setattr(sys, "argv", ["booley", "--extra", *values, command, "--help"])
    with pytest.raises(SystemExit) as caught:
        cli._parse_cli()
    assert caught.value.code == 2
    assert capsys.readouterr().err == cli.RETIRED_COMMAND_POINTERS[command] + "\n"


@pytest.mark.parametrize("prefix", [["-C/x"], ["--proj", "/absent"], ["--"]])
@pytest.mark.parametrize("command", ["run", "board"])
def test_retired_command_after_attached_or_zero_arity_options(
    prefix, command, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["booley", *prefix, command, "--bogus"])
    with pytest.raises(SystemExit) as caught:
        cli._parse_cli()
    assert caught.value.code == 2
    assert capsys.readouterr().err == cli.RETIRED_COMMAND_POINTERS[command] + "\n"


@pytest.mark.parametrize("nargs", [1, "+"])
def test_required_global_value_is_not_a_command(nargs):
    parser = cli._global_options_parser()
    parser.add_argument("--extra", nargs=nargs)
    assert cli._argv_command(["--extra", "board"], parser) is None
    assert cli._argv_command(["--extra", "board", "goal", "status"], parser) == "goal"


@pytest.mark.parametrize("flag", ["--version", "-h", "--help"])
@pytest.mark.parametrize("command", ["run", "board"])
def test_global_exit_option_before_retired_command_wins(flag, command, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["booley", flag, command])
    with pytest.raises(SystemExit) as caught:
        cli._parse_cli()
    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "booley" in captured.out
    assert "/booley-goal" not in captured.out


def test_preparser_global_options_match_root_parser():
    root = cli._build_parser()
    globals_parser = cli._global_options_parser()

    def options(parser):
        return {
            option: action.nargs for action in parser._actions for option in action.option_strings
        }

    assert options(globals_parser) == options(root)
    assert globals_parser.allow_abbrev == root.allow_abbrev


def test_nonexistent_short_project_option_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["booley", "-p/x", "run"])
    with pytest.raises(SystemExit) as caught:
        cli._parse_cli()
    assert caught.value.code == 2
    assert "/booley-goal" not in capsys.readouterr().err


@pytest.mark.parametrize("command", [["goal", "--help"], ["goal", "status", "--help"]])
def test_goal_help_describes_both_selection_cases(command, capsys):
    with pytest.raises(SystemExit) as caught:
        cli._build_parser().parse_args(command)
    assert caught.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "this worktree" in text
    assert "every active Goal Mode in the Project" in text
    assert "when this worktree hosts none" in text


def test_usage_status_describes_both_selection_cases():
    from pathlib import Path

    usage = Path(__file__).parents[2] / "docs/user/USAGE.md"
    line = next(
        line for line in usage.read_text().splitlines() if line.startswith("booley goal status ")
    )
    assert "this worktree" in line
    assert "every active Goal Mode in the Project" in line
    assert "when this worktree hosts none" in line


def test_goal_group_description_and_catalog_describe_selection(capsys):
    root = cli._build_parser()
    commands = next(
        action
        for action in root._actions
        if hasattr(action, "choices") and action.choices and "goal" in action.choices
    )
    goal = commands.choices["goal"]
    assert "every active Goal Mode in the Project" in goal.description
    assert "when this worktree hosts none" in goal.description
    with pytest.raises(SystemExit) as caught:
        root.parse_args(["--help"])
    assert caught.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert (
        "status shows every active Goal Mode in the Project when this worktree hosts none" in text
    )
