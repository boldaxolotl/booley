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


def test_top_level_subcommand_metavar_matches_command_locations():
    """`booley --help` usage line must include `specialist` and every public command,
    built from COMMAND_LOCATIONS, and omit unadvertised commands (e.g. `shell`)."""
    parser = tlr._build_parser()
    sub = _get_subparsers_action(parser)
    metavar_cmds = _metavar_tokens(sub)

    # 1. Metavar equals COMMAND_LOCATIONS exactly (including specialist)
    assert metavar_cmds == set(tlr.COMMAND_LOCATIONS.keys())
    assert "specialist" in metavar_cmds
    assert "shell" not in metavar_cmds

    # 2. Registered public subparsers equal metavar and COMMAND_LOCATIONS
    registered_public = {
        action.dest
        for action in sub._choices_actions
        if action.help is not None and action.help != argparse.SUPPRESS
    }
    assert metavar_cmds == registered_public

    # 3. Usage line in booley --help names specialist and every public command
    usage = parser.format_usage()
    assert "specialist" in usage
    assert "shell" not in usage
    for cmd in tlr.COMMAND_LOCATIONS:
        assert cmd in usage


@pytest.mark.parametrize(
    ("path", "expected_choices"),
    [
        (
            ("board",),
            {
                "show",
                "review",
                "approve",
                "validate",
                "check-ready",
                "create",
                "move",
                "reset",
                "archive",
            },
        ),
        (
            ("session",),
            {"up", "enter", "down", "status", "validate", "refresh"},
        ),
        (
            ("upgrade",),
            {"status", "acknowledge"},
        ),
        (
            ("projects",),
            {"discover", "forget"},
        ),
        (
            ("eda", "grant"),
            {"add", "revoke"},
        ),
    ],
)
def test_audited_subcommand_metavars_match_public_subparsers(path, expected_choices):
    """Sub-group subparsers with custom metavars must match their registered public choices."""
    parser = tlr._build_parser()
    for segment in path:
        sub = _get_subparsers_action(parser)
        parser = sub.choices[segment]

    target_sub = _get_subparsers_action(parser)
    metavar_tokens = _metavar_tokens(target_sub)
    assert metavar_tokens == expected_choices

    # Where subparsers have advertised help, ensure metavar matches them
    advertised = {
        action.dest
        for action in target_sub._choices_actions
        if action.help is not None and action.help != argparse.SUPPRESS
    }
    if advertised:
        assert metavar_tokens == advertised
