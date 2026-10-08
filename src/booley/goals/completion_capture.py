"""Private materialization bytes and their exact public hash/provenance projection."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from booley.core.boundary import require_dict, require_list, require_str_value
from booley.goals.proposals import digest


def public_capture(private: dict[str, Any]) -> dict[str, Any]:
    """Only classified materialization buffers are private; producer facts stay exact."""
    proof = copy.deepcopy(private)
    proof["nonversioned_observations"] = [
        public_observation(row) for row in private["nonversioned_observations"]
    ]
    snapshot = private.get("original_project_snapshot")
    proof["original_project_snapshot"] = (
        None
        if snapshot is None
        else {
            name: None if value is None else byte_proof(value) for name, value in snapshot.items()
        }
    )
    proof["private_capture_sha256"] = "sha256:" + digest(private)
    return proof


def public_observation(raw: dict[str, Any]) -> dict[str, Any]:
    row = copy.deepcopy(raw)
    if "bytes" in row:
        row["materialization"] = byte_proof(row.pop("bytes"))
    provenance = row.get("provenance")
    if provenance is not None and "producer_manifest_bytes" in provenance:
        provenance["producer_manifest_capture"] = byte_proof(
            provenance.pop("producer_manifest_bytes")
        )
    return row


def byte_proof(raw: str) -> dict[str, Any]:
    content = bytes.fromhex(raw)
    return {"sha256": "sha256:" + hashlib.sha256(content).hexdigest(), "size": len(content)}


def legacy_materialization_buffers(frozen: dict[str, Any]) -> bool:
    """Historical terminal bytes remain immutable; unpublished private captures cannot publish."""
    if frozen["schema"] != "booley.goal-finish-attempt/v1":
        return False
    package = frozen["package"]
    proof = package["input_proof"]
    return (
        proof.get("original_project_snapshot") is not None
        or package["record"].get("project_snapshot") is not None
        or any(
            "bytes" in row or "producer_manifest_bytes" in (row.get("provenance") or {})
            for row in proof.get("nonversioned_observations", [])
        )
    )


def validate_capture(raw: object, public: dict[str, Any]) -> dict[str, Any]:
    """Decode and hash every classified byte buffer before trusting its projection."""
    private = require_dict(raw, field="private completion capture")
    for raw_row in require_list(private.get("nonversioned_observations"), field="observations"):
        row = require_dict(raw_row, field="observation")
        require_str_value(row.get("path"), field="observation path")
        require_str_value(row.get("classification"), field="observation classification")
        if "bytes" in row:
            actual = byte_proof(
                require_str_value(row["bytes"], field="captured bytes", allow_empty=True)
            )
            if actual["sha256"].removeprefix("sha256:") != row.get("sha256"):
                raise ValueError("captured materialization bytes differ from their hash")
        provenance = row.get("provenance")
        if provenance is not None:
            byte_proof(
                require_str_value(
                    require_dict(provenance, field="provenance")["producer_manifest_bytes"],
                    field="producer manifest bytes",
                    allow_empty=True,
                )
            )
    if public_capture(private) != public:
        raise ValueError("public input proof differs from its private materialization authority")
    return private
