"""CLI argument adapter for the fpga Flow."""

from __future__ import annotations

import argparse
import logging

from booley.flows.cli_arguments import BuiltinArguments

logger = logging.getLogger(__name__)


class FpgaArguments(BuiltinArguments):
    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--baseline", default=None, help="Baseline git ref for comparison")
        parser.add_argument(
            "--no-cache",
            action="store_true",
            help="Bypass reusable implementation results and run the recipe again",
        )
