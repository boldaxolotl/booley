"""Immutable, attempt-owned evidence for actual Pre-Sim Commands firings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from booley.flows.sim import faults
from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.execution.contract import PreSimEvidence, PreSimScopeStoppedError
from booley.flows.sim.execution.pre_sim_reporting import pre_sim_document
from booley.runtime.supervised_execution import current_supervised_execution
from booley.runtime.timefmt import utc_now_rfc3339

from .codec import MANIFEST_MAX_BYTES, SimulationRecordIntegrityError, canonical_json_bytes
from .model import FrozenJson, SimulationRecordDocument
from .planning import manifest_digest

if TYPE_CHECKING:
    from .coordinator import WorkExecutionRequest
    from .model import SimulationRecordManifest

_SCHEMA = "booley.pre-sim-evidence/v2"


@dataclass(frozen=True, slots=True)
class PreSimFiring:
    """Parsed firing with its origin and terminal provenance."""

    manifest_path: Path
    path: Path
    document: Mapping[str, object]
    terminal: bool

    @property
    def key(self) -> tuple[str, str, str, int]:
        return _firing_key(self.document)


@dataclass(frozen=True)
class PreSimFiringRecord(SimulationRecordDocument):
    """Recorded firing facts, without storage provenance."""


def _firing_key(value: Mapping[str, object]) -> tuple[str, str, str, int]:
    return (
        cast(str, value["simulation_record_id"]),
        cast(str, value["work_item_id"]),
        cast(str, value["attempt_id"]),
        cast(int, value["ordinal"]),
    )


def _reference(path: Path, raw: bytes, owner: str) -> dict[str, object]:
    return {
        "kind": "pre_sim_commands",
        "path": f"pre-sim/{path.name}",
        "bytes": len(raw),
        "sha256": content_digest(raw),
        "owner": owner,
    }


def publish_pre_sim_firing(
    request: WorkExecutionRequest, evidence: PreSimEvidence, *, ordinal: int = 1
) -> Mapping[str, object]:
    """Publish before later build checks or simulator work can fail."""
    decoder = request.pre_sim_decoder
    document = {
        "$schema": _SCHEMA,
        **decoder.owner(request.work_item, request.attempt_id, request.producer_invocation_id),
        "ordinal": ordinal,
        "recorded_at": utc_now_rfc3339(),
        **pre_sim_document(evidence),
    }
    raw = canonical_json_bytes(document)
    if len(raw) > MANIFEST_MAX_BYTES:
        raise SimulationRecordIntegrityError("Pre-Sim Commands evidence exceeds byte limit")
    key = _firing_key(document)
    faults.fault_point("before:pre_sim_evidence")
    observer = request.pre_sim_firing_published
    path = request.store.publish_pre_sim_evidence(
        request.attempt_directory,
        ordinal,
        raw,
        on_published=(lambda: observer(key)) if observer is not None else None,
    )
    faults.fault_point("after:pre_sim_evidence")
    scope = current_supervised_execution()
    if scope is not None and scope.cancelled():
        raise PreSimScopeStoppedError("execution scope stopped after Pre-Sim Commands")
    return _reference(path, raw, request.attempt_id)


def decode_pre_sim_firing(raw: bytes) -> Mapping[str, object]:
    """Parse one recorded firing and check its schema; its fields are trusted."""
    try:
        value = cast(Mapping[str, FrozenJson], json.loads(raw))
        document = PreSimFiringRecord(value).document
        if document["$schema"] != _SCHEMA:
            raise ValueError(f"unsupported $schema {document['$schema']!r}")
    except (KeyError, TypeError, ValueError) as exc:
        raise SimulationRecordIntegrityError(f"invalid Pre-Sim Commands evidence: {exc}") from exc
    return document


@dataclass(frozen=True)
class PreSimFiringDecoder:
    """Derive firing owners from one immutable manifest's authenticated identity."""

    manifest: SimulationRecordManifest
    manifest_sha256: str = dataclass_field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_sha256", manifest_digest(self.manifest))

    def owner(
        self, item: Mapping[str, object], attempt_id: object, invocation: object
    ) -> dict[str, object]:
        return {
            "simulation_record_id": self.manifest.document["simulation_record_id"],
            "manifest_sha256": self.manifest_sha256,
            "work_item_id": item["work_item_id"],
            "attempt_id": attempt_id,
            "producer_invocation_id": invocation,
            "target": self.manifest.document["target"],
            "role": item["role"],
            "revision": item["revision"],
        }
