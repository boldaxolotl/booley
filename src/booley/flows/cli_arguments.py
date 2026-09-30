"""Shared built-in CLI options."""

import argparse

from booley.core.boundary import parse_positive_int_arg


class BuiltinArguments:
    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        """Concrete adapters add their own options."""

    @staticmethod
    def add_common_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Resolve and validate the requested work without executing EDA tools",
        )
        parser.add_argument(
            "--timeout-ms",
            type=parse_positive_int_arg,
            default=None,
            help=(
                "Active-time budget in milliseconds for each Flow work unit. "
                "Overrides [flows.<name>].timeout_ms."
            ),
        )

    @staticmethod
    def normalize(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
        """Hook for concrete adapters with compatibility normalization."""
