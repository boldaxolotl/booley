"""Strict reference from a Simulation Campaign to its nested Coverage Campaign."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from booley.core.boundary import BoundaryError, require_int, require_str_value
from booley.flows.artifact_durability import durable_create
from booley.flows.sim.campaign_files import (
    canonical_json,
    parse_json_object,
    read_bounded,
)
from booley.flows.sim.coverage_campaign_store import (
    CAMPAIGN_SCHEMA,
    MAX_MANIFEST_BYTES,
    LoadedCoverageCampaign,
    load_coverage_campaign_bytes,
)
from booley.flows.sim.coverage_json import content_digest, freeze_coverage_mapping
from booley.flows.sim.record_layout import (
    COVERAGE_RECORD_DIRECTORY,
    RECORD_DIRECTORY,
    refuse_older_nested_path,
)
from booley.runtime.booley_dir import crossed_booley_dir_entry
from booley.runtime.sandbox_layout import canonical_project_alias_path

if TYPE_CHECKING:
    from booley.flows.sim.campaign import CampaignWorkItemEvidence

REFERENCE_SCHEMA = "booley.coverage-record-reference/v1"
MAX_REFERENCE_BYTES = 1024 * 1024
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_WORK_ITEM_RE = re.compile(r"^item:[0-9]{4}:[0-9a-f]{16}$")


class CoverageCampaignReferenceError(ValueError):
    """A Coverage Campaign reference is malformed or cannot be authenticated."""


@dataclass(frozen=True, slots=True)
class CoverageCampaignReference:
    """Immutable Coverage Campaign reference document.

    ``decode_coverage_campaign_reference`` validates bytes;
    ``build_coverage_campaign_reference`` constructs one from values Booley owns.
    """

    document: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ResolvedCoverageCampaign:
    """Authenticated nested Coverage Campaign selected by a public reference."""

    reference: CoverageCampaignReference
    reference_path: Path
    campaign_path: Path
    loaded: LoadedCoverageCampaign


def encode_coverage_campaign_reference(value: CoverageCampaignReference) -> bytes:
    """Encode one exact canonical reference."""
    return canonical_json(value.document)


def decode_coverage_campaign_reference(raw: bytes) -> CoverageCampaignReference:
    """Decode bounded canonical bytes and reject every schema contradiction."""
    if not raw or len(raw) > MAX_REFERENCE_BYTES:
        raise CoverageCampaignReferenceError("Coverage Campaign reference exceeds size limit")
    value = parse_json_object(raw, error=CoverageCampaignReferenceError, canonical=True)
    # Imported here: ``booley.flows.sim.campaign`` imports this module through its coordinator.
    from booley.flows.sim.campaign.codec import raise_if_retired_schema

    # An older Booley's reference is refused as old before its fields are checked.
    raise_if_retired_schema(value.get("$schema"))
    _validate_reference(value)
    return CoverageCampaignReference(freeze_coverage_mapping(value))


def publish_coverage_campaign_reference(
    path: Path, value: CoverageCampaignReference
) -> CoverageCampaignReference:
    """Create the immutable reference, or verify byte-identical prior publication."""
    raw = encode_coverage_campaign_reference(value)
    try:
        durable_create(path, raw)
    except FileExistsError:
        existing = read_bounded(
            path,
            limit=MAX_REFERENCE_BYTES,
            error=CoverageCampaignReferenceError,
            booley_dir=crossed_booley_dir_entry(path),
        )
        decoded = decode_coverage_campaign_reference(existing)
        if existing != raw:
            raise CoverageCampaignReferenceError(
                "existing Coverage Campaign reference conflicts with this attempt"
            ) from None
        return decoded
    return value


def resolve_coverage_campaign_reference(path: Path) -> ResolvedCoverageCampaign:
    """Load the reference and authenticate only its selected nested campaign."""
    try:
        absolute = canonical_project_alias_path(path.absolute())
    except ValueError as exc:
        raise CoverageCampaignReferenceError(str(exc)) from exc
    raw = read_bounded(
        absolute,
        limit=MAX_REFERENCE_BYTES,
        error=CoverageCampaignReferenceError,
        booley_dir=crossed_booley_dir_entry(absolute),
    )
    return _resolve_located(absolute, raw)


def _resolve_located(absolute: Path, raw: bytes) -> ResolvedCoverageCampaign:
    """Resolve the reference bytes; an older-Booley refusal names the directory to delete."""
    # Imported here: ``booley.flows.sim.campaign`` imports this module through its coordinator.
    from booley.flows.sim.campaign.codec import UnsupportedSimulationCampaignFormatError

    try:
        return _resolve_coverage_campaign_reference_bytes(absolute, raw)
    except UnsupportedSimulationCampaignFormatError as exc:
        # ``<invocation>/targets/<target>/coverage.json``: name the directory to delete.
        if absolute.parent.parent.name == "targets":
            raise exc.located(absolute.parents[2]) from None
        raise


def _resolve_coverage_campaign_reference_bytes(
    absolute: Path, raw: bytes
) -> ResolvedCoverageCampaign:
    """Resolve one already-authenticated public reference byte sequence."""
    reference = decode_coverage_campaign_reference(raw)
    document = reference.document
    nested = cast(Mapping[str, object], document["coverage_record"])
    target_root = absolute.parent
    campaign_path = _contained_relative(target_root, cast(str, nested["path"]))
    campaign_raw = read_bounded(
        campaign_path,
        limit=MAX_MANIFEST_BYTES,
        error=CoverageCampaignReferenceError,
        booley_dir=crossed_booley_dir_entry(campaign_path),
    )
    if len(campaign_raw) != nested["bytes"] or content_digest(campaign_raw) != nested["sha256"]:
        raise CoverageCampaignReferenceError(
            "nested Coverage Campaign bytes disagree with reference"
        )
    loaded = load_coverage_campaign_bytes(campaign_path, campaign_raw)
    campaign = loaded.campaign
    target = cast(Mapping[str, str], document["target"])
    if (
        campaign.campaign_id != nested["coverage_record_id"]
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
        or parts[:2] != (RECORD_DIRECTORY, "work-items")
        or parts[2] != work_key
        or parts[3] != "attempts"
        or not re.fullmatch(rf"[0-9]{{4}}-{re.escape(attempt_id)}", parts[4])
        or parts[5:] != (COVERAGE_RECORD_DIRECTORY, "coverage.json")
    ):
        raise CoverageCampaignReferenceError(
            "nested Coverage Campaign path disagrees with Simulation Attempt identity"
        )
    return ResolvedCoverageCampaign(reference, absolute, campaign_path, loaded)


def resolve_persisted_coverage_campaign_reference(
    reports_root: Path, value: object
) -> ResolvedCoverageCampaign:
    """Authenticate an acceptance-state pointer and its selected nested Campaign."""
    try:
        reports_root = canonical_project_alias_path(reports_root.absolute())
    except ValueError as exc:
        raise CoverageCampaignReferenceError(str(exc)) from exc
    persisted = _exact(
        value,
        {"path_base", "path", "bytes", "sha256", "nested_campaign_sha256"},
        "persisted coverage_campaign_reference",
    )
    if persisted["path_base"] != "reports_root":
        raise CoverageCampaignReferenceError(
            "persisted coverage_campaign_reference path_base is invalid"
        )
    relative = _relative_path(persisted["path"])
    try:
        expected_bytes = require_int(persisted["bytes"], field="coverage_campaign_reference.bytes")
    except BoundaryError as exc:
        raise CoverageCampaignReferenceError(
            "persisted coverage_campaign_reference bytes is invalid"
        ) from exc
    if expected_bytes < 1 or expected_bytes > MAX_REFERENCE_BYTES:
        raise CoverageCampaignReferenceError(
            "persisted coverage_campaign_reference bytes is invalid"
        )
    _digest(persisted["sha256"], "coverage_campaign_reference.sha256")
    _digest(
        persisted["nested_campaign_sha256"],
        "coverage_campaign_reference.nested_campaign_sha256",
    )
    public_path = _contained_relative(reports_root.absolute(), relative)
    raw = read_bounded(
        public_path,
        limit=MAX_REFERENCE_BYTES,
        error=CoverageCampaignReferenceError,
        booley_dir=crossed_booley_dir_entry(public_path),
    )
    if len(raw) != expected_bytes or content_digest(raw) != persisted["sha256"]:
        raise CoverageCampaignReferenceError(
            "persisted Coverage Campaign reference bytes disagree with acceptance state"
        )
    resolved = _resolve_located(public_path, raw)
    nested = cast(Mapping[str, object], resolved.reference.document["coverage_record"])
    if nested["sha256"] != persisted["nested_campaign_sha256"]:
        raise CoverageCampaignReferenceError(
            "persisted nested Coverage Campaign digest disagrees with public reference"
        )
    return resolved


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
    raw = read_bounded(
        coverage_campaign_path,
        limit=MAX_MANIFEST_BYTES,
        error=CoverageCampaignReferenceError,
        booley_dir=crossed_booley_dir_entry(coverage_campaign_path),
    )
    loaded = load_coverage_campaign_bytes(coverage_campaign_path, raw)
    relative = coverage_campaign_path.relative_to(origin_target_directory).as_posix()
    return CoverageCampaignReference(
        freeze_coverage_mapping(
            {
                "$schema": REFERENCE_SCHEMA,
                "simulation_record_id": simulation_campaign_id,
                "simulation_manifest_sha256": simulation_manifest_sha256,
                "target": {"identity": target_identity, "selector": target_selector},
                "origin_invocation_id": origin_invocation_id,
                "producer_invocation_id": producer_invocation_id,
                "simulation_work_item_id": simulation_work_item_id,
                "simulation_attempt_id": simulation_attempt_id,
                "coverage_record": {
                    "path_base": "origin_target",
                    "path": relative,
                    "bytes": len(raw),
                    "sha256": content_digest(raw),
                    "coverage_record_id": loaded.campaign.campaign_id,
                    "schema": loaded.summary.source_schema,
                },
            }
        )
    )


def authenticate_coverage_campaign_owner(
    resolved: ResolvedCoverageCampaign,
) -> CampaignWorkItemEvidence:
    """Authenticate the enclosing Simulation manifest, result, and nested evidence."""
    from booley.flows.sim.campaign import (
        SimulationCampaignIntegrityError,
        SimulationCampaignWorkItemError,
        authenticate_work_item,
    )

    document = resolved.reference.document
    nested = cast(Mapping[str, object], document["coverage_record"])
    manifest_path = resolved.reference_path.parent / RECORD_DIRECTORY / "manifest.json"
    try:
        authenticated = authenticate_work_item(
            manifest_path,
            cast(str, document["simulation_work_item_id"]),
            booley_dir=crossed_booley_dir_entry(manifest_path),
        )
    except SimulationCampaignWorkItemError as exc:
        raise CoverageCampaignReferenceError(
            "reference has no exact enclosing Simulation result"
        ) from exc
    except SimulationCampaignIntegrityError as exc:
        raise CoverageCampaignReferenceError(
            "enclosing Simulation Campaign cannot be authenticated"
        ) from exc
    manifest = authenticated.manifest
    target = cast(Mapping[str, str], manifest.document["target"])
    result = authenticated.result
    evidence = cast(tuple[Mapping[str, object], ...], result.document["evidence"])
    try:
        # The result decoder checks only the document boundary (#1744).
        nested_evidence = [
            (item["bytes"], item["sha256"], Path(cast(str, item["path"])).parts)
            for item in evidence
            if item["kind"] == "coverage_record_manifest"
        ]
    except (KeyError, TypeError) as exc:
        raise CoverageCampaignReferenceError(
            "enclosing Simulation result evidence is malformed"
        ) from exc
    if (
        manifest.document["simulation_record_id"] != document["simulation_record_id"]
        or authenticated.manifest_sha256 != document["simulation_manifest_sha256"]
        or target["selector"] != cast(Mapping[str, str], document["target"])["selector"]
        or f"{target['vlnv']}#{target['name']}"
        != cast(Mapping[str, str], document["target"])["identity"]
        or result.document["attempt_id"] != document["simulation_attempt_id"]
        or result.document["producer_invocation_id"] != document["producer_invocation_id"]
        or nested_evidence
        != [(nested["bytes"], nested["sha256"], Path(cast(str, nested["path"])).parts[5:])]
    ):
        raise CoverageCampaignReferenceError(
            "enclosing Simulation Campaign disagrees with reference"
        )
    return authenticated


def _validate_reference(value: Mapping[str, object]) -> None:
    _exact(
        value,
        {
            "$schema",
            "simulation_record_id",
            "simulation_manifest_sha256",
            "target",
            "origin_invocation_id",
            "producer_invocation_id",
            "simulation_work_item_id",
            "simulation_attempt_id",
            "coverage_record",
        },
        "reference",
    )
    if value["$schema"] != REFERENCE_SCHEMA:
        raise CoverageCampaignReferenceError("unsupported Coverage Campaign reference schema")
    _uuid(value["simulation_record_id"], "simulation_record_id")
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
            value["coverage_record"],
            {"path_base", "path", "bytes", "sha256", "coverage_record_id", "schema"},
            "coverage_record",
        )
    )


def _validate_nested_reference(nested: Mapping[str, object]) -> None:
    # Imported here: ``booley.flows.sim.campaign`` imports this module through its coordinator.
    from booley.flows.sim.campaign.codec import raise_if_retired_schema

    raise_if_retired_schema(nested["schema"])
    if nested["path_base"] != "origin_target" or nested["schema"] != CAMPAIGN_SCHEMA:
        raise CoverageCampaignReferenceError("coverage_record storage contract is invalid")
    nested_path = _relative_path(nested["path"])
    refuse_older_nested_path(nested_path)
    nested_parts = Path(nested_path).parts
    if (
        len(nested_parts) != 7
        or nested_parts[:2] != (RECORD_DIRECTORY, "work-items")
        or nested_parts[3] != "attempts"
        or nested_parts[5:] != (COVERAGE_RECORD_DIRECTORY, "coverage.json")
    ):
        raise CoverageCampaignReferenceError(
            "coverage_record.path is not an attempt-scoped Coverage Record path"
        )
    try:
        size = require_int(nested["bytes"], field="coverage_record.bytes")
    except BoundaryError as exc:
        raise CoverageCampaignReferenceError("coverage_record.bytes is invalid") from exc
    if size < 1 or size > MAX_MANIFEST_BYTES:
        raise CoverageCampaignReferenceError("coverage_record.bytes is invalid")
    _digest(nested["sha256"], "coverage_record.sha256")
    _string(nested["coverage_record_id"], "coverage_record.coverage_record_id")


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
    text = _string(value, "coverage_record.path")
    path = Path(text)
    if (
        "\\" in text
        or "\x00" in text
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CoverageCampaignReferenceError("coverage_record.path is not normalized")
    return text


def _contained_relative(root: Path, relative: str) -> Path:
    path = root / _relative_path(relative)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise CoverageCampaignReferenceError("coverage_record.path escapes origin Target") from exc
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise CoverageCampaignReferenceError(
                "Coverage Campaign reference path contains a link"
            )
    return path


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
    "resolve_persisted_coverage_campaign_reference",
]
