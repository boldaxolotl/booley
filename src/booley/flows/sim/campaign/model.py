"""Immutable in-memory values for a Simulation Campaign.

The model has no filesystem, process, CLI, persistence, or validation imports.
Untrusted JSON becomes one of these values only through :mod:`.codec`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


class ExecutionObservation(StrEnum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CRASH = "crash"
    SETUP_ERROR = "setup_error"
    BLOCKED_BY_BUILD = "blocked_by_build"
    NOT_RUN = "not_run"


class FailureClass(StrEnum):
    DESIGN = "design"
    INFRASTRUCTURE = "infrastructure"


class FunctionalObservation(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_OBSERVED = "not_observed"


class AssertionObservation(StrEnum):
    CLEAN = "clean"
    DIRTY = "dirty"
    NOT_OBSERVED = "not_observed"


class StrictGrade(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


def grade_observations(
    execution: ExecutionObservation,
    failure_class: FailureClass | None,
    functional: FunctionalObservation,
    assertions: AssertionObservation,
) -> StrictGrade:
    """Apply the exhaustive Simulation Campaign grade table."""
    grade = StrictGrade.INCONCLUSIVE
    if failure_class is FailureClass.INFRASTRUCTURE:
        grade = StrictGrade.ERROR
    elif execution in {
        ExecutionObservation.TIMEOUT,
        ExecutionObservation.CRASH,
        ExecutionObservation.SETUP_ERROR,
        ExecutionObservation.BLOCKED_BY_BUILD,
    }:
        grade = StrictGrade.FAIL
    elif execution is ExecutionObservation.COMPLETED:
        if functional is FunctionalObservation.FAIL or assertions is AssertionObservation.DIRTY:
            grade = StrictGrade.FAIL
        elif functional is FunctionalObservation.PASS:
            grade = StrictGrade.PASS
    return grade


@dataclass(frozen=True)
class SimulationCampaignPlan:
    target: Mapping[str, FrozenJson]
    selection: Mapping[str, FrozenJson]
    workload: Mapping[str, FrozenJson]
    required_suite: Mapping[str, FrozenJson]
    build_variants: tuple[FrozenJson, ...]
    planning_disclosures: tuple[FrozenJson, ...]
    prerequisites: tuple[FrozenJson, ...]
    work_items: tuple[FrozenJson, ...]
    fingerprints: Mapping[str, FrozenJson]


@dataclass(frozen=True)
class CampaignDocument:
    document: Mapping[str, FrozenJson]

    def canonical_bytes(self) -> bytes:
        from .codec import canonical_json_bytes

        return canonical_json_bytes(self.document)


@dataclass(frozen=True)
class SimulationCampaignManifest(CampaignDocument):
    pass


@dataclass(frozen=True)
class SimulationAttempt(CampaignDocument):
    pass


@dataclass(frozen=True)
class BundleBuildAttempt(CampaignDocument):
    pass


@dataclass(frozen=True)
class BundleBuildResult(CampaignDocument):
    pass


@dataclass(frozen=True)
class SimulationResult(CampaignDocument):
    pass


@dataclass(frozen=True)
class SimulatorBundle(CampaignDocument):
    pass


@dataclass(frozen=True)
class ExecutableSnapshot(CampaignDocument):
    pass
