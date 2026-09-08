"""CLI argument adapter for the fpga Flow."""

from __future__ import annotations

import argparse
import logging

from booley.flows.cli_arguments import BuiltinArguments
from booley.flows.implementation_profiles import PPA_PROFILE_CHOICES

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
        parser.add_argument(
            "--ppa-profile",
            choices=PPA_PROFILE_CHOICES,
            default=None,
            help="Override the Target's portable FPGA optimization profile for this call",
        )
