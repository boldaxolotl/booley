"""Shared built-in CLI options."""

import argparse
from pathlib import Path

from booley.core.boundary import parse_positive_int_arg
from booley.flows.cli_help import HelpGroup
from booley.flows.cli_selection import EndpointProjectAction, TimeoutAction, parse_duration

_TIMEOUT_HELP = (
    "Active-time budget in milliseconds for each Flow work unit. "
    "Overrides [flows.<name>].timeout_ms."
)


def add_project_args(parser: argparse.ArgumentParser, *, human: bool = False) -> None:
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path.cwd(),
        action=EndpointProjectAction if human else "store",
        help=argparse.SUPPRESS if human else "Working directory (worktree root)",
    )


def add_canonical_project_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-C",
        "--project",
        dest="work_dir",
        action=EndpointProjectAction,
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="Select the Project Root",
    )


def add_timeout_args(parser: argparse.ArgumentParser, *, human: bool = False) -> None:
    parser.add_argument(
        "--timeout-ms",
        type=parse_positive_int_arg,
        default=None,
        action=TimeoutAction if human else "store",
        help=argparse.SUPPRESS if human else _TIMEOUT_HELP,
    )


def add_human_options(parser: argparse.ArgumentParser) -> None:
    """Append canonical controls after the adapter's options, preserving help order."""
    add_canonical_project_args(parser)
    scope = _TIMEOUT_HELP.replace(" in positive milliseconds", "").replace(" in milliseconds", "")
    parser.add_argument(
        "--timeout",
        dest="timeout_ms",
        type=parse_duration,
        action=TimeoutAction,
        default=argparse.SUPPRESS,
        metavar="DURATION",
        help=f"{scope.rstrip('.')}. Use seconds, 30m, or 1h30m.",
    )


class BuiltinArguments:
    help_groups: tuple[HelpGroup, ...] = ()

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        """Concrete adapters add their own options."""

    @staticmethod
    def add_common_args(parser: argparse.ArgumentParser, *, human: bool = False) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Resolve and validate the requested work without executing EDA tools",
        )
        add_timeout_args(parser, human=human)

    @staticmethod
    def normalize(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
        """Hook for concrete adapters with compatibility normalization."""
