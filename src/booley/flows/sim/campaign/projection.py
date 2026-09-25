"""Versioned Target-local Simulation Campaign acceptance projections."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

SIMULATION_PROJECTION_SCHEMA = "booley.simulation-projection/v2"


class ProjectionTrust(StrEnum):
    """Authority available from one decoded projection profile."""

    AUTHENTICATED = "authenticated"
    COVERAGE_AUTHENTICATED_LEGACY = "coverage_authenticated_legacy"
    TARGET_CONSISTENT_LEGACY = "target_consistent_legacy"


@dataclass(frozen=True, slots=True)
class SimulationProjection:
    """Decoded projection plus its explicit provenance grade."""

    document: Mapping[str, object]
    trust: ProjectionTrust


def decode_simulation_projection(
    value: object, *, coverage_expected: bool
) -> SimulationProjection:
    """Decode a new projection or classify the exact legacy profile."""
    if not isinstance(value, dict):
        raise ValueError("Simulation projection must be an object")
    schema = value.get("$schema")
    if schema is None:
        _validate_legacy(value)
        if coverage_expected:
            if (
                value.get("coverage_campaign") != "coverage.json"
                or value.get("coverage_campaign_base") != "origin_target"
            ):
                raise ValueError("legacy Coverage projection pointers disagree")
            trust = ProjectionTrust.COVERAGE_AUTHENTICATED_LEGACY
        else:
            trust = ProjectionTrust.TARGET_CONSISTENT_LEGACY
        return SimulationProjection(value, trust)
    if schema != SIMULATION_PROJECTION_SCHEMA:
        raise ValueError("unsupported Simulation projection schema")
    _validate_common(value)
    fields = {
        "$schema",
        "flow",
        "mode",
        "target",
        "target_identity",
        "passed",
        "inconclusive",
        "tests",
        "campaign_id",
        "campaign_manifest",
        "complete",
    }
    if coverage_expected:
        fields.update({"coverage_campaign", "collection", "evaluation"})
    if set(value) != fields:
        raise ValueError("Simulation projection fields are not exact")
    try:
        campaign_id = uuid.UUID(str(value["campaign_id"]))
    except ValueError as exc:
        raise ValueError("Simulation projection Campaign identity is invalid") from exc
    if campaign_id.version != 4 or str(campaign_id) != value["campaign_id"]:
        raise ValueError("Simulation projection Campaign identity is invalid")
    return SimulationProjection(value, ProjectionTrust.AUTHENTICATED)


def _validate_common(value: Mapping[str, object]) -> None:
    if (
        value.get("flow") != "sim"
        or value.get("mode") != "simulate"
        or not isinstance(value.get("target"), str)
        or not isinstance(value.get("target_identity"), str)
        or type(value.get("passed")) is not bool
        or type(value.get("inconclusive")) is not bool
        or not isinstance(value.get("tests"), list)
        or type(value.get("complete")) is not bool
    ):
        raise ValueError("Simulation projection fields are invalid")


def _validate_legacy(value: Mapping[str, object]) -> None:
    if (
        value.get("flow") != "sim"
        or not isinstance(value.get("target"), str)
        or not isinstance(value.get("target_identity"), str)
        or type(value.get("complete")) is not bool
    ):
        raise ValueError("legacy Simulation projection fields are invalid")


__all__ = [
    "SIMULATION_PROJECTION_SCHEMA",
    "ProjectionTrust",
    "SimulationProjection",
    "decode_simulation_projection",
]
