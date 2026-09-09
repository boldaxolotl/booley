"""Ticket-owned command composition for built-in deterministic Flows."""

from __future__ import annotations

import sys

from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow

from .flow_execution import TicketBoardFlowExecution

_FLOW_TYPES = {
    "fpga": FpgaImplFlow,
    "lint": LintFlow,
    "sim": SimulateFlow,
    "synth": AsicSynthesizeFlow,
}


def main(argv: list[str] | None = None) -> int:
    """Run one named built-in Flow with mandatory Ticket composition."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in _FLOW_TYPES:
        choices = ", ".join(sorted(_FLOW_TYPES))
        print(f"ERROR: ticket Flow runner requires one of: {choices}", file=sys.stderr)
        return 2
    flow_name = args.pop(0)
    return _FLOW_TYPES[flow_name]().main(args, adapter=TicketBoardFlowExecution())


if __name__ == "__main__":
    raise SystemExit(main())
