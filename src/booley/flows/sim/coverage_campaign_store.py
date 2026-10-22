"""Durable V5 Coverage Campaign persistence behind one filesystem seam."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_int,
    require_str,
)
from booley.runtime.atomic_files import atomic_write_once
from booley.runtime.regular_file import open_regular_nofollow
from booley.runtime.sandbox_layout import canonical_project_alias_path

from .campaign_reports import is_report_link
from .coverage_campaign import (
    CoverageCampaign,
    CoverageCampaignValidationError,
    CoverageRollup,
    DurableTargetIdentity,
    FrozenJson,
    _decode_owned_coverage_campaign,
    derive_coverage_source_rollups,
    encode_coverage_campaign,
    encode_coverage_point,
    freeze_coverage_mapping,
)

CAMPAIGN_SCHEMA_V1 = "booley.coverage-campaign/v1"
CAMPAIGN_SCHEMA_V4 = "booley.coverage-campaign/v4"
#: v5: every run names its Test Run and declared-inputs fingerprint (ADR 0072),
#: and one selected Test may have several runs. Booley writes only v5.
CAMPAIGN_SCHEMA_V5 = "booley.coverage-campaign/v5"
#: Every manifest version Booley reads. v4 (written by v0.3.0) stays readable;
#: its runs name a bare Test.
POINT_STORE_CAMPAIGN_SCHEMAS = frozenset({CAMPAIGN_SCHEMA_V4, CAMPAIGN_SCHEMA_V5})
POINT_STORE_SCHEMA_V1 = "booley.coverage-points/v1"
POINT_STORE_NAME = "coverage-points.jsonl.gz"
#: The one resource ceiling on a point store: its decompressed size, on write and read.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_SHA256_PREFIX = "sha256:"
_MANIFEST_FIELDS = frozenset(
    {
        "$schema",
        "campaign_id",
        "invocation",
        "target",
        "collector",
        "build",
        "coverage_window",
        "fingerprints",
        "source_closure",
        "tests",
        "artifacts",
        "normalization",
        "point_store",
        "rollups",
        "source_rollups",
        "scoring",
        "collection",
        "findings",
        "evaluation",
    }
)
#: Manifest keys the codec does not read; the store consumes or republishes them.
_STORE_FIELDS = frozenset({"point_store", "source_rollups", "scoring"})


@dataclass(frozen=True)
class CoveragePointStoreReference:
    """The point-store identity the writer recorded; readers do not re-check it."""

    path: str
    sha256: str
    bytes: int
    uncompressed_bytes: int
    point_count: int


@dataclass(frozen=True)
class CoverageCampaignSummary:
    """The persisted envelope callers bind to; Campaign facts come from the codec."""

    source_schema: str
    document: Mapping[str, FrozenJson]
    manifest_sha256: str
    point_store: CoveragePointStoreReference | None


@dataclass(frozen=True)
class LoadedCoverageCampaign:
    """A complete Campaign together with its validated persisted envelope."""

    summary: CoverageCampaignSummary
    campaign: CoverageCampaign


@dataclass(frozen=True)
class CampaignPaths:
    """Canonical files published for one current Campaign."""

    campaign: Path
    points: Path


class CoverageCampaignStoreError(ValueError):
    """Campaign persistence cannot produce or recover exact evidence."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if message is None:
            message, code = code, "COV_STORE_OPERATION"
        self.code = code
        super().__init__(f"{code}: {message}")


def _canonical_line(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_line(stream: gzip.GzipFile, value: object, total: int) -> int:
    line = _canonical_line(value)
    total += len(line)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise CoverageCampaignStoreError(
            "COV_POINT_LIMIT", "Coverage Point storage exceeds its ceiling"
        )
    stream.write(line)
    return total


def _point_store_bytes(
    campaign: CoverageCampaign,
) -> tuple[bytes, CoveragePointStoreReference]:
    """Compress the point store in memory and describe it for the manifest."""
    buffer = io.BytesIO()
    uncompressed_bytes = 0
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
        header = {
            "$schema": POINT_STORE_SCHEMA_V1,
            "campaign_id": campaign.campaign_id,
            "target_identity": campaign.target.identity,
        }
        uncompressed_bytes = _write_line(compressed, header, uncompressed_bytes)
        for point in campaign.points:
            uncompressed_bytes = _write_line(
                compressed, encode_coverage_point(point), uncompressed_bytes
            )
    data = buffer.getvalue()
    return data, CoveragePointStoreReference(
        path=POINT_STORE_NAME,
        sha256=f"{_SHA256_PREFIX}{hashlib.sha256(data).hexdigest()}",
        bytes=len(data),
        uncompressed_bytes=uncompressed_bytes,
        point_count=len(campaign.points),
    )


def _point_store_document(reference: CoveragePointStoreReference) -> dict[str, object]:
    return {
        "$schema": POINT_STORE_SCHEMA_V1,
        "path": reference.path,
        "encoding": "jsonl+gzip",
        "sha256": reference.sha256,
        "bytes": reference.bytes,
        "uncompressed_bytes": reference.uncompressed_bytes,
        "point_count": reference.point_count,
    }


def _manifest(
    campaign: CoverageCampaign, reference: CoveragePointStoreReference
) -> dict[str, object]:
    document = encode_coverage_campaign(campaign)
    document["$schema"] = CAMPAIGN_SCHEMA_V5
    del document["points"]
    document["point_store"] = _point_store_document(reference)
    scoring = _expected_scoring(campaign.collection)
    document["scoring"] = scoring
    document["source_rollups"] = [
        {
            "source": item.source,
            "rollups": [_rollup_document(rollup) for rollup in item.rollups],
        }
        for item in (
            derive_coverage_source_rollups(campaign.points) if scoring["status"] == "valid" else ()
        )
    ]
    return document


def _rollup_document(rollup: CoverageRollup) -> dict[str, object]:
    return {
        "metric": rollup.metric,
        "semantics": rollup.semantics,
        "total_points": rollup.total_points,
        "eligible_points": rollup.eligible_points,
        "covered_points": rollup.covered_points,
        "waived_points": rollup.waived_points,
        "percent": rollup.percent,
    }


def _manifest_bytes(document: Mapping[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"


def publish_coverage_campaign(target_dir: Path, campaign: CoverageCampaign) -> CampaignPaths:
    """Publish a complete V5 Campaign without replacing existing final files."""
    if _contains_link(target_dir):
        raise CoverageCampaignStoreError("Coverage Campaign path contains a link")
    target_dir.mkdir(parents=True, exist_ok=True)
    campaign_path = target_dir / "coverage.json"
    points_path = target_dir / POINT_STORE_NAME
    if campaign_path.exists() or points_path.exists():
        raise CoverageCampaignStoreError("Coverage Campaign publication cannot replace evidence")
    points_bytes, reference = _point_store_bytes(campaign)
    manifest_bytes = _manifest_bytes(_manifest(campaign, reference))
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise CoverageCampaignStoreError(
            "COV_MANIFEST_LIMIT", "Coverage Campaign manifest exceeds its size limit"
        )
    atomic_write_once(points_path, points_bytes)
    # The manifest is the commit marker: link it last, and drop the point store
    # this call wrote when the manifest never appears.
    try:
        atomic_write_once(campaign_path, manifest_bytes)
    finally:
        if not campaign_path.exists():
            points_path.unlink(missing_ok=True)
    return CampaignPaths(campaign_path, points_path)


def _decode_reference(value: object) -> CoveragePointStoreReference:
    """Decode the recorded identity; readers trust the published pair (ADR 0062)."""
    record = require_dict(value, field="point_store")
    return CoveragePointStoreReference(
        POINT_STORE_NAME,
        require_str(record, "sha256"),
        require_int(record.get("bytes"), field="bytes"),
        require_int(record.get("uncompressed_bytes"), field="uncompressed_bytes"),
        require_int(record.get("point_count"), field="point_count"),
    )


def _expected_scoring(collection: Mapping[str, object]) -> dict[str, object]:
    status = collection.get("status")
    if status == "complete":
        return {"status": "valid", "reason": None}
    return {"status": "invalid", "reason": status}


def _contains_link(path: Path) -> bool:
    return any(is_report_link(item) for item in (path, *path.parents))


def _open_regular(path: Path) -> int:
    try:
        return open_regular_nofollow(path)
    except OSError as exc:
        raise CoverageCampaignStoreError(
            "COV_PATH_UNSAFE", f"Cannot open retained regular file: {path}"
        ) from exc


def _read_manifest_bytes(path: Path) -> bytes:
    """Read at most one byte past the manifest ceiling through a no-follow regular open."""
    with os.fdopen(_open_regular(path), "rb") as stream:
        return stream.read(MAX_MANIFEST_BYTES + 1)


def _canonical_campaign_path(path: Path) -> Path:
    try:
        return canonical_project_alias_path(path.absolute())
    except ValueError as exc:
        raise CoverageCampaignStoreError("COV_PATH_UNSAFE", str(exc)) from exc


def _point_documents(path: Path) -> list[dict[str, object]]:
    """Gunzip the point store once under the ceiling; return its rows after the header."""
    try:
        with (
            os.fdopen(_open_regular(path), "rb") as source,
            gzip.GzipFile(fileobj=source, mode="rb") as stream,
        ):
            # Chunked: one read(ceiling + 1) would reserve the whole ceiling up front.
            raw = bytearray()
            while chunk := stream.read(1 << 20):
                raw += chunk
                if len(raw) > MAX_UNCOMPRESSED_BYTES:
                    raise CoverageCampaignStoreError(
                        "COV_POINT_LIMIT", "Coverage Point storage exceeds its ceiling"
                    )
    except (EOFError, OSError, zlib.error) as exc:
        raise CoverageCampaignStoreError(
            "COV_POINT_FORMAT", "Malformed gzip Coverage Point storage"
        ) from exc
    try:
        header, *rows = (dict(require_dict(json.loads(line))) for line in raw.splitlines())
    except (ValueError, BoundaryError) as exc:
        raise CoverageCampaignStoreError(
            "COV_POINT_FORMAT", "Malformed Coverage Point JSON line"
        ) from exc
    if header.get("$schema") != POINT_STORE_SCHEMA_V1:
        raise CoverageCampaignStoreError(
            "COV_POINT_FORMAT", "Coverage Point storage header is not point-store v1"
        )
    return rows


def _load_manifest(
    path: Path, raw: bytes, expected_target: DurableTargetIdentity | None
) -> LoadedCoverageCampaign:
    """Deep-load one V4/V5 Campaign; the codec validates manifest and points once."""
    try:
        if len(raw) > MAX_MANIFEST_BYTES:
            raise CoverageCampaignStoreError(
                "COV_MANIFEST_LIMIT", "Coverage Campaign manifest exceeds its size limit"
            )
        document = dict(require_dict(json.loads(raw)))
        schema = document.get("$schema")
        # A tampered non-string schema (a list or object) is unhashable: reject it as unsupported.
        if not isinstance(schema, str) or schema not in POINT_STORE_CAMPAIGN_SCHEMAS:
            raise CoverageCampaignStoreError(
                "COV_SCHEMA_VERSION_UNSUPPORTED",
                "Unsupported Coverage Campaign schema; recollect coverage with this Booley version",
            )
        if set(document) != _MANIFEST_FIELDS:
            raise CoverageCampaignStoreError(
                "COV_MANIFEST_FORMAT", f"Malformed {schema} Coverage Campaign manifest"
            )
        point_store = _decode_reference(document["point_store"])
        if expected_target is None:
            target = require_dict(document.get("target"), field="target")
            expected_target = DurableTargetIdentity(require_str(target, "identity"))
        # Source rollups and scoring are writer-derived manifest content (ADR 0062).
        v1_document = {key: value for key, value in document.items() if key not in _STORE_FIELDS}
        v1_document["$schema"] = CAMPAIGN_SCHEMA_V1
        v1_document["points"] = _point_documents(path.parent / POINT_STORE_NAME)
        campaign = _decode_owned_coverage_campaign(v1_document, expected_target)
    except (CoverageCampaignValidationError, CoverageCampaignStoreError):
        raise
    except (json.JSONDecodeError, UnicodeError, BoundaryError) as exc:
        raise CoverageCampaignStoreError(
            "COV_MANIFEST_FORMAT", f"Cannot load Coverage Campaign: {exc}"
        ) from exc
    summary = CoverageCampaignSummary(
        source_schema=schema,
        document=freeze_coverage_mapping(document),
        manifest_sha256=f"{_SHA256_PREFIX}{hashlib.sha256(raw).hexdigest()}",
        point_store=point_store,
    )
    return LoadedCoverageCampaign(summary, campaign)


def load_coverage_campaign(
    path: Path, expected_target: DurableTargetIdentity | None = None
) -> LoadedCoverageCampaign:
    """Deep-load one V4/V5 Campaign; the codec validates it once, after the points are read."""
    path = _canonical_campaign_path(path)
    try:
        return _load_manifest(path, _read_manifest_bytes(path), expected_target)
    except OSError as exc:
        raise CoverageCampaignStoreError(
            "COV_MANIFEST_FORMAT", f"Cannot load Coverage Campaign: {exc}"
        ) from exc


def load_coverage_campaign_bytes(
    path: Path,
    raw: bytes,
    expected_target: DurableTargetIdentity | None = None,
) -> LoadedCoverageCampaign:
    """Deep-load a V4/V5 Campaign from exact manifest bytes already authenticated."""
    return _load_manifest(_canonical_campaign_path(path), raw, expected_target)
