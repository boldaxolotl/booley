"""Top-level help advertises human commands in execution-location order."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from booley.harness import booley as cli
from booley.harness import colors
from booley.harness.terminal_status import section_header

PUBLIC_COMMANDS: list[str] = [
    "chat",
    "dashboard",
    "flow",
    "specialist",
    "worktree",
    "cheat",
    "doctor",
    "help",
    "resources",
    "targets",
    "auth",
    "bootstrap",
    "eda",
    "init",
    "projects",
]


GROUPS = {
    "Sandbox": PUBLIC_COMMANDS[:5],
    "Host or Sandbox": PUBLIC_COMMANDS[5:10],
    "Host": PUBLIC_COMMANDS[10:],
}


@pytest.fixture
def plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain output at a fixed width: no palette, no argparse color, COLUMNS=100."""
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("PYTHON_COLORS", raising=False)
    monkeypatch.setenv("COLUMNS", "100")


def _grouped_rows(help_text: str) -> tuple[dict[str, list[str]], set[int]]:
    """Command names under each column-0 header, and the column descriptions start at."""
    groups: dict[str, list[str]] = {}
    columns: set[int] = set()
    header = None
    for line in help_text.splitlines():
        if line in GROUPS:
            header = line
            groups[header] = []
        elif not line:
            header = None
        elif header is not None and (row := re.match(r"  (\w+)( +)\S", line)):
            groups[header].append(row[1])
            columns.add(len(row[0]) - 1)
    return groups, columns


def _run_cli(monkeypatch: pytest.MonkeyPatch, capsys, *argv: str) -> str:
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr("sys.argv", ["booley", *argv])
    with pytest.raises(SystemExit) as exc:
        cli._parse_cli()
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_help_groups_public_commands_by_location(plain) -> None:
    help_text = cli._build_parser().format_help()
    groups, columns = _grouped_rows(help_text)
    assert groups == GROUPS
    assert len(columns) == 1, "descriptions start at one shared column"
    assert "{" not in help_text and "positional arguments" not in help_text
    for hidden in ("feedback", "cleanup", "upgrade", "session", "goal", "shell"):
        assert not re.search(rf"^  {hidden}\s", help_text, re.M)


@pytest.mark.parametrize(
    "argv",
    [["--help"], ["-h"], ["help"], ["help", "-h"], []],
    ids=["--help", "-h", "help", "help -h", "bare-host"],
)
def test_root_help_is_the_same_for_every_entry_point(
    argv: list[str], plain, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    out = _run_cli(monkeypatch, capsys, *argv)
    help_text = cli._build_parser().format_help()
    assert out.startswith(help_text)
    assert _grouped_rows(out)[0] == GROUPS


@pytest.mark.parametrize("tty", [True, False], ids=["tty", "pipe"])
def test_root_help_headers_use_the_accent_only_on_a_tty(
    tty: bool, plain, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(colors, "COLORS_ENABLED", tty)
    lines = cli._build_parser().format_help().splitlines()
    for title in GROUPS:
        assert section_header(title) in lines
    assert any("\x1b[" in line for line in lines) is tty


@pytest.mark.parametrize("columns", [80, 200])
def test_root_help_fits_the_width_without_cutting(
    columns: int, plain, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("COLUMNS", str(columns))
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for argv in (["--help"], []):
        out = _run_cli(monkeypatch, capsys, *argv)
        assert max(map(len, out.splitlines())) <= min(columns, 100)
        flat = " ".join(out.split())
        assert "…" not in flat
        for action in sub._choices_actions:
            assert " ".join(action.help.split()) in flat


def test_cli_reference_has_the_same_public_inventory() -> None:
    reference = Path(__file__).parents[2] / "docs/user/CLI-REFERENCE.md"
    rows = re.findall(
        r"^\| (Sandbox|Either|Host-only) \| ([^|]+) \|$",
        reference.read_text(),
        re.M,
    )
    assert [(location, re.findall(r"`(\w+)`", commands)) for location, commands in rows] == [
        ("Sandbox", PUBLIC_COMMANDS[:5]),
        ("Either", PUBLIC_COMMANDS[5:10]),
        ("Host-only", PUBLIC_COMMANDS[10:]),
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
        ("resources", "either"),
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
