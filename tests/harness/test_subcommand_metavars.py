"""Subcommand metavar and public registry contract tests (Issue #1106)."""

from __future__ import annotations

import argparse
import re

import pytest

from booley.harness import booley as tlr


def _get_subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError(f"No subparsers action found on {parser}")


def _metavar_tokens(action: argparse._SubParsersAction) -> set[str]:
    assert action.metavar is not None, f"Subparser {action} missing metavar"
    return set(re.findall(r"[\w-]+", str(action.metavar)))


def _public_commands(action: argparse._SubParsersAction, *, hidden: set[str]) -> set[str]:
    """Commands without help remain public unless deliberately hidden."""
    suppressed = {
        choice.dest for choice in action._choices_actions if choice.help == argparse.SUPPRESS
    }
    return set(action.choices) - hidden - suppressed


@pytest.fixture
def parser() -> argparse.ArgumentParser:
    return tlr._build_parser()


def test_top_level_subcommand_metavar_matches_command_locations(
    parser: argparse.ArgumentParser,
) -> None:
    sub = _get_subparsers_action(parser)
    assert _metavar_tokens(sub) == set(tlr.COMMAND_LOCATIONS)


def test_top_level_subcommand_metavar_matches_public_subparsers(
    parser: argparse.ArgumentParser,
) -> None:
    sub = _get_subparsers_action(parser)
    assert _metavar_tokens(sub) == _public_commands(sub, hidden={"shell"})


def test_top_level_usage_includes_every_public_command(parser: argparse.ArgumentParser) -> None:
    usage_commands = _metavar_tokens(_get_subparsers_action(parser))
    usage = parser.format_usage()
    assert "specialist" in usage_commands
    assert "shell" not in usage_commands
    assert "shell" not in usage
    for command in tlr.COMMAND_LOCATIONS:
        assert command in usage


@pytest.mark.parametrize(
    ("path", "hidden"),
    [
        (("session",), {"prepare"}),  # Hidden lifecycle compatibility command.
        (("upgrade",), set()),
        (("projects",), set()),
        (("eda", "grant"), {"list"}),  # Deprecated alias for `booley projects`.
    ],
)
def test_audited_subcommand_metavars_match_public_subparsers(
    parser: argparse.ArgumentParser, path: tuple[str, ...], hidden: set[str]
) -> None:
    """Each audited subgroup must advertise all its registered public commands."""
    for segment in path:
        sub = _get_subparsers_action(parser)
        parser = sub.choices[segment]

    target_sub = _get_subparsers_action(parser)
    assert _metavar_tokens(target_sub) == _public_commands(target_sub, hidden=hidden)
