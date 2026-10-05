"""CLI argument adapter for the lint Flow."""

from __future__ import annotations

import argparse
import logging

from booley.flows.cli_arguments import BuiltinArguments
from booley.flows.cli_help import HelpGroup, HelpOption

logger = logging.getLogger(__name__)


class LintArguments(BuiltinArguments):
    help_groups = (
        HelpGroup(
            "Lint options", (HelpOption("scope", "When omitted, include all resolved files."),)
        ),
    )

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--scope",
            default="",
            help="Comma-separated file paths to filter warnings",
        )
