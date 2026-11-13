"""CLI argument adapter for the Mutation Flow."""

from __future__ import annotations

import argparse
from typing import Literal

from booley.core.boundary import parse_positive_int_arg
from booley.flows.cli_arguments import BuiltinArguments
from booley.flows.cli_help import HelpGroup, HelpOption


def _mutant_count(value: str) -> int | Literal["auto"]:
    """argparse type of ``--count``: ``auto`` in any case, or a positive integer."""
    if value.strip().lower() == "auto":
        return "auto"
    try:
        return parse_positive_int_arg(value)
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError("must be a positive integer or 'auto'") from None


class MutationArguments(BuiltinArguments):
    help_groups = (
        HelpGroup(
            "Mutation options",
            (
                HelpOption(
                    "scope",
                    "When omitted, mutate the Target's own RTL.",
                    "Comma-separated RTL files eligible for mutation, excluding "
                    "testbench, vendored and generated files by default",
                ),
                HelpOption(
                    "count",
                    "When omitted, use the criterion's count, else 10.",
                    "Number of Mutants: a positive integer or 'auto'",
                ),
                HelpOption(
                    "min_detected",
                    "When omitted, use the criterion's threshold, else ceil(0.8 x --count).",
                    "Pass threshold: minimum Mutants that must be detected (some "
                    "survivors may be equivalent Mutants). Raise it to demand more, "
                    "lower it to accept less",
                ),
                HelpOption(
                    "seed",
                    "Default: 0.",
                    "Non-negative integer that selects the Mutants. The same RTL, "
                    "--count and --seed give the same Mutants",
                ),
            ),
        ),
    )

    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--scope",
            default=None,
            help=(
                "Comma-separated RTL files eligible for mutation "
                "(default: the Target's own RTL, without testbench, vendored "
                "and generated files)"
            ),
        )
        parser.add_argument(
            "--count",
            type=_mutant_count,
            default=None,
            help="Number of Mutants: a positive integer or 'auto' (default: 10)",
        )
        parser.add_argument(
            "--min-detected",
            type=int,
            default=None,
            help=(
                "Pass threshold: minimum Mutants that must be detected "
                "(default: ceil(0.8 x --count); some survivors may be equivalent "
                "Mutants). Raise it to demand more, lower it to accept less."
            ),
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=0,
            help=(
                "Non-negative integer that selects the Mutants (default: 0). "
                "The same RTL, --count and --seed give the same Mutants."
            ),
        )
