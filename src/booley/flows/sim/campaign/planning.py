"""Pure planning helpers for durable Simulation Campaign workloads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from booley.flows.sim.runtime_inputs import preview_runtime_inputs
from booley.targets.domain import TargetInput

from .codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulation_campaign_manifest,
    encode_simulation_campaign_manifest,
)
from .model import SimulationCampaignManifest


@dataclass(frozen=True, slots=True)
class WorkloadMismatch:
    """One stable pointer/value difference found while validating resume."""

    pointer: str
    expected: object
    actual: object

    @property
    def message(self) -> str:
        return (
            f"{self.pointer}: manifest has {_display(self.expected)}, "
            f"current workload has {_display(self.actual)}"
        )


def canonical_sha256(value: object) -> str:
    """Hash one canonical JSON value without the document trailing newline."""
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value).rstrip(b"\n")).hexdigest()


def derive_runtime_input_declarations(
    inputs: Sequence[TargetInput],
) -> tuple[Mapping[str, str], ...]:
    """Derive ordered workload declarations from Target ``user`` copy inputs."""
    destinations = preview_runtime_inputs(inputs)
    declarations: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for destination in destinations:
        normalized = _relative_path(destination, field="runtime input destination")
        if normalized in seen:
            raise SimulationCampaignIntegrityError(
                f"duplicate runtime input destination {normalized!r}"
            )
        seen.add(normalized)
        identity = {
            "source_artifact_path": normalized,
            "destination": normalized,
        }
        declarations.append({"declaration_id": canonical_sha256(identity), **identity})
    return tuple(declarations)


def finalize_manifest(document: Mapping[str, object]) -> SimulationCampaignManifest:
    """Compute every component fingerprint and return a strictly validated manifest."""
    mutable = _json_copy(document)
    required = {
        "$schema",
        "campaign_id",
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
        raise SimulationCampaignIntegrityError(
            f"manifest planning input must have exact fields {sorted(required)}"
        )
    mutable["fingerprints"] = _manifest_fingerprints(mutable)
    raw = canonical_json_bytes(mutable)
    return decode_simulation_campaign_manifest(raw)


def _manifest_fingerprints(mutable: Mapping[str, object]) -> dict[str, str]:
    workload = cast(Mapping[str, object], mutable["workload"])
    variants = cast(Sequence[Mapping[str, object]], mutable["build_variants"])
    return {
        "target_recipe_sha256": canonical_sha256(
            {
                "target": mutable["target"],
                "source_recipe": workload["source_recipe"],
                "build_recipe": workload["build_recipe"],
            }
        ),
        "source_closures_sha256": canonical_sha256(
            [
                {
                    "build_variant_id": variant["build_variant_id"],
                    "source_closure": variant["source_closure"],
                }
                for variant in variants
            ]
        ),
        "required_suite_sha256": canonical_sha256(mutable["required_suite"]),
        "planning_disclosures_sha256": canonical_sha256(mutable["planning_disclosures"]),
        "prerequisites_sha256": canonical_sha256(mutable["prerequisites"]),
        "work_items_sha256": canonical_sha256(mutable["work_items"]),
        "workload_sha256": canonical_sha256(
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
        ),
    }


def compare_manifests(
    expected: SimulationCampaignManifest,
    current: SimulationCampaignManifest,
) -> tuple[WorkloadMismatch, ...]:
    """Return all workload-identity mismatches in deterministic pointer order."""
    left = _identity_document(expected)
    right = _identity_document(current)
    findings: list[WorkloadMismatch] = []
    _compare_value(left, right, "", findings)
    return tuple(findings)


def verify_workload(
    expected: SimulationCampaignManifest,
    current: SimulationCampaignManifest,
) -> None:
    """Reject resume with one stable complete mismatch report."""
    findings = compare_manifests(expected, current)
    if findings:
        detail = "; ".join(item.message for item in findings)
        raise SimulationCampaignIntegrityError(f"campaign workload mismatch: {detail}")


def manifest_digest(manifest: SimulationCampaignManifest) -> str:
    """Return the canonical digest used by durable records."""
    raw = encode_simulation_campaign_manifest(manifest)
    return "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()


def _identity_document(manifest: SimulationCampaignManifest) -> Mapping[str, object]:
    document = manifest.document
    return {
        key: document[key]
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


def _compare_value(
    expected: object,
    actual: object,
    pointer: str,
    findings: list[WorkloadMismatch],
) -> None:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        for key in sorted(set(expected) | set(actual)):
            child = f"{pointer}/{_escape_pointer(str(key))}"
            if key not in expected:
                findings.append(WorkloadMismatch(child, "<absent>", actual[key]))
            elif key not in actual:
                findings.append(WorkloadMismatch(child, expected[key], "<absent>"))
            else:
                _compare_value(expected[key], actual[key], child, findings)
        return
    if isinstance(expected, tuple | list) and isinstance(actual, tuple | list):
        common = min(len(expected), len(actual))
        for index in range(common):
            _compare_value(expected[index], actual[index], f"{pointer}/{index}", findings)
        for index in range(common, max(len(expected), len(actual))):
            left = expected[index] if index < len(expected) else "<absent>"
            right = actual[index] if index < len(actual) else "<absent>"
            findings.append(WorkloadMismatch(f"{pointer}/{index}", left, right))
        return
    if expected != actual:
        findings.append(WorkloadMismatch(pointer or "/", expected, actual))


def _relative_path(value: str, *, field: str) -> str:
    path = Path(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SimulationCampaignIntegrityError(f"invalid {field} {value!r}")
    return path.as_posix()


def _json_copy(value: object) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _display(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "WorkloadMismatch",
    "canonical_sha256",
    "compare_manifests",
    "derive_runtime_input_declarations",
    "finalize_manifest",
    "manifest_digest",
    "verify_workload",
]
