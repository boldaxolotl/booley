"""Compose built-in and Project Flows with immutable Goal admission facts."""

from __future__ import annotations

import sys
from pathlib import Path

from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.goals.binding import GoalBindingError
from booley.goals.flow_execution import GoalFlowExecution
from booley.mcp.call_context import binding_from_environment
from booley.ticket_board.flow_runner import TicketFlowLoadError, load_project_flow

_FLOW_TYPES = {
    "fpga": FpgaImplFlow,
    "lint": LintFlow,
    "sim": SimulateFlow,
    "synth": AsicSynthesizeFlow,
}


def main(argv: list[str] | None = None) -> int:
    """Execute a Flow with the recorder and persistence chosen at admission."""
    args = list(sys.argv[1:] if argv is None else argv)
    custom_path = None
    if args[:1] == ["--custom-path"] and len(args) >= 3:
        custom_path = Path(args[1])
        del args[:2]
    try:
        binding = binding_from_environment()
        if binding is None:
            raise GoalBindingError("Goal Flow runner requires an admission binding")
        if not args or (custom_path is None and args[0] not in _FLOW_TYPES):
            raise GoalBindingError("Goal Flow runner requires a supported Flow name")
        name = args.pop(0)
        adapter = GoalFlowExecution(binding)
        if custom_path is None:
            return _FLOW_TYPES[name]().main(args, adapter=adapter)
        flow = load_project_flow(custom_path, name)
        flow.configure_flow_execution(adapter)
        return flow.main(args)
    except (GoalBindingError, TicketFlowLoadError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
