"""Pure planning helpers for durable Simulation Record workloads."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from booley.flows.sim.record_files import document_digest
from booley.flows.sim.runtime_inputs import preview_runtime_inputs
from booley.targets.domain import TargetInput

from .codec import (
    SimulationRecordIntegrityError,
    canonical_json_bytes,
    decode_simulation_record_manifest,
    validate_relative_path,
)
from .model import SimulationRecordManifest

# Report order. Each key names one per-area fingerprint of a manifest.
_AREA_LABELS = {
    "target_recipe": "Target recipe",
    "source_closures": "source closures",
    "required_suite": "test suite",
    "planning_disclosures": "planning disclosures",
    "prerequisites": "prerequisites",
    "work_items": "work items",
}
_OTHER_AREA = "other workload settings"


@dataclass(frozen=True, slots=True)
class WorkloadDiagnostic:
    """The areas of a resumed workload that differ from its Simulation Record."""

    areas: tuple[str, ...]

    @property
    def detail(self) -> dict[str, object]:
        return {"mismatches": list(self.areas)}

    def report(self) -> str:
        areas = ", ".join(self.areas)
        return f"record workload mismatch: {areas} changed since the original run"


class SimulationRecordWorkloadMismatchError(SimulationRecordIntegrityError):
    """Resume refusal naming the areas that changed."""

    def __init__(self, diagnostic: WorkloadDiagnostic) -> None:
        super().__init__(diagnostic.report())
        self.diagnostic = diagnostic


def derive_runtime_input_declarations(
    inputs: Sequence[TargetInput],
) -> tuple[Mapping[str, str], ...]:
    """Derive ordered workload declarations from Target ``user`` copy inputs."""
    destinations = preview_runtime_inputs(inputs)
    declarations: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for destination in destinations:
        try:
            normalized = validate_relative_path(
                _relative_path(destination, field="runtime input destination")
            )
        except SimulationRecordIntegrityError as exc:
            raise SimulationRecordIntegrityError(
                f"Target input copyto destination {destination!r} must be a relative path "
                f"inside the run directory ({exc}); fix copyto in the Target's .core file"
            ) from exc
        if normalized in seen:
            raise SimulationRecordIntegrityError(
                f"duplicate runtime input destination {normalized!r}"
            )
        seen.add(normalized)
        identity = {"source_artifact_path": normalized, "destination": normalized}
        declarations.append({"declaration_id": document_digest(identity), **identity})
    return tuple(declarations)


def finalize_manifest(document: Mapping[str, object]) -> SimulationRecordManifest:
    """Compute every component fingerprint and return the decoded manifest."""
    mutable = _json_copy(document)
    required = {
        "$schema",
        "simulation_record_id",
        "created_at",
        "origin",
        "target",
        "workload",
        "required_suite",
        "build_variants",
        "planning_disclosures",
        "prerequisites",
        "work_items",
    }
    if set(mutable) != required:
        raise SimulationRecordIntegrityError(
            f"manifest planning input must have exact fields {sorted(required)}"
        )
    mutable["fingerprints"] = _manifest_fingerprints(mutable)
    raw = canonical_json_bytes(mutable)
    return decode_simulation_record_manifest(raw)


def _area_members(document: Mapping[str, Any]) -> dict[str, object]:
    """The members each per-area fingerprint digests."""
    workload = document["workload"]
    return {
        "target_recipe": {
            "target": document["target"],
            "source_recipe": workload["source_recipe"],
            "build_recipe": workload["build_recipe"],
        },
        "source_closures": [
            {
                "build_variant_id": variant["build_variant_id"],
                "source_closure": variant["source_closure"],
            }
            for variant in document["build_variants"]
        ],
        "required_suite": document["required_suite"],
        "planning_disclosures": document["planning_disclosures"],
        "prerequisites": document["prerequisites"],
        "work_items": document["work_items"],
    }


def _other_members(document: Mapping[str, Any]) -> dict[str, object]:
    """The identity members no named area digests.

    A Build Variant's other members are derived from the workload and the Target
    recipe, so including them would report every source edit here too.
    """
    recipes = {"source_recipe", "build_recipe"}
    workload = {k: v for k, v in document["workload"].items() if k not in recipes}
    return {"$schema": document["$schema"], "workload": workload}


def _manifest_fingerprints(mutable: Mapping[str, object]) -> dict[str, str]:
    fingerprints = {
        f"{name}_sha256": document_digest(member)
        for name, member in _area_members(mutable).items()
    }
    fingerprints["workload_sha256"] = document_digest(
        {
            key: mutable[key]
            for key in (
                "target",
                "workload",
                "required_suite",
                "build_variants",
                "planning_disclosures",
                "prerequisites",
                "work_items",
            )
        }
    )
    return fingerprints


def workload_diagnostic(
    expected: SimulationRecordManifest, current: SimulationRecordManifest
) -> WorkloadDiagnostic:
    """Decide workload identity and, only when it differs, name the changed areas."""
    suite = cast("Mapping[str, Any]", expected.document["required_suite"])
    # A record written before per-Target fingerprints compares the whole file.
    legacy = "target_tests" not in suite
    left = _identity_document(expected, legacy=legacy, recorded=suite)
    right = _identity_document(current, legacy=legacy, recorded=suite)
    if left == right:
        return WorkloadDiagnostic(())
    # Digests, not ==, so that a parameter changed from false to 0 is still named.
    before, after = _area_members(left), _area_members(right)
    areas = [
        label
        for key, label in _AREA_LABELS.items()
        if document_digest(before[key]) != document_digest(after[key])
    ]
    other_differs = document_digest(_other_members(left)) != document_digest(_other_members(right))
    if other_differs or not areas:
        areas.append(_OTHER_AREA)
    return WorkloadDiagnostic(tuple(areas))


def verify_workload(expected: SimulationRecordManifest, current: SimulationRecordManifest) -> None:
    """Reject resume of a changed workload, naming what changed."""
    diagnostic = workload_diagnostic(expected, current)
    if diagnostic.areas:
        raise SimulationRecordWorkloadMismatchError(diagnostic)


def manifest_digest(manifest: SimulationRecordManifest) -> str:
    """Return the canonical digest used by durable records."""
    return document_digest(manifest.document)


def _identity_document(
    manifest: SimulationRecordManifest, *, legacy: bool, recorded: Mapping[str, Any]
) -> Mapping[str, object]:
    document = manifest.document
    identity: dict[str, object] = {
        key: document[key]
        for key in (
            "$schema",
            "target",
            "workload",
            "build_variants",
            "planning_disclosures",
            "prerequisites",
            "work_items",
        )
    }
    suite = cast("Mapping[str, Any]", document["required_suite"])
    identity["required_suite"] = _suite_identity(suite, legacy=legacy, recorded=recorded)
    return identity


def _suite_identity(
    suite: Mapping[str, Any], *, legacy: bool, recorded: Mapping[str, Any]
) -> dict[str, object]:
    """The members of ``required_suite`` that decide whether a resume is compatible."""
    if not legacy:
        keys = ("names", "default_invocation", "target_tests")
        return {key: suite[key] for key in keys}
    keys = ("names", "source_path", "source_bytes", "source_sha256", "default_invocation")
    identity = {key: suite[key] for key in keys}
    if recorded["default_invocation"]:
        identity["registered_tests"] = _registered_tests(suite)
    return identity


def _registered_tests(suite: Mapping[str, Any]) -> list[str]:
    """The Test names the Target's section registers, empty for a legacy record."""
    return [item["name"] for item in suite.get("target_tests", {}).get("tests", ())]


def _relative_path(value: str, *, field: str) -> str:
    path = Path(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SimulationRecordIntegrityError(f"invalid {field} {value!r}")
    return path.as_posix()


def _json_copy(value: object) -> Any:
    return json.loads(canonical_json_bytes(value))


__all__ = [
    "SimulationRecordWorkloadMismatchError",
    "WorkloadDiagnostic",
    "derive_runtime_input_declarations",
    "finalize_manifest",
    "manifest_digest",
    "verify_workload",
    "workload_diagnostic",
]
