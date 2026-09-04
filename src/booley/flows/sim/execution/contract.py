"""Stable caller-facing values for Simulation execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from booley.flows.sim.build import BuildOutcome

SimulationVerdict = Literal["pass", "fail", "elab_error", "timeout", "inconclusive"]


class InvalidSimulationRequestError(ValueError):
    """A Simulation execution request violates the interface invariants."""


@dataclass(frozen=True)
class NamedTests:
    """A nonempty ordered selection of unique registered test names."""

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.names:
            raise InvalidSimulationRequestError(
                "named Simulation test selection must not be empty"
            )
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
    test_names: tuple[str, ...]
    status: str
    elapsed_s: float
    detail: str = ""


@dataclass(frozen=True)
class SimulationArtifactEvidence:
    """A current-attempt artifact validated by shared execution policy."""

    kind: str
    path: str
    size: int
    test_names: tuple[str, ...]
    top_scope: str = ""
    signal_count: int = 0
    total_ticks: int = 0


@dataclass(frozen=True)
class SimulationInfrastructureFailure:
    """An expected execution failure that produced no design verdict."""

    kind: str
    message: str
    missing_executable: str = ""
    detail: str = ""


@dataclass(frozen=True)
class SimulationTestOutcome:
    """Immutable normalized result for one selected Simulation test."""

    name: str
    verdict: SimulationVerdict
    passed: bool
    elapsed_s: float = 0.0
    build_s: float = 0.0
    cycles: int | None = None
    cycle_status: str = "missing"
    inconclusive: bool = False
    reason: str = ""
    sva_errors: int = 0
    error_tail: str = ""
    timed_out: bool = False
    elab_failed: bool = False
    test_validated: bool = True
    build: BuildOutcome | None = None
    artifacts: tuple[SimulationArtifactEvidence, ...] = ()
    run_log_path: str = ""
    workload_snapshot: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SimulationTargetOutcome:
    """Immutable aggregate returned by the deep execution seam."""

    target: str
    target_identity: str
    toplevel: str
    eda_tool: str
    passed: bool
    verdict: Literal["pass", "fail", "inconclusive", "error"]
    elapsed_s: float
    tests: tuple[SimulationTestOutcome, ...]
    builds: tuple[BuildOutcome, ...] = ()
    pre_runs: tuple[PreRunEvidence, ...] = ()
    artifacts: tuple[SimulationArtifactEvidence, ...] = ()
    diagnostics: tuple[str, ...] = ()
    infrastructure_failure: SimulationInfrastructureFailure | None = None


@dataclass(frozen=True)
class SimulationPreview:
    """Side-effect-free command descriptions in execution order."""

    commands: tuple[tuple[str, ...], ...]


__all__ = [
    "DefaultSelection",
    "InvalidSimulationRequestError",
    "NamedTests",
    "PreRunEvidence",
    "SimulationArtifactEvidence",
    "SimulationInfrastructureFailure",
    "SimulationOptions",
    "SimulationPreview",
    "SimulationSelection",
    "SimulationTargetOutcome",
    "SimulationTestOutcome",
    "SimulationVerdict",
]
