"""Structured input for the sim Flow."""

from dataclasses import dataclass

from booley.flows.request import FlowRequest

from .mode import SimulationMode


@dataclass(kw_only=True)
class SimRequest(FlowRequest):
    mode: SimulationMode = SimulationMode.SIMULATE
    test: str | None = None
    skip: str | None = None
    trace: bool = False
    result_verbosity: str = "compact"
    no_kill: bool = False
    _legacy_standalone_without_elab: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.mode = SimulationMode(self.mode)
        if self.result_verbosity not in ("compact", "full"):
            raise ValueError("result_verbosity must be compact or full")
