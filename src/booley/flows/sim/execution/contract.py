"""Stable caller-facing values for Simulation execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class InvalidSimulationRequestError(ValueError):
    """A Simulation execution request violates the interface invariants."""


@dataclass(frozen=True)
class NamedTests:
    """A nonempty ordered selection of unique registered test names."""

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.names:
            raise InvalidSimulationRequestError("named Simulation test selection must not be empty")
        if any(not name for name in self.names):
            raise InvalidSimulationRequestError("Simulation test names must not be empty")
        if len(set(self.names)) != len(self.names):
            raise InvalidSimulationRequestError("Simulation test names must be unique")


@dataclass(frozen=True)
class DefaultSelection:
    """Run the Target's native default or an unfiltered Cocotb module."""


SimulationSelection = NamedTests | DefaultSelection


@dataclass(frozen=True)
class SimulationOptions:
    """Root-independent invocation policy supplied by the Flow."""

    trace: bool = False
    timeout_ms: int | None = None
    result_verbosity: str = "compact"
    no_kill: bool = False
    report_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.timeout_ms is not None and self.timeout_ms <= 0:
            raise InvalidSimulationRequestError("Simulation timeout must be positive")
        if self.result_verbosity not in {"compact", "full"}:
            raise InvalidSimulationRequestError(
                "Simulation result verbosity must be 'compact' or 'full'"
            )


@dataclass(frozen=True)
class PreRunEvidence:
    """Outcome of one Project-owned Pre-Run Commands firing."""

    commands: tuple[str, ...]
    status: str
    elapsed_s: float
    detail: str = ""


@dataclass(frozen=True)
class SimulationTestOutcome:
    """Immutable normalized result for one selected Simulation test."""

    name: str
    passed: bool
    elapsed_s: float = 0.0
    inconclusive: bool = False
    reason: str = ""
    sva_errors: int = 0


@dataclass(frozen=True)
class SimulationTargetOutcome:
    """Immutable aggregate returned by the deep execution seam."""

    target: str
    target_identity: str
    toplevel: str
    eda_tool: str
    passed: bool
    elapsed_s: float
    tests: tuple[SimulationTestOutcome, ...]
    pre_runs: tuple[PreRunEvidence, ...] = ()


@dataclass(frozen=True)
class SimulationPreview:
    """Side-effect-free command descriptions in execution order."""

    commands: tuple[tuple[str, ...], ...]


__all__ = [
    "DefaultSelection",
    "InvalidSimulationRequestError",
    "NamedTests",
    "PreRunEvidence",
    "SimulationOptions",
    "SimulationPreview",
    "SimulationSelection",
    "SimulationTargetOutcome",
    "SimulationTestOutcome",
]
