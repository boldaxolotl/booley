"""Check executable documentation examples against the registered CLI trees."""

from __future__ import annotations

import argparse
import re
import shlex
from functools import cache
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.flows.sim import campaign_retention
from booley.harness.booley import _build_parser
from booley.mcp.registry import discover_mcp_tools
from booley.ticket_board.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
COMMAND = re.compile(r"(?<![\w./-])(?:python -m booley\.[\w.]+|booley)(?=\s|$)")
# No hidden commands are approved for current user/packaged documentation.
HIDDEN_COMMAND_ALLOWLIST: set[tuple[str, ...]] = set()
# Changelog entries describe retired interfaces rather than current instructions.
HISTORICAL_COMMANDS = {
    ("src/booley/data/refs/CHANGELOG.md", "booley flow elab"),
    ("src/booley/data/refs/CHANGELOG.md", "booley board prepare-review"),
}


@cache
def _public_parser() -> argparse.ArgumentParser:
    return _build_parser()


@cache
def _ticket_parser() -> argparse.ArgumentParser:
    return build_parser()


@cache
def _flow_names() -> set[str]:
    return {tool.name for tool in discover_mcp_tools() if tool.kind == "flow"}


def _commands(text: str) -> list[tuple[int, list[str]]]:
    """Extract fenced shell examples, inline code, and shell substitutions."""
    examples = []
    for number, line in enumerate(text.splitlines(), 1):
        snippets = re.findall(r"`([^`]+)`", line)
        if not snippets and COMMAND.match(line.lstrip()):
            snippets = [line.strip()]
        snippets.extend(re.findall(r"\$\((python -m booley\.[^)]*)\)", line))
        for snippet in snippets:
            if not COMMAND.match(snippet):
                continue
            # Continuation arguments do not affect the command path. Shell
            # placeholders remain literal; examples are never executed.
            command = snippet.rstrip().removesuffix("\\").rstrip()
            examples.append((number, shlex.split(command, comments=True)))
    return examples


def _subcommand_error(parser: argparse.ArgumentParser, args: list[str]) -> str | None:
    path = []
    while args:
        sub = next(
            (
                action
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            None,
        )
        if sub is None:
            return None
        token = args.pop(0)
        if token.startswith("-"):
            option = parser._option_string_actions.get(token.split("=", 1)[0])
            if option is not None and option.nargs != 0 and "=" not in token and args:
                args.pop(0)
            continue
        if token.startswith(("<", "$")) or token in {"...", "…"} or "|" in token:
            return None  # Explicit generic command template, not a concrete invocation.
        path.append(token)
        if token not in sub.choices:
            return f"unknown subcommand {' '.join(path)}"
        advertised = {
            action.dest for action in sub._choices_actions if action.help != argparse.SUPPRESS
        }
        suppressed = any(
            action.dest == token and action.help == argparse.SUPPRESS
            for action in sub._choices_actions
        )
        usage_choices = set(re.findall(r"[\w-]+", str(sub.metavar)))
        hidden = suppressed or (
            sub.metavar is not None and token not in usage_choices and token not in advertised
        )
        if hidden and tuple(path) not in HIDDEN_COMMAND_ALLOWLIST:
            return f"hidden subcommand {' '.join(path)}"
        parser = sub.choices[token]
    return None


def _command_error(tokens: list[str]) -> str | None:
    if tokens[0] == "booley":
        if len(tokens) > 2 and tokens[1] == "flow":
            name = tokens[2]
            flows = _flow_names()
            if not name.startswith(("<", "$", "-")) and name not in flows:
                return f"unknown Flow {name}"
        return _subcommand_error(_public_parser(), tokens[1:])
    module = tokens[2]
    if module == "booley.ticket_board":
        return _subcommand_error(_ticket_parser(), tokens[3:])
    if module == "booley.flows.sim.campaign_retention":
        # Capture its real parser before parsing or pruning can happen.
        with (
            patch.object(
                argparse.ArgumentParser,
                "parse_args",
                autospec=True,
                side_effect=RuntimeError("capture"),
            ) as parse_args,
            pytest.raises(RuntimeError, match="capture"),
        ):
            campaign_retention.main()
        return _subcommand_error(parse_args.call_args.args[0], tokens[3:])
    return f"undocumented module CLI {module}"


def test_documented_command_paths_are_registered_and_public() -> None:
    failures = []
    count = 0
    for directory in (ROOT / "src/booley/data", ROOT / "docs/user"):
        for doc in sorted(directory.rglob("*")):
            if not doc.is_file():
                continue
            try:
                text = doc.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # Binary packaged assets cannot contain shell examples.
            for line, tokens in _commands(text):
                count += 1
                if (doc.relative_to(ROOT).as_posix(), " ".join(tokens)) in HISTORICAL_COMMANDS:
                    continue
                error = _command_error(tokens)
                if error:
                    failures.append(f"{doc.relative_to(ROOT)}:{line}: {error}")
    assert count > 100, "Command extraction must exercise the documentation corpus"
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (
            "booley board amend slug --changes-file changes.json --preview",
            "unknown subcommand board amend",
        ),
        ("booley shell -- echo hello", "hidden subcommand shell"),
        ("booley session prepare", "hidden subcommand session prepare"),
        ("booley board prepare-review", "hidden subcommand board prepare-review"),
        ("booley flow coverage_analyst --campaign coverage.json", "unknown Flow coverage_analyst"),
        ("python -m booley.ticket_board not-a-command", "unknown subcommand not-a-command"),
    ],
)
def test_invalid_documented_commands_fail(command: str, error: str) -> None:
    assert _command_error(shlex.split(command)) == error


def test_amendment_example_is_accepted_by_real_parser() -> None:
    args = build_parser().parse_args(
        ["amend", "slug", "--changes-file", "changes.json", "--preview"]
    )
    assert args.command == "amend"


def test_extracts_inline_fenced_and_substitution_commands() -> None:
    text = """Run `booley board amend "$SLUG" --preview` now.
  booley shell -- echo hi
CLASSIFIED=$(python -m booley.ticket_board classify)
"""
    assert [tokens[:3] for _, tokens in _commands(text)] == [
        ["booley", "board", "amend"],
        ["booley", "shell", "--"],
        ["python", "-m", "booley.ticket_board"],
    ]


@pytest.mark.parametrize(
    "command",
    [
        "booley eda grant add /project --kind vivado",
        "booley eda installation list",
        "booley board --project-root /project show slug",
        "python -m booley.ticket_board amend slug --changes-file file --apply --expected-preview digest",
    ],
)
def test_registered_documented_commands_pass(command: str) -> None:
    assert _command_error(shlex.split(command)) is None
