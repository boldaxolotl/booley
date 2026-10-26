"""Top-level help advertises human commands in execution-location order."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from booley.harness import booley as cli

PUBLIC_COMMANDS: list[str] = [
    "chat",
    "dashboard",
    "flow",
    "specialist",
    "worktree",
    "cheat",
    "doctor",
    "help",
    "targets",
    "auth",
    "bootstrap",
    "eda",
    "init",
    "projects",
]


def test_help_groups_public_commands_by_location() -> None:
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert str(sub.metavar) == "{" + ",".join(PUBLIC_COMMANDS) + "}"
    assert [action.dest for action in sub._choices_actions] == PUBLIC_COMMANDS
    rows = re.findall(r"^ +(\w+)\s+\[(Sandbox|either|host)\]", parser.format_help(), re.M)
    assert rows == [
        (command, location)
        for commands, location in [
            (PUBLIC_COMMANDS[:5], "Sandbox"),
            (PUBLIC_COMMANDS[5:9], "either"),
            (PUBLIC_COMMANDS[9:], "host"),
        ]
        for command in commands
    ]


def test_cli_reference_has_the_same_public_inventory() -> None:
    reference = Path(__file__).parents[2] / "docs/user/CLI-REFERENCE.md"
    rows = re.findall(
        r"^\| (Sandbox|Either|Host-only) \| ([^|]+) \|$",
        reference.read_text(),
        re.M,
    )
    assert [(location, re.findall(r"`(\w+)`", commands)) for location, commands in rows] == [
        ("Sandbox", PUBLIC_COMMANDS[:5]),
        ("Either", PUBLIC_COMMANDS[5:9]),
        ("Host-only", PUBLIC_COMMANDS[9:]),
    ]


@pytest.mark.parametrize("command", ["feedback", "cleanup", "upgrade", "session", "goal", "shell"])
def test_skill_commands_are_hidden_but_remain_registered(command: str) -> None:
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert command in sub.choices
    assert command not in re.findall(r"\w+", str(sub.metavar))
    assert not re.search(rf"^ +{command}\s", parser.format_help(), re.M)


@pytest.mark.parametrize(
    "command,location",
    [
        ("chat", "Sandbox"),
        ("dashboard", "Sandbox"),
        ("flow", "Sandbox"),
        ("specialist", "Sandbox"),
        ("worktree", "Sandbox"),
        ("goal", "Sandbox"),
        ("cheat", "either"),
        ("doctor", "either"),
        ("help", "either"),
        ("feedback", "either"),
        ("cleanup", "either"),
        ("upgrade", "either"),
        ("targets", "either"),
        ("auth", "host"),
        ("bootstrap", "host"),
        ("eda", "host"),
        ("init", "host"),
        ("projects", "host"),
        ("session", "host"),
    ],
)
@pytest.mark.parametrize("inside", [False, True])
def test_command_locations_enforce_the_audited_boundary(
    command: str,
    location: str,
    inside: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)
    monkeypatch.setattr("sys.argv", ["booley", command])
    assert cli.COMMAND_LOCATIONS[command].value == location
    refused = (location == "Sandbox" and not inside) or (location == "host" and inside)
    if refused:
        with pytest.raises(SystemExit) as exc:
            cli._enforce_runtime_location(command)
        assert exc.value.code == 2
        assert f"booley {command}" in capsys.readouterr().err
    else:
        cli._enforce_runtime_location(command)
        assert capsys.readouterr().err == ""
