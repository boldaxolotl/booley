"""CLI argument adapter for the sim Flow."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from booley.flows.cli_arguments import BuiltinArguments
from booley.flows.cli_help import HelpGroup, HelpOption, SurfaceHelp
from booley.flows.test_runs import (
    MAX_SEED,
    SEED_WITH_SEEDED_SELECTOR,
    TestRunLimitError,
    TestRunNameError,
    UnpinnedSeed,
    parse_seed_request,
    parse_test_selector,
)

from .mode import SimulationMode, parse_simulation_mode

logger = logging.getLogger(__name__)


def parse_seed_argument(value: str) -> int | str:
    """argparse type of ``--seed``: a seed in range, or ``random``."""
    try:
        return parse_seed_request(value)
    except TestRunNameError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


class SimArguments(BuiltinArguments):
    help_groups = (
        HelpGroup(
            "Output",
            (
                HelpOption("verbose", "Default: off."),
                HelpOption("result_verbosity", "Default: compact."),
            ),
        ),
        HelpGroup(
            "Simulation options",
            (
                HelpOption(
                    "coverage",
                    "Default: off for new Simulation Campaigns; resume restores the manifest's coverage policy and rejects this flag.",
                ),
                HelpOption(
                    "no_waivers",
                    "When omitted, approved waivers apply on new coverage-collecting Simulation Campaigns; resume restores the manifest's raw-coverage/waiver policy and rejects this flag.",
                ),
                HelpOption(
                    "mode",
                    "When omitted, use simulate for new Simulation Campaigns; on resume omission is required because the manifest owns the workload.",
                ),
                HelpOption("_legacy_elab_only", ""),
                HelpOption("_legacy_standalone", ""),
                HelpOption(
                    "test",
                    "When omitted, use the configured suite and skip policy; on resume the manifest owns test selection. Cannot combine with --tests-file or --resume-from.",
                ),
                HelpOption(
                    "seed",
                    "When omitted, each Test Run uses the seed its --test names, else the Target's Default Seed. Cannot combine with a --test that names a seed, or with --resume-from.",
                ),
                HelpOption(
                    "tests_file",
                    "When omitted, use --test or the configured suite and skip policy. Cannot combine with --test or --resume-from.",
                ),
                HelpOption("resume_from", "When omitted, start a new Simulation Campaign."),
                HelpOption(
                    "trace",
                    "Default: off for new Simulation Campaigns; resume restores the manifest's trace policy and rejects this flag.",
                ),
                HelpOption("no_kill", "When omitted, zombie process cleanup is enabled."),
            ),
        ),
    )

    @staticmethod
    def _add_test_run_args(parser: Any) -> None:
        """What to run: Test Run selectors, the seed, and a selector file."""
        parser.add_argument(
            "--test",
            action="append",
            default=None,
            metavar="TEST_RUN",
            help=SurfaceHelp(
                "Select Test Runs: test, test+variant, test@seed, test+variant@seed, or "
                "test[+variant]@xN for N drawn seeds; a bare test means its default Test "
                "Variant (coverage: deterministic sorted order; plain: input order; "
                "CLI: repeat; MCP: array)",
                cli=(
                    "Run only the named Test Runs: test, test+variant, test@seed, "
                    "test+variant@seed, or test[+variant]@xN for N drawn seeds; a bare test "
                    "means its default Test Variant; repeat the flag for more. With --coverage "
                    "they run in sorted order, otherwise in the order given"
                ),
            ),
        )
        parser.add_argument(
            "--seed",
            type=parse_seed_argument,
            default=None,
            metavar="{N,random}",
            help=f"Seed for every selected Test Run (1..{MAX_SEED}), or 'random' to draw "
            "one seed per Test Run",
        )
        parser.add_argument(
            "--tests-file",
            type=Path,
            default=None,
            help="Read Test Run selectors from PATH, one per line; blanks and # comments ignored",
        )

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
        parser.add_argument(
            "--no-waivers",
            action="store_true",
            help="With --coverage, report raw coverage without applying approved waivers; "
            "with a Coverage Criterion also requires --diagnostic",
        )
        SimArguments._add_elaboration_args(parser)
        SimArguments._add_test_run_args(parser)
        parser.add_argument(
            "--resume-from",
            type=Path,
            default=None,
            metavar="MANIFEST",
            help="Resume one exact Simulation Campaign manifest.json",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Include full Simulation Campaign resume mismatch pointers and values",
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
        if args.test and args.tests_file is not None:
            parser.error("--test and --tests-file are mutually exclusive")
        if args.seed is not None and args.mode is not None and args.mode.elaborates_only:
            parser.error("--seed applies to simulation, not to an elaboration mode")
        if args.resume_from is not None:
            conflicts = SimArguments._resume_conflicts(args)
            if conflicts:
                parser.error("--resume-from cannot be combined with " + ", ".join(conflicts))
            args.target = ""
        elif args.no_waivers and not args.coverage:
            parser.error("--no-waivers requires --coverage")
        if args.tests_file is not None:
            args.test = SimArguments._read_tests_file(args.tests_file, parser)
            args.tests_file = None
        duplicates = sorted({name for name in (args.test or []) if args.test.count(name) > 1})
        if duplicates:
            parser.error("duplicate exact test name(s): " + ", ".join(duplicates))
        if args.test is not None:
            args.test = tuple(args.test)
        SimArguments._check_test_run_selection(args, parser)
        vars(args).pop("_legacy_elab_only")
        vars(args).pop("_legacy_standalone")

    @staticmethod
    def _check_test_run_selection(
        args: argparse.Namespace, parser: argparse.ArgumentParser
    ) -> None:
        """Refuse a malformed selector, and ``--seed`` beside a seeded selector."""
        try:
            selectors = [parse_test_selector(text) for text in args.test or ()]
        except (TestRunNameError, TestRunLimitError) as exc:
            parser.error(str(exc))
        if args.seed is not None and any(
            not isinstance(selector.seed_form, UnpinnedSeed) for selector in selectors
        ):
            parser.error(SEED_WITH_SEEDED_SELECTOR)

    @staticmethod
    def _resume_conflicts(args: argparse.Namespace) -> list[str]:
        return [
            option
            for present, option in (
                (bool(args.target), "--target"),
                (args.test is not None, "--test"),
                (args.tests_file is not None, "--tests-file"),
                (args.seed is not None, "--seed"),
                (args.mode is not None, "--mode"),
                (args.coverage, "--coverage"),
                (args.no_waivers, "--no-waivers"),
                (args.trace, "--trace"),
            )
            if present
        ]

    @staticmethod
    def _read_tests_file(path: Path, parser: argparse.ArgumentParser) -> list[str]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            parser.error(f"cannot read --tests-file {path}: {exc}")
        names = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not names:
            parser.error("--tests-file contains no test names")
        return names

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
