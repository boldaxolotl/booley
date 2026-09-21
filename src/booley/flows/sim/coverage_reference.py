"""Strict reference from a Simulation Campaign to its nested Coverage Campaign."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

from booley.core.boundary import BoundaryError, require_dict, require_int, require_str_value
from booley.flows.sim.campaign_durability import durable_create
from booley.flows.sim.coverage_campaign_store import (
    CAMPAIGN_SCHEMA_V3,
    MAX_MANIFEST_BYTES,
    LoadedCoverageCampaign,
    load_coverage_campaign_bytes,
)
from booley.runtime.regular_file import open_regular_nofollow

REFERENCE_SCHEMA = "booley.coverage-campaign-reference/v1"
MAX_REFERENCE_BYTES = 1024 * 1024
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_WORK_ITEM_RE = re.compile(r"^item:[0-9]{4}:[0-9a-f]{16}$")


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


class CoverageCampaignReferenceError(ValueError):
    """A Coverage Campaign reference is malformed or cannot be authenticated."""


@dataclass(frozen=True, slots=True)
class CoverageCampaignReference:
    """Validated immutable Coverage Campaign reference document."""

    document: Mapping[str, object]

    def __post_init__(self) -> None:
        decoded = decode_coverage_campaign_reference(_canonical_json_bytes(self.document))
        object.__setattr__(self, "document", decoded.document)


@dataclass(frozen=True, slots=True)
class ResolvedCoverageCampaign:
    """Authenticated nested Coverage Campaign selected by a public reference."""

    reference: CoverageCampaignReference
    reference_path: Path
    campaign_path: Path
    loaded: LoadedCoverageCampaign


def encode_coverage_campaign_reference(value: CoverageCampaignReference) -> bytes:
    """Encode one exact canonical reference."""
    if type(value) is not CoverageCampaignReference:
        raise CoverageCampaignReferenceError("expected CoverageCampaignReference")
    raw = _canonical_json_bytes(value.document)
    decode_coverage_campaign_reference(raw)
    return raw


def decode_coverage_campaign_reference(raw: bytes) -> CoverageCampaignReference:
    """Decode bounded canonical bytes and reject every schema contradiction."""
    if not raw or len(raw) > MAX_REFERENCE_BYTES:
        raise CoverageCampaignReferenceError("Coverage Campaign reference exceeds size limit")
    try:
        value = require_dict(json.loads(raw))
    except (UnicodeDecodeError, json.JSONDecodeError, BoundaryError) as exc:
        raise CoverageCampaignReferenceError("Coverage Campaign reference is not valid JSON") from exc
    if _canonical_json_bytes(value) != raw:
        raise CoverageCampaignReferenceError("Coverage Campaign reference is not canonical JSON")
    _validate_reference(value)
    instance = object.__new__(CoverageCampaignReference)
    object.__setattr__(instance, "document", _freeze(value))
    return instance


def publish_coverage_campaign_reference(
    path: Path, value: CoverageCampaignReference
) -> CoverageCampaignReference:
    """Create the immutable reference, or verify byte-identical prior publication."""
    raw = encode_coverage_campaign_reference(value)
    try:
        durable_create(path, raw)
    except FileExistsError:
        descriptor = open_regular_nofollow(path)
        with os.fdopen(descriptor, "rb") as stream:
            existing = stream.read(MAX_REFERENCE_BYTES + 1)
        decoded = decode_coverage_campaign_reference(existing)
        if existing != raw:
            raise CoverageCampaignReferenceError(
                "existing Coverage Campaign reference conflicts with this attempt"
            ) from None
        return decoded
    return value


def resolve_coverage_campaign_reference(path: Path) -> ResolvedCoverageCampaign:
    """Load the reference and authenticate only its selected nested campaign."""
    absolute = path.absolute()
    raw = _read_regular(absolute, "Coverage Campaign reference", MAX_REFERENCE_BYTES)
    reference = decode_coverage_campaign_reference(raw)
    document = reference.document
    nested = cast(Mapping[str, object], document["coverage_campaign"])
    target_root = absolute.parent
    campaign_path = _contained_relative(target_root, cast(str, nested["path"]))
    campaign_raw = _read_regular(
        campaign_path, "nested Coverage Campaign", MAX_MANIFEST_BYTES
    )
    if len(campaign_raw) != nested["bytes"] or _digest_bytes(campaign_raw) != nested["sha256"]:
        raise CoverageCampaignReferenceError("nested Coverage Campaign bytes disagree with reference")
    loaded = load_coverage_campaign_bytes(campaign_path, campaign_raw)
    campaign = loaded.campaign
    target = cast(Mapping[str, str], document["target"])
    if (
        campaign.campaign_id != nested["campaign_id"]
        or campaign.target.identity != target["identity"]
        or campaign.target.selector != target["selector"]
        or campaign.invocation["id"] != document["origin_invocation_id"]
        or loaded.summary.source_schema != nested["schema"]
    ):
        raise CoverageCampaignReferenceError(
            "nested Coverage Campaign identity disagrees with reference"
        )
    work_item_id = cast(str, document["simulation_work_item_id"])
    _, ordinal_text, work_digest = work_item_id.split(":")
    work_key = f"{int(ordinal_text) + 1:04d}-{work_digest}"
    attempt_id = cast(str, document["simulation_attempt_id"])
    parts = Path(cast(str, nested["path"])).parts
    if (
        len(parts) != 7
        or parts[:2] != ("campaign", "work-items")
        or parts[2] != work_key
        or parts[3] != "attempts"
        or not re.fullmatch(rf"[0-9]{{4}}-{re.escape(attempt_id)}", parts[4])
        or parts[5:] != ("coverage-campaign", "coverage.json")
    ):
        raise CoverageCampaignReferenceError(
            "nested Coverage Campaign path disagrees with Simulation Attempt identity"
        )
    return ResolvedCoverageCampaign(reference, absolute, campaign_path, loaded)


def build_coverage_campaign_reference(
    *,
    simulation_campaign_id: str,
    simulation_manifest_sha256: str,
    target_identity: str,
    target_selector: str,
    origin_invocation_id: int,
    producer_invocation_id: int,
    simulation_work_item_id: str,
    simulation_attempt_id: str,
    origin_target_directory: Path,
    coverage_campaign_path: Path,
) -> CoverageCampaignReference:
    """Construct a reference from an already committed nested Coverage Campaign."""
    raw = _read_regular(
        coverage_campaign_path, "nested Coverage Campaign", MAX_MANIFEST_BYTES
    )
    loaded = load_coverage_campaign_bytes(coverage_campaign_path, raw)
    relative = coverage_campaign_path.relative_to(origin_target_directory).as_posix()
    return CoverageCampaignReference(
        {
            "$schema": REFERENCE_SCHEMA,
            "simulation_campaign_id": simulation_campaign_id,
            "simulation_manifest_sha256": simulation_manifest_sha256,
            "target": {"identity": target_identity, "selector": target_selector},
            "origin_invocation_id": origin_invocation_id,
            "producer_invocation_id": producer_invocation_id,
            "simulation_work_item_id": simulation_work_item_id,
            "simulation_attempt_id": simulation_attempt_id,
            "coverage_campaign": {
                "path_base": "origin_target",
                "path": relative,
                "bytes": len(raw),
                "sha256": _digest_bytes(raw),
                "campaign_id": loaded.campaign.campaign_id,
                "schema": loaded.summary.source_schema,
            },
        }
    )


def authenticate_coverage_campaign_owner(
    resolved: ResolvedCoverageCampaign,
) -> None:
    """Authenticate the enclosing Simulation manifest, result, and nested evidence."""
    from booley.flows.sim.campaign.planning import manifest_digest
    from booley.flows.sim.campaign.store import CampaignStore

    document = resolved.reference.document
    nested = cast(Mapping[str, object], document["coverage_campaign"])
    store = CampaignStore(resolved.reference_path.parent / "campaign")
    try:
        manifest = store.load_manifest()
        recovery = store.scan()
    except (OSError, ValueError) as exc:
        raise CoverageCampaignReferenceError(
            "enclosing Simulation Campaign cannot be authenticated"
        ) from exc
    target = cast(Mapping[str, str], manifest.document["target"])
    matching = [
        item
        for item in recovery.items
        if item.work_item_id == document["simulation_work_item_id"]
        and item.result is not None
    ]
    if len(matching) != 1:
        raise CoverageCampaignReferenceError(
            "reference has no exact enclosing Simulation result"
        )
    result = matching[0].result
    assert result is not None
    evidence = cast(tuple[Mapping[str, object], ...], result.document["evidence"])
    nested_evidence = [item for item in evidence if item["kind"] == "coverage_campaign_manifest"]
    if (
        manifest.document["campaign_id"] != document["simulation_campaign_id"]
        or manifest_digest(manifest) != document["simulation_manifest_sha256"]
        or target["selector"] != cast(Mapping[str, str], document["target"])["selector"]
        or f"{target['vlnv']}#{target['name']}"
        != cast(Mapping[str, str], document["target"])["identity"]
        or result.document["attempt_id"] != document["simulation_attempt_id"]
        or result.document["producer_invocation_id"] != document["producer_invocation_id"]
        or len(nested_evidence) != 1
        or nested_evidence[0]["bytes"] != nested["bytes"]
        or nested_evidence[0]["sha256"] != nested["sha256"]
        or Path(cast(str, nested_evidence[0]["path"])).parts
        != Path(cast(str, nested["path"])).parts[5:]
    ):
        raise CoverageCampaignReferenceError(
            "enclosing Simulation Campaign disagrees with reference"
        )


def _validate_reference(value: Mapping[str, object]) -> None:
    _exact(
        value,
        {
            "$schema", "simulation_campaign_id", "simulation_manifest_sha256", "target",
            "origin_invocation_id", "producer_invocation_id", "simulation_work_item_id",
            "simulation_attempt_id", "coverage_campaign",
        },
        "reference",
    )
    if value["$schema"] != REFERENCE_SCHEMA:
        raise CoverageCampaignReferenceError("unsupported Coverage Campaign reference schema")
    _uuid(value["simulation_campaign_id"], "simulation_campaign_id")
    _uuid(value["simulation_attempt_id"], "simulation_attempt_id")
    _digest(value["simulation_manifest_sha256"], "simulation_manifest_sha256")
    for field in ("origin_invocation_id", "producer_invocation_id"):
        try:
            parsed = require_int(value[field], field=field)
        except BoundaryError as exc:
            raise CoverageCampaignReferenceError(f"{field} must be a positive integer") from exc
        if parsed < 1:
            raise CoverageCampaignReferenceError(f"{field} must be a positive integer")
    work_item = _string(value["simulation_work_item_id"], "simulation_work_item_id")
    if not _WORK_ITEM_RE.fullmatch(work_item):
        raise CoverageCampaignReferenceError("simulation_work_item_id is invalid")
    target = _exact(value["target"], {"identity", "selector"}, "target")
    _string(target["identity"], "target.identity")
    _string(target["selector"], "target.selector")
    _validate_nested_reference(
        _exact(
        value["coverage_campaign"],
        {"path_base", "path", "bytes", "sha256", "campaign_id", "schema"},
        "coverage_campaign",
        )
    )


def _validate_nested_reference(nested: Mapping[str, object]) -> None:
    if nested["path_base"] != "origin_target" or nested["schema"] != CAMPAIGN_SCHEMA_V3:
        raise CoverageCampaignReferenceError("coverage_campaign storage contract is invalid")
    nested_path = _relative_path(nested["path"])
    nested_parts = Path(nested_path).parts
    if (
        len(nested_parts) != 7
        or nested_parts[:2] != ("campaign", "work-items")
        or nested_parts[3] != "attempts"
        or nested_parts[5:] != ("coverage-campaign", "coverage.json")
    ):
        raise CoverageCampaignReferenceError(
            "coverage_campaign.path is not an attempt-scoped Campaign path"
        )
    try:
        size = require_int(nested["bytes"], field="coverage_campaign.bytes")
    except BoundaryError as exc:
        raise CoverageCampaignReferenceError("coverage_campaign.bytes is invalid") from exc
    if size < 1 or size > MAX_MANIFEST_BYTES:
        raise CoverageCampaignReferenceError("coverage_campaign.bytes is invalid")
    _digest(nested["sha256"], "coverage_campaign.sha256")
    _string(nested["campaign_id"], "coverage_campaign.campaign_id")


def _exact(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CoverageCampaignReferenceError(f"{label} must have exact fields {sorted(fields)}")
    return value


def _string(value: object, field: str) -> str:
    try:
        result = require_str_value(value, field=field)
    except BoundaryError as exc:
        raise CoverageCampaignReferenceError(f"{field} must be a nonempty string") from exc
    if len(result.encode()) > 4096:
        raise CoverageCampaignReferenceError(f"{field} exceeds size limit")
    return result


def _uuid(value: object, field: str) -> None:
    text = _string(value, field)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise CoverageCampaignReferenceError(f"{field} must be lowercase UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != text:
        raise CoverageCampaignReferenceError(f"{field} must be lowercase UUIDv4")


def _digest(value: object, field: str) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise CoverageCampaignReferenceError(f"{field} must be a sha256 digest")


def _relative_path(value: object) -> str:
    text = _string(value, "coverage_campaign.path")
    path = Path(text)
    if (
        "\\" in text
        or "\x00" in text
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CoverageCampaignReferenceError("coverage_campaign.path is not normalized")
    return text


def _contained_relative(root: Path, relative: str) -> Path:
    path = root / _relative_path(relative)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise CoverageCampaignReferenceError("coverage_campaign.path escapes origin Target") from exc
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise CoverageCampaignReferenceError("Coverage Campaign reference path contains a link")
    return path


def _read_regular(path: Path, label: str, maximum: int) -> bytes:
    try:
        descriptor = open_regular_nofollow(path)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError as exc:
        raise CoverageCampaignReferenceError(f"{label} is not a regular file: {path}") from exc
    if len(raw) > maximum:
        raise CoverageCampaignReferenceError(f"{label} exceeds size limit")
    return raw


def _digest_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "REFERENCE_SCHEMA",
    "CoverageCampaignReference",
    "CoverageCampaignReferenceError",
    "ResolvedCoverageCampaign",
    "authenticate_coverage_campaign_owner",
    "build_coverage_campaign_reference",
    "decode_coverage_campaign_reference",
    "encode_coverage_campaign_reference",
    "publish_coverage_campaign_reference",
    "resolve_coverage_campaign_reference",
]
