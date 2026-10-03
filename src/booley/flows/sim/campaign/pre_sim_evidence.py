"""Immutable, attempt-owned evidence for actual Pre-Sim Commands firings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from booley.core.boundary import require_finite_number, require_int, require_str_value
from booley.flows.sim.execution.contract import PreSimEvidence, PreSimScopeStoppedError
from booley.flows.sim.execution.pre_sim import TAIL_MAX_BYTES
from booley.flows.sim.execution.pre_sim_reporting import pre_sim_document
from booley.runtime.supervised_execution import current_supervised_execution
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_datetime, utc_now_rfc3339

from .codec import (
    MANIFEST_MAX_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from .planning import manifest_digest

if TYPE_CHECKING:
    from .coordinator import WorkExecutionRequest
    from .model import SimulationAttempt, SimulationCampaignManifest

_SCHEMA = "booley.pre-sim-evidence/v1"
_FIELDS = frozenset(
    (
        "$schema",
        "campaign_id",
        "manifest_sha256",
        "work_item_id",
        "attempt_id",
        "producer_invocation_id",
        "ordinal",
        "target",
        "role",
        "revision",
        "recorded_at",
        "command_count",
        "test_names",
        "status",
        "returncode",
        "elapsed_s",
        "detail",
        "stdout_tail",
        "stderr_tail",
    )
)


@dataclass(frozen=True, slots=True)
class PreSimFiring:
    """Authenticated record with its immutable origin and terminal provenance."""

    manifest_path: Path
    path: Path
    reference: Mapping[str, object]
    document: Mapping[str, object]
    terminal: bool

    @property
    def key(self) -> tuple[str, str, str, int]:
        value = self.document
        return (
            cast(str, value["campaign_id"]),
            cast(str, value["work_item_id"]),
            cast(str, value["attempt_id"]),
            cast(int, value["ordinal"]),
        )


def _reference(path: Path, raw: bytes, owner: str) -> dict[str, object]:
    return {
        "kind": "pre_sim_commands",
        "path": f"pre-sim/{path.name}",
        "bytes": len(raw),
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "owner": owner,
    }


def publish_pre_sim_firing(
    request: WorkExecutionRequest,
    evidence: PreSimEvidence,
    *,
    ordinal: int = 1,
    checkpoint: Callable[[str], None] | None = None,
) -> Mapping[str, object]:
    """Publish before later build checks or simulator work can fail."""
    decoder = PreSimFiringDecoder(request.manifest)
    document = {
        "$schema": _SCHEMA,
        "campaign_id": request.manifest.document["campaign_id"],
        "manifest_sha256": decoder.manifest_sha256,
        "work_item_id": request.work_item["work_item_id"],
        "attempt_id": request.attempt_id,
        "producer_invocation_id": request.producer_invocation_id,
        "ordinal": ordinal,
        "target": request.manifest.document["target"],
        "role": request.work_item["role"],
        "revision": request.work_item["revision"],
        "recorded_at": utc_now_rfc3339(),
        **pre_sim_document(evidence),
    }
    raw = canonical_json_bytes(document)
    if len(raw) > MANIFEST_MAX_BYTES:
        raise SimulationCampaignIntegrityError("Pre-Sim Commands evidence exceeds byte limit")
    attempt = request.store.read_attempt(request.attempt_directory)
    firing = decoder.decode(raw, request.work_item, attempt)
    key = (
        cast(str, firing["campaign_id"]),
        cast(str, firing["work_item_id"]),
        cast(str, firing["attempt_id"]),
        cast(int, firing["ordinal"]),
    )
    if checkpoint is not None:
        checkpoint("before:pre_sim_evidence")
    observer = request.pre_sim_firing_published
    path = request.store.publish_pre_sim_evidence(
        request.attempt_directory,
        ordinal,
        raw,
        on_published=(lambda: observer(key)) if observer is not None else None,
    )
    if checkpoint is not None:
        checkpoint("after:pre_sim_evidence")
    scope = current_supervised_execution()
    if scope is not None and scope.cancelled():
        raise PreSimScopeStoppedError("execution scope stopped after Pre-Sim Commands")
    return _reference(path, raw, request.attempt_id)


@dataclass(frozen=True)
class PreSimFiringDecoder:
    """Reuse one immutable manifest's authenticated identity across a snapshot read."""

    manifest: SimulationCampaignManifest
    manifest_sha256: str = dataclass_field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_sha256", manifest_digest(self.manifest))

    def decode(
        self, raw: bytes, item: Mapping[str, object], attempt: SimulationAttempt
    ) -> Mapping[str, object]:
        return _decode_pre_sim_firing(raw, self.manifest, item, attempt, self.manifest_sha256)


def _decode_pre_sim_firing(
    raw: bytes,
    manifest: SimulationCampaignManifest,
    item: Mapping[str, object],
    attempt: SimulationAttempt,
    authenticated_manifest_sha256: str,
) -> Mapping[str, object]:
    """Reject unbound, malformed, or over-budget records at the storage boundary."""
    try:
        value = json.loads(raw)
        _validate_fields(value, raw)
        _validate_attempt_binding(manifest, item, attempt, authenticated_manifest_sha256)
        expected = {
            "campaign_id": manifest.document["campaign_id"],
            "manifest_sha256": authenticated_manifest_sha256,
            "work_item_id": item["work_item_id"],
            "attempt_id": attempt.document["attempt_id"],
            "producer_invocation_id": attempt.document["producer_invocation_id"],
            "target": manifest.document["target"],
            "role": item["role"],
            "revision": item["revision"],
        }
        if any(value[key] != expected_value for key, expected_value in expected.items()):
            raise ValueError("Pre-Sim Commands evidence owner disagrees")
        commands = manifest.document["workload"]["source_recipe"]["pre_sim_commands"]
        if value["command_count"] != len(commands) or not commands:
            raise ValueError("Pre-Sim Commands evidence command count disagrees")
        selected = item["selection"]["names"]
        names = value["test_names"]
        if item["kind"] != "coverage_aggregate" and tuple(names) != tuple(selected):
            raise ValueError("Pre-Sim Commands evidence selection disagrees")
        if item["kind"] != "coverage_aggregate" and value["ordinal"] != 1:
            raise ValueError("ordinary attempt cannot have multiple hook firings")
        if selected and any(name not in selected for name in names):
            raise ValueError("Pre-Sim Commands evidence selection is outside workload")
    except (ValueError, TypeError, KeyError) as exc:
        raise SimulationCampaignIntegrityError(
            f"invalid Pre-Sim Commands evidence: {exc}"
        ) from exc
    value["test_names"] = tuple(value["test_names"])
    value["target"] = MappingProxyType(value["target"])
    return MappingProxyType(value)


def _validate_attempt_binding(manifest, item, attempt, authenticated_manifest_sha256) -> None:
    expected = {
        "campaign_id": manifest.document["campaign_id"],
        "manifest_sha256": authenticated_manifest_sha256,
        "workload_sha256": manifest.document["fingerprints"]["workload_sha256"],
        "work_item_id": item["work_item_id"],
        "build_variant_id": item["build_variant_id"],
    }
    if any(attempt.document[field] != value for field, value in expected.items()):
        raise ValueError("hook attempt does not bind its frozen workload")


def _validate_fields(value: object, raw: bytes) -> None:
    if not isinstance(value, dict) or set(value) != _FIELDS or value["$schema"] != _SCHEMA:
        raise ValueError("Pre-Sim Commands evidence fields/schema disagree")
    if len(raw) > MANIFEST_MAX_BYTES:
        raise ValueError("Pre-Sim Commands evidence exceeds byte limit")
    for field, maximum in (("ordinal", 4096), ("command_count", 10000)):
        number = require_int(value[field], field=field)
        if not 1 <= number <= maximum:
            raise ValueError(f"invalid {field}")
    if require_int(value["producer_invocation_id"], field="producer_invocation_id") < 1:
        raise ValueError("invalid producer invocation")
    timestamp = require_str_value(value["recorded_at"], field="recorded_at")
    if rfc3339_from_datetime(parse_timestamp(timestamp)) != timestamp:
        raise ValueError("noncanonical hook timestamp")
    elapsed = require_finite_number(value["elapsed_s"], field="elapsed_s")
    if elapsed < 0:
        raise ValueError("negative elapsed_s")
    rc = value["returncode"]
    if rc is not None and not -(2**31) <= require_int(rc, field="returncode") < 2**31:
        raise ValueError("invalid hook returncode")
    if value["status"] not in {"passed", "failed", "spawn_error", "timed_out"}:
        raise ValueError("invalid Pre-Sim Commands status")
    if value["status"] == "timed_out" and rc is not None:
        raise ValueError("timed out hook cannot supply an exit code")
    for field in ("detail", "stdout_tail", "stderr_tail"):
        text = require_str_value(value[field], field=field, allow_empty=True)
        if len(text.encode("utf-8")) > TAIL_MAX_BYTES:
            raise ValueError(f"over-budget {field}")
    _validate_names(value["test_names"])


def _validate_names(names: object) -> None:
    if not isinstance(names, list) or len(names) > 4000 or len(set(names)) != len(names):
        raise ValueError("invalid hook selection")
    for name in names:
        text = require_str_value(name, field="test name")
        if not text or len(text.encode("utf-8")) > 512:
            raise ValueError("invalid hook test name")
