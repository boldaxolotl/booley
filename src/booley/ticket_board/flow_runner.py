"""Ticket-owned command composition for deterministic Flows."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from booley.flows.base import BooleyFlow
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


class TicketFlowLoadError(ValueError):
    """A project-local Flow cannot be loaded unambiguously."""


def _load_project_flow(path: Path, name: str) -> BooleyFlow:
    """Load one named project-local Flow without executing its script entry point."""
    module = _load_module(path)
    candidates = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, BooleyFlow)
        and value is not BooleyFlow
        and value.__module__ == module.__name__
        and value.name == name
    ]
    if len(candidates) != 1:
        raise TicketFlowLoadError(
            f"project Flow {name!r} must resolve to exactly one BooleyFlow in {path}"
        )
    return candidates[0]()


def _load_module(path: Path) -> ModuleType:
    if not path.is_file():
        raise TicketFlowLoadError(f"project Flow module does not exist: {path}")
    module_name = f"_booley_ticket_flow_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise TicketFlowLoadError(f"project Flow module cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(path.resolve().parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def main(argv: list[str] | None = None) -> int:
    """Run one named built-in or project-local Flow with Ticket composition."""
    args = list(sys.argv[1:] if argv is None else argv)
    custom_path: Path | None = None
    if args[:1] == ["--custom-path"]:
        if len(args) < 3:
            print("ERROR: --custom-path requires a path and Flow name", file=sys.stderr)
            return 2
        custom_path = Path(args[1])
        del args[:2]
    if not args:
        print("ERROR: ticket Flow runner requires a Flow name", file=sys.stderr)
        return 2
    if custom_path is None and args[0] not in _FLOW_TYPES:
        choices = ", ".join(sorted(_FLOW_TYPES))
        print(f"ERROR: ticket Flow runner requires one of: {choices}", file=sys.stderr)
        return 2
    flow_name = args.pop(0)
    adapter = TicketBoardFlowExecution()
    if custom_path is None:
        return _FLOW_TYPES[flow_name]().main(args, adapter=adapter)
    try:
        flow = _load_project_flow(custom_path, flow_name)
    except (OSError, TicketFlowLoadError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    flow.configure_flow_execution(adapter)
    return flow.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
