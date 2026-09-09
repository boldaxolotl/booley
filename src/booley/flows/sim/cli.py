"""CLI argument adapter for the sim Flow."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from booley.flows.cli_arguments import BuiltinArguments

from .mode import SimulationMode, parse_simulation_mode

logger = logging.getLogger(__name__)


class SimArguments(BuiltinArguments):
    @staticmethod
    def add_args(parser: Any) -> None:
        # tb_top left the surface (ADR 0021): a sim Target's `toplevel` IS its
        # TB top, so it comes from the resolved Target (tb_top_for_target), not
        # a per-call arg.
        parser.add_argument(
            "--coverage",
            "--cov",
            action="store_true",
            help="Collect a native Verilator Coverage Campaign for each selected Target",
        )
        SimArguments._add_elaboration_args(parser)
        parser.add_argument(
            "--test",
            default=None,
            help="Run specific test by name (substring match)",
        )
        parser.add_argument(
            "--skip",
            default=None,
            help="Comma-separated test names to exclude (exact match). Adds to "
            "any [flows.sim] / tests.toml 'skip' list. Use to dodge "
            "known-hanging tests that burn the full wall-clock budget.",
        )
        SimArguments._add_run_control_args(parser)

    @staticmethod
    def _add_elaboration_args(parser: Any) -> None:
        """Add the canonical mode plus CLI-only compatibility aliases."""
        parser.add_argument(
            "--mode",
            type=parse_simulation_mode,
            choices=list(SimulationMode),
            default=None,
            metavar="{simulate,elab-only,elab-only-standalone}",
            help="Execution mode: run tests, elaborate only, or elaborate then "
            "perform the standalone module sweep",
        )
        parser.add_argument(
            "--elab-only",
            "--build-only",
            dest="_legacy_elab_only",
            action="store_true",
            help=argparse.SUPPRESS,
        )
        parser.add_argument(
            "--standalone",
            dest="_legacy_standalone",
            action="store_true",
            help=argparse.SUPPRESS,
        )

    @staticmethod
    def normalize(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
        """Normalize legacy mode flags into the single canonical selector."""
        BuiltinArguments.normalize(args, parser)
        legacy_requested = args._legacy_elab_only or args._legacy_standalone
        if args.mode is not None and legacy_requested:
            parser.error("--mode cannot be combined with legacy mode flags")
        args._legacy_standalone_without_elab = (
            args._legacy_standalone and not args._legacy_elab_only
        )
        if legacy_requested:
            logger.warning(
                "--elab-only, --build-only, and --standalone are deprecated; use --mode"
            )
        if args._legacy_elab_only and args._legacy_standalone:
            args.mode = SimulationMode.ELAB_ONLY_STANDALONE
        elif args._legacy_elab_only:
            args.mode = SimulationMode.ELAB_ONLY
        elif args.mode is None:
            args.mode = SimulationMode.SIMULATE
        vars(args).pop("_legacy_elab_only")
        vars(args).pop("_legacy_standalone")

    @staticmethod
    def _add_run_control_args(parser: Any) -> None:
        """Add run-stage tracing, reporting, cleanup, and timeout controls."""
        parser.add_argument(
            "--trace",
            action="store_true",
            help="Enable waveform trace (debugging only — do not use for pass/fail checks)",
        )
        parser.add_argument(
            "--result-verbosity",
            choices=["compact", "full"],
            default="compact",
            help="Cocotb result detail on stdout; full XML/JSON artifacts are always retained",
        )
        # --trace-scope left the surface (ADR 0022, 2026-06-23): the --trace
        # overlay .core traces the full hierarchy at a fixed depth, so there is no
        # per-call scope knob. Scoping, when a specialist needs it, lives on the
        # specialist's own surface, not the built-in simulate one.
        parser.add_argument(
            "--no-kill",
            action="store_true",
            help="Skip zombie process cleanup",
        )
