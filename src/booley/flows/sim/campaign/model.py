"""Immutable in-memory values for a Simulation Campaign.

The model has no filesystem, process, CLI, persistence, or validation imports.
Untrusted JSON becomes one of these values only through :mod:`.codec`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias, cast

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


def _freeze_json(value: object) -> FrozenJson:
    """Copy JSON-shaped input into recursively immutable storage."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("document object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"document contains non-JSON value {type(value).__name__}")


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
class SimulationCampaignDocument:
    document: Mapping[str, FrozenJson]

    def __post_init__(self) -> None:
        frozen = _freeze_json(self.document)
        if not isinstance(frozen, Mapping):
            raise TypeError("campaign document must be an object")
        object.__setattr__(self, "document", frozen)

    def canonical_bytes(self) -> bytes:
        from .codec import encode_simulation_campaign_document

        return encode_simulation_campaign_document(self)


@dataclass(frozen=True)
class SimulationCampaignManifest(SimulationCampaignDocument):
    pass


@dataclass(frozen=True)
class SimulationAttempt(SimulationCampaignDocument):
    pass


@dataclass(frozen=True)
class BundleBuildAttempt(SimulationCampaignDocument):
    pass


@dataclass(frozen=True)
class BundleBuildResult(SimulationCampaignDocument):
    pass


@dataclass(frozen=True)
class SimulationResult(SimulationCampaignDocument):
    pass


@dataclass(frozen=True)
class SimulatorBundle(SimulationCampaignDocument):
    pass


@dataclass(frozen=True)
class ExecutableSnapshot(SimulationCampaignDocument):
    pass


@dataclass(frozen=True, init=False)
class SimulationCampaignPlan:
    """A validated manifest paired with its immutable scheduled work-item order."""

    manifest: SimulationCampaignManifest
    work_item_ids: tuple[str, ...]

    def __new__(cls) -> SimulationCampaignPlan:
        raise TypeError("use create_simulation_campaign_plan()")

    @classmethod
    def _from_validated_manifest(
        cls, manifest: SimulationCampaignManifest
    ) -> SimulationCampaignPlan:
        from .codec import (
            decode_simulation_campaign_manifest,
            encode_simulation_campaign_manifest,
        )

        raw = encode_simulation_campaign_manifest(manifest)
        validated = decode_simulation_campaign_manifest(raw)
        items = cast(tuple[Mapping[str, FrozenJson], ...], validated.document["work_items"])
        instance = object.__new__(cls)
        object.__setattr__(instance, "manifest", validated)
        object.__setattr__(
            instance,
            "work_item_ids",
            tuple(cast(str, item["work_item_id"]) for item in items),
        )
        return instance


def create_simulation_campaign_plan(
    manifest: SimulationCampaignManifest,
) -> SimulationCampaignPlan:
    """Validate a manifest and derive its immutable scheduled work-item order."""
    return SimulationCampaignPlan._from_validated_manifest(manifest)
