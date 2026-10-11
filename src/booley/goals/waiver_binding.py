"""Immutable persisted waiver facts, independent of coverage and MCP adapters.

Only storage shape is checked here. Campaign authentication, candidate eligibility
and waiver policy interpretation remain with their concrete owners. No value is
trimmed, coerced or reconstructed from mutable producer files during recovery.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, cast

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_list,
    require_positive_int,
    require_str_value,
)
from booley.goals.proposal_errors import ProposalError


def _object(value: object, where: str, fields: frozenset[str] | None = None) -> dict[str, Any]:
    try:
        raw = cast("dict[str, Any]", require_dict(value, field=where))
    except BoundaryError as exc:
        raise ProposalError(str(exc)) from exc
    for key in raw:
        _string(key, where, allow_empty=True)
    if fields is not None:
        missing, unknown = fields - raw.keys(), raw.keys() - fields
        if missing:
            raise ProposalError(f"{where}.{sorted(missing)[0]} is missing")
        if unknown:
            raise ProposalError(f"{where} has unknown fields: {sorted(unknown)}")
    return raw


def _string(value: object, where: str, *, allow_empty: bool = False) -> str:
    try:
        return require_str_value(value, field=where, allow_empty=allow_empty)
    except BoundaryError as exc:
        raise ProposalError(str(exc)) from exc


def _positive(value: object, where: str) -> int:
    try:
        return require_positive_int(value, field=where)
    except BoundaryError as exc:
        raise ProposalError(str(exc)) from exc


def _strings(value: object, where: str) -> tuple[str, ...]:
    try:
        items = require_list(value, field=where)
    except BoundaryError as exc:
        raise ProposalError(str(exc)) from exc
    return tuple(_string(item, f"{where}[{index}]") for index, item in enumerate(items))


def _json_value(value: object, where: str) -> None:
    if isinstance(value, dict):
        for key, item in cast("dict[object, object]", value).items():
            _string(key, where, allow_empty=True)
            _json_value(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(cast("list[object]", value)):
            _json_value(item, f"{where}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ProposalError(f"{where} must be finite")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ProposalError(f"{where} must be a JSON value")


@dataclass(frozen=True)
class JsonSnapshot:
    """Opaque fingerprint facts stored without mutable aliases or normalization."""

    _encoded: bytes

    def __post_init__(self) -> None:
        _require_type(self._encoded, bytes, "snapshot")
        try:
            raw = json.loads(self._encoded)
        except (ValueError, UnicodeError) as exc:
            raise ProposalError("snapshot must contain valid JSON") from exc
        _json_value(_object(raw, "snapshot"), "snapshot")

    @classmethod
    def from_json(cls, value: object, *, where: str) -> JsonSnapshot:
        """Freeze an object; neither callers nor JSON consumers retain an alias."""
        raw = _object(value, where)
        _json_value(raw, where)
        return cls(json.dumps(raw, sort_keys=True, allow_nan=False).encode())

    def to_json(self) -> dict[str, Any]:
        """Return a detached copy for the owning adapter or public display."""
        return cast("dict[str, Any]", json.loads(self._encoded))


@dataclass(frozen=True)
class WaiverCandidateSnapshot:
    """Named facts from the candidate captured when the request was created."""

    campaign_id: str
    manifest_sha256: str
    point_store_sha256: str
    campaign_path: str
    target_identity: str
    target_selector: str
    candidate_id: str
    point_id: str
    source: str
    source_sha256: str
    reason: str
    justification: str
    evidence_refs: tuple[str, ...]
    proof_reference: str
    proposal_count: int
    first_recorded_at: str
    last_recorded_at: str
    last_invocation_id: str

    def __post_init__(self) -> None:
        _require_type(self.evidence_refs, tuple, "waiver.candidate.evidence_refs")
        raw = self.to_json()
        for name, value in raw.items():
            if name not in {"evidence_refs", "proposal_count"}:
                _string(value, f"waiver.candidate.{name}", allow_empty=name == "proof_reference")
        _strings(raw["evidence_refs"], "waiver.candidate.evidence_refs")
        _positive(self.proposal_count, "waiver.candidate.proposal_count")

    @classmethod
    def from_json(cls, value: object) -> WaiverCandidateSnapshot:
        """Decode the historical candidate shape without importing its adapter."""
        raw = _object(value, "waiver.candidate", _CANDIDATE_FIELDS)
        return cls(
            raw["campaign_id"],
            raw["manifest_sha256"],
            raw["point_store_sha256"],
            raw["campaign_path"],
            raw["target_identity"],
            raw["target_selector"],
            raw["candidate_id"],
            raw["point_id"],
            raw["source"],
            raw["source_sha256"],
            raw["reason"],
            raw["justification"],
            _strings(raw["evidence_refs"], "waiver.candidate.evidence_refs"),
            raw["proof_reference"],
            raw["proposal_count"],
            raw["first_recorded_at"],
            raw["last_recorded_at"],
            raw["last_invocation_id"],
        )

    def to_json(self) -> dict[str, Any]:
        """Keep every persisted candidate field and its original value."""
        return {
            "campaign_id": self.campaign_id,
            "manifest_sha256": self.manifest_sha256,
            "point_store_sha256": self.point_store_sha256,
            "campaign_path": self.campaign_path,
            "target_identity": self.target_identity,
            "target_selector": self.target_selector,
            "candidate_id": self.candidate_id,
            "point_id": self.point_id,
            "source": self.source,
            "source_sha256": self.source_sha256,
            "reason": self.reason,
            "justification": self.justification,
            "evidence_refs": list(self.evidence_refs),
            "proof_reference": self.proof_reference,
            "proposal_count": self.proposal_count,
            "first_recorded_at": self.first_recorded_at,
            "last_recorded_at": self.last_recorded_at,
            "last_invocation_id": self.last_invocation_id,
        }


_CANDIDATE_FIELDS = frozenset(WaiverCandidateSnapshot.__dataclass_fields__)


def _campaign_reference(value: object) -> None:
    where = "waiver.campaign_reference"
    raw = _object(
        value, where, frozenset({"path_base", "path", "bytes", "sha256", "nested_campaign_sha256"})
    )
    for name in ("path_base", "path", "sha256", "nested_campaign_sha256"):
        _string(raw[name], f"{where}.{name}")
    _positive(raw["bytes"], f"{where}.bytes")


def _policy(value: object) -> None:
    where = "waiver.policy"
    raw = _object(value, where)
    for name in ("config", "root", "files", "digest"):
        if name not in raw:
            raise ProposalError(f"{where}.{name} is missing")
    config = _object(raw["config"], f"{where}.config", frozenset({"anchor", "directory"}))
    for name, item in config.items():
        _string(item, f"{where}.config.{name}")
    for name in ("root", "digest"):
        _string(raw[name], f"{where}.{name}")
    for name, item in _object(raw["files"], f"{where}.files").items():
        _string(item, f"{where}.files.{name}")


@dataclass(frozen=True)
class WaiverBinding:
    """The complete validated identity of one persisted waiver request."""

    candidate_id: str
    candidate: WaiverCandidateSnapshot
    candidate_digest: str
    goal_key: str
    campaign_reference: JsonSnapshot
    target_surface: JsonSnapshot | None
    policy: JsonSnapshot
    approval_root: str
    anchor_root: str
    anchor: str
    directory: str
    candidate_root: str
    record_id: str

    def __post_init__(self) -> None:
        _require_type(self.candidate, WaiverCandidateSnapshot, "waiver.candidate")
        for name, value in (
            ("campaign_reference", self.campaign_reference),
            ("policy", self.policy),
            ("target_surface", self.target_surface),
        ):
            if not isinstance(value, JsonSnapshot) and not (
                name == "target_surface" and value is None
            ):
                raise ProposalError(f"waiver.{name} must be an immutable JSON snapshot")
        _campaign_reference(self.campaign_reference.to_json())
        _policy(self.policy.to_json())
        for name, value in self.to_json().items():
            if name not in {"candidate", "campaign_reference", "target_surface", "policy"}:
                _string(value, f"waiver.{name}")

    @classmethod
    def from_json(cls, value: object) -> WaiverBinding:
        """Validate presence and primitive types before external storage is touched."""
        raw = _object(value, "waiver", _BINDING_FIELDS)
        return cls(
            raw["candidate_id"],
            WaiverCandidateSnapshot.from_json(raw["candidate"]),
            raw["candidate_digest"],
            raw["goal_key"],
            JsonSnapshot.from_json(raw["campaign_reference"], where="waiver.campaign_reference"),
            None
            if raw["target_surface"] is None
            else JsonSnapshot.from_json(raw["target_surface"], where="waiver.target_surface"),
            JsonSnapshot.from_json(raw["policy"], where="waiver.policy"),
            raw["approval_root"],
            raw["anchor_root"],
            raw["anchor"],
            raw["directory"],
            raw["candidate_root"],
            raw["record_id"],
        )

    def to_json(self) -> dict[str, Any]:
        """Preserve the v1 JSON shape and expose no mutable storage references."""
        return {
            "candidate_id": self.candidate_id,
            "candidate": self.candidate.to_json(),
            "candidate_digest": self.candidate_digest,
            "goal_key": self.goal_key,
            "campaign_reference": self.campaign_reference.to_json(),
            "target_surface": None
            if self.target_surface is None
            else self.target_surface.to_json(),
            "policy": self.policy.to_json(),
            "approval_root": self.approval_root,
            "anchor_root": self.anchor_root,
            "anchor": self.anchor,
            "directory": self.directory,
            "candidate_root": self.candidate_root,
            "record_id": self.record_id,
        }


_BINDING_FIELDS = frozenset(WaiverBinding.__dataclass_fields__)


def _require_type(value: object, expected: type[object], where: str) -> None:
    if not isinstance(value, expected):
        raise ProposalError(f"{where} must be {expected.__name__}")


def require_waiver_binding(value: object) -> None:
    """Refuse an unvalidated producer value before publishing a proposal."""
    _require_type(value, WaiverBinding, "waiver")
