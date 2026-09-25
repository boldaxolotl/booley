"""Shared built-in CLI options and legacy alias normalization."""

import argparse
import logging

from booley.core.boundary import parse_positive_int_arg

logger = logging.getLogger(__name__)


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
        parser.add_argument(
            "--timeout",
            dest="_legacy_timeout_ms",
            type=parse_positive_int_arg,
            default=None,
            help=argparse.SUPPRESS,
        )

    @staticmethod
    def normalize(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
        """Parse and normalize the built-in-only compatibility aliases."""
        legacy = args._legacy_timeout_ms
        if args.timeout_ms is not None and legacy is not None:
            parser.error("--timeout-ms cannot be combined with deprecated --timeout")
        if legacy is not None:
            logger.warning("--timeout is deprecated; use --timeout-ms")
            args.timeout_ms = legacy
        del args._legacy_timeout_ms
