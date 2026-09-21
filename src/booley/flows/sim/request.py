"""Structured input for the sim Flow."""

from dataclasses import dataclass

from booley.flows.request import FlowRequest

from .mode import SimulationMode


@dataclass(kw_only=True)
class SimRequest(FlowRequest):
    # ``None`` is meaningful transport presence.  Campaign preparation chooses
    # SIMULATE only on the new-campaign path; resume must be able to distinguish
    # an omitted mode from an explicitly supplied one.
    mode: SimulationMode | None = None
    # ``None`` preserves omission across typed and transport boundaries.  An
    # empty tuple is not the same request: callers that explicitly provide a
    # selector must name at least one test.
    test: tuple[str, ...] | None = None
    trace: bool = False
    coverage: bool = False
    result_verbosity: str = "compact"
    no_kill: bool = False
    _legacy_standalone_without_elab: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.coverage, bool):
            raise ValueError("coverage must be boolean")
        if self.mode is not None:
            self.mode = SimulationMode(self.mode)
        if self.test is not None:
            if isinstance(self.test, list):
                self.test = tuple(self.test)
            if not isinstance(self.test, tuple) or not self.test or any(
                not isinstance(name, str) or not name for name in self.test
            ):
                raise ValueError("test must be a non-empty array of exact names")
            if len(set(self.test)) != len(self.test):
                raise ValueError("test names must be unique")
        if self.result_verbosity not in ("compact", "full"):
            raise ValueError("result_verbosity must be compact or full")
