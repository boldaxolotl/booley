"""CLI argument adapter for the synth Flow."""

from __future__ import annotations

import argparse
import logging

from booley.flows.cli_arguments import BuiltinArguments
from booley.flows.synth.backends.configure import FRONTEND_CHOICES
from booley.flows.synth.ppa_config import add_ppa_arguments

logger = logging.getLogger(__name__)


class SynthArguments(BuiltinArguments):
    @staticmethod
    def add_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--baseline",
            default=None,
            help="Baseline git ref (SHA/branch/tag) for comparison",
        )
        # ADR 0029 decision 7: flatten is an A/B experiment toggle, so it lives
        # on the Flow CLI. Tri-state: unset (None) means "use the selected
        # Target's flow_options.flatten"; the explicit flag
        # wins over it. --no-flatten shares the dest so the two are exclusive.
        parser.add_argument(
            "--flatten",
            dest="flatten",
            action="store_true",
            default=None,
            help="Flatten the hierarchy before tech-mapping (overrides the "
            "selected Target's flow_options.flatten).",
        )
        parser.add_argument(
            "--no-flatten",
            dest="flatten",
            action="store_false",
            default=None,
            help="Preserve hierarchy through synthesis (overrides the selected Target).",
        )
        # RTL frontend A/B toggle (like --flatten): sv2v transpile vs Yosys
        # 0.67 native read_slang. Tri-state — unset (None) uses the selected
        # Target's flow_options.frontend (else sv2v).
        parser.add_argument(
            "--frontend",
            choices=list(FRONTEND_CHOICES),
            default=None,
            help="RTL frontend: 'sv2v' (transpile + read_verilog) or 'slang' "
            "(native Yosys read_slang, requires the Yosys>=0.67 sandbox image). "
            "Overrides the selected Target's flow_options.frontend (default sv2v).",
        )
        add_ppa_arguments(parser)
