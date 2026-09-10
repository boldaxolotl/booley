"""Durable V1/V2 Coverage Campaign persistence behind one filesystem seam."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_int,
    require_list,
    require_str,
)

from .campaign_reports import is_report_link
from .coverage_campaign import (
    CoverageCampaign,
    CoverageCampaignValidationError,
    CoverageRollup,
    CoverageTarget,
    DurableTargetIdentity,
    FrozenJson,
    decode_coverage_campaign,
    encode_coverage_campaign,
    encode_coverage_point,
    freeze_coverage_mapping,
    validate_coverage_campaign_summary,
)

CAMPAIGN_SCHEMA_V1 = "booley.coverage-campaign/v1"
CAMPAIGN_SCHEMA_V2 = "booley.coverage-campaign/v2"
POINT_STORE_SCHEMA_V1 = "booley.coverage-points/v1"
POINT_STORE_NAME = "coverage-points.jsonl.gz"
MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_POINTS = 1_000_000
_SHA256_PREFIX = "sha256:"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class CoveragePointStoreReference:
    """Integrity and resource limits for one V2 point store."""

    path: str
    sha256: str
    bytes: int
    uncompressed_bytes: int
    point_count: int


@dataclass(frozen=True)
class CoverageCampaignSummary:
    """Validated manifest-local Campaign facts; point bytes are not opened."""

    source_schema: str
    campaign_id: str
    target: CoverageTarget
    rollups: tuple[CoverageRollup, ...]
    collection: Mapping[str, FrozenJson]
    evaluation: Mapping[str, FrozenJson]
    document: Mapping[str, FrozenJson]
    point_store: CoveragePointStoreReference | None


@dataclass(frozen=True)
class LoadedCoverageCampaign:
    """A complete Campaign together with its validated persisted envelope."""

    summary: CoverageCampaignSummary
    campaign: CoverageCampaign


@dataclass(frozen=True)
class CampaignPaths:
    """Canonical files published for one V2 Campaign."""

    campaign: Path
    points: Path


class CoverageCampaignStoreError(ValueError):
    """Campaign persistence cannot produce or recover exact evidence."""


class _DigestReader(io.RawIOBase):
    def __init__(self, stream: io.BufferedReader) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        count = self.stream.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
            self.bytes_read += count
            if self.bytes_read > MAX_COMPRESSED_BYTES:
                raise CoverageCampaignStoreError(
                    "Compressed Coverage Point storage exceeds V2 limit"
                )
        return count


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
    if len(line) > MAX_LINE_BYTES:
        raise CoverageCampaignStoreError("Coverage Point line exceeds V2 limit")
    total += len(line)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise CoverageCampaignStoreError("Uncompressed Coverage Point storage exceeds V2 limit")
    stream.write(line)
    return total


def _directory_fsync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_commit_new(temporary: Path, final: Path) -> None:
    import ctypes

    move_file_ex = ctypes.windll.kernel32.MoveFileExW
    move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong)
    move_file_ex.restype = ctypes.c_int
    movefile_write_through = 0x8
    if not move_file_ex(str(temporary), str(final), movefile_write_through):
        raise ctypes.WinError()


def _commit_new(temporary: Path, final: Path) -> None:
    if os.name == "nt":
        _windows_commit_new(temporary, final)
        return
    os.link(temporary, final)
    temporary.unlink()
    _directory_fsync(final.parent)


def _write_point_store(path: Path, campaign: CoverageCampaign) -> CoveragePointStoreReference:
    if len(campaign.points) > MAX_POINTS:
        raise CoverageCampaignStoreError("Coverage Point count exceeds V2 limit")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".coverage-points-", dir=path.parent)
    temporary = Path(temporary_name)
    uncompressed_bytes = 0
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
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
            raw.flush()
            os.fsync(raw.fileno())
        size = temporary.stat().st_size
        if size > MAX_COMPRESSED_BYTES:
            raise CoverageCampaignStoreError("Compressed Coverage Point storage exceeds V2 limit")
        with temporary.open("rb") as stream:
            digest = f"{_SHA256_PREFIX}{hashlib.file_digest(stream, 'sha256').hexdigest()}"
        _commit_new(temporary, path)
        return CoveragePointStoreReference(
            path=POINT_STORE_NAME,
            sha256=digest,
            bytes=size,
            uncompressed_bytes=uncompressed_bytes,
            point_count=len(campaign.points),
        )
    finally:
        temporary.unlink(missing_ok=True)


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
    document["$schema"] = CAMPAIGN_SCHEMA_V2
    del document["points"]
    document["point_store"] = _point_store_document(reference)
    return document


def _write_manifest(path: Path, document: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".coverage-manifest-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _commit_new(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_coverage_campaign(target_dir: Path, campaign: CoverageCampaign) -> CampaignPaths:
    """Publish a complete V2 Campaign without replacing existing final files."""
    if _contains_link(target_dir):
        raise CoverageCampaignStoreError("Coverage Campaign path contains a link")
    target_dir.mkdir(parents=True, exist_ok=True)
    campaign_path = target_dir / "coverage.json"
    points_path = target_dir / POINT_STORE_NAME
    if campaign_path.exists() or points_path.exists():
        raise CoverageCampaignStoreError("Coverage Campaign publication cannot replace evidence")
    reference = _write_point_store(points_path, campaign)
    document = _manifest(campaign, reference)
    _load_v2(campaign_path, document, DurableTargetIdentity(campaign.target.identity))
    _write_manifest(campaign_path, document)
    return CampaignPaths(campaign_path, points_path)


def _decode_rollups(value: object) -> tuple[CoverageRollup, ...]:
    rollups = []
    for index, item in enumerate(require_list(value, field="rollups")):
        record = require_dict(item, field=f"rollups[{index}]")
        expected = {
            "metric",
            "semantics",
            "total_points",
            "eligible_points",
            "covered_points",
            "waived_points",
            "percent",
        }
        if set(record) != expected:
            raise CoverageCampaignStoreError("Malformed V2 Coverage rollup")
        percent = record["percent"]
        if percent is not None and (
            isinstance(percent, bool) or not isinstance(percent, int | float)
        ):
            raise CoverageCampaignStoreError("Malformed V2 Coverage percentage")
        rollups.append(
            CoverageRollup(
                require_str(record, "metric"),
                require_str(record, "semantics"),
                require_int(record["total_points"], field="total_points"),
                require_int(record["eligible_points"], field="eligible_points"),
                require_int(record["covered_points"], field="covered_points"),
                require_int(record["waived_points"], field="waived_points"),
                float(percent) if percent is not None else None,
            )
        )
    return tuple(rollups)


def _decode_reference(value: object) -> CoveragePointStoreReference:
    record = require_dict(value, field="point_store")
    expected = {
        "$schema",
        "path",
        "encoding",
        "sha256",
        "bytes",
        "uncompressed_bytes",
        "point_count",
    }
    if set(record) != expected:
        raise CoverageCampaignStoreError("Malformed V2 Coverage Point storage reference")
    if require_str(record, "$schema") != POINT_STORE_SCHEMA_V1:
        raise CoverageCampaignStoreError("Unsupported Coverage Point storage schema")
    if require_str(record, "path") != POINT_STORE_NAME:
        raise CoverageCampaignStoreError("Coverage Point storage path is not canonical")
    if require_str(record, "encoding") != "jsonl+gzip":
        raise CoverageCampaignStoreError("Unsupported Coverage Point storage encoding")
    digest = require_str(record, "sha256")
    if _SHA256_RE.fullmatch(digest) is None:
        raise CoverageCampaignStoreError("Malformed Coverage Point storage digest")
    reference = CoveragePointStoreReference(
        POINT_STORE_NAME,
        digest,
        require_int(record["bytes"], field="bytes"),
        require_int(record["uncompressed_bytes"], field="uncompressed_bytes"),
        require_int(record["point_count"], field="point_count"),
    )
    if not 0 <= reference.bytes <= MAX_COMPRESSED_BYTES:
        raise CoverageCampaignStoreError("Compressed Coverage Point storage exceeds V2 limit")
    if not 0 <= reference.uncompressed_bytes <= MAX_UNCOMPRESSED_BYTES:
        raise CoverageCampaignStoreError("Uncompressed Coverage Point storage exceeds V2 limit")
    if not 0 <= reference.point_count <= MAX_POINTS:
        raise CoverageCampaignStoreError("Coverage Point count exceeds V2 limit")
    return reference


def _summary_v2(document: Mapping[str, object], expected_target: DurableTargetIdentity):
    expected = {
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
        "collection",
        "findings",
        "evaluation",
    }
    if set(document) != expected:
        raise CoverageCampaignStoreError("Malformed V2 Coverage Campaign manifest")
    validate_coverage_campaign_summary(document, expected_target)
    target = require_dict(document.get("target"), field="target")
    identity = require_str(target, "identity")
    if identity != expected_target:
        raise CoverageCampaignStoreError("Coverage Campaign belongs to a different Target")
    collection = require_dict(document.get("collection"), field="collection")
    evaluation = require_dict(document.get("evaluation"), field="evaluation")
    return CoverageCampaignSummary(
        source_schema=CAMPAIGN_SCHEMA_V2,
        campaign_id=require_str(document, "campaign_id"),
        target=CoverageTarget(identity, require_str(target, "selector")),
        rollups=_decode_rollups(document.get("rollups")),
        collection=freeze_coverage_mapping(collection),
        evaluation=freeze_coverage_mapping(evaluation),
        document=freeze_coverage_mapping(document),
        point_store=_decode_reference(document.get("point_store")),
    )


def _summary_v1(document: Mapping[str, object], expected_target: DurableTargetIdentity):
    campaign = decode_coverage_campaign(document, expected_target)
    return CoverageCampaignSummary(
        source_schema=CAMPAIGN_SCHEMA_V1,
        campaign_id=campaign.campaign_id,
        target=campaign.target,
        rollups=campaign.rollups,
        collection=campaign.collection,
        evaluation=campaign.evaluation,
        document=freeze_coverage_mapping(document),
        point_store=None,
    )


def _contains_link(path: Path) -> bool:
    return any(is_report_link(item) for item in (path, *path.parents))


def _read_document(path: Path) -> dict[str, object]:
    if _contains_link(path) or not path.is_file():
        raise CoverageCampaignStoreError(f"Expected a retained regular Campaign file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    return dict(require_dict(value))


def read_coverage_summary(
    path: Path, expected_target: DurableTargetIdentity
) -> CoverageCampaignSummary:
    """Read manifest facts without opening V2 Coverage Point storage."""
    try:
        document = _read_document(path)
        schema = document.get("$schema")
        if schema == CAMPAIGN_SCHEMA_V1:
            return _summary_v1(document, expected_target)
        if schema == CAMPAIGN_SCHEMA_V2:
            return _summary_v2(document, expected_target)
        raise CoverageCampaignStoreError("Unsupported Coverage Campaign schema")
    except (OSError, json.JSONDecodeError, UnicodeError, BoundaryError) as exc:
        raise CoverageCampaignStoreError(f"Cannot read Coverage Campaign summary: {exc}") from exc


def _json_line(stream: gzip.GzipFile, total: int) -> tuple[dict[str, object] | None, int]:
    line = stream.readline(MAX_LINE_BYTES + 1)
    if not line:
        return None, total
    if len(line) > MAX_LINE_BYTES:
        raise CoverageCampaignStoreError("Coverage Point line exceeds V2 limit")
    total += len(line)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise CoverageCampaignStoreError("Uncompressed Coverage Point storage exceeds V2 limit")
    try:
        return dict(require_dict(json.loads(line))), total
    except (json.JSONDecodeError, UnicodeError, BoundaryError) as exc:
        raise CoverageCampaignStoreError("Malformed Coverage Point JSON line") from exc


def _read_point_lines(
    source: io.BufferedReader, summary: CoverageCampaignSummary
) -> tuple[list[dict[str, object]], int, _DigestReader]:
    documents: list[dict[str, object]] = []
    total = 0
    raw = _DigestReader(source)
    buffered = io.BufferedReader(raw)
    try:
        with gzip.GzipFile(fileobj=buffered, mode="rb") as stream:
            header, total = _json_line(stream, total)
            if header != {
                "$schema": POINT_STORE_SCHEMA_V1,
                "campaign_id": summary.campaign_id,
                "target_identity": summary.target.identity,
            }:
                raise CoverageCampaignStoreError("Coverage Point storage header mismatch")
            while True:
                item, total = _json_line(stream, total)
                if item is None:
                    break
                documents.append(item)
                if len(documents) > MAX_POINTS:
                    raise CoverageCampaignStoreError("Coverage Point count exceeds V2 limit")
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise CoverageCampaignStoreError("Malformed gzip Coverage Point storage") from exc
    while buffered.read(1024 * 1024):
        pass
    return documents, total, raw


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(before, field) == getattr(after, field) for field in fields)


def _verify_point_read(
    reference: CoveragePointStoreReference,
    documents: list[dict[str, object]],
    total: int,
    raw: _DigestReader,
) -> None:
    digest = f"{_SHA256_PREFIX}{raw.digest.hexdigest()}"
    if raw.bytes_read != reference.bytes or digest != reference.sha256:
        raise CoverageCampaignStoreError("Coverage Point storage integrity mismatch")
    if total != reference.uncompressed_bytes:
        raise CoverageCampaignStoreError("Coverage Point storage decoded byte count mismatch")
    if len(documents) != reference.point_count:
        raise CoverageCampaignStoreError("Coverage Point storage count mismatch")


def _point_documents(path: Path, summary: CoverageCampaignSummary) -> list[dict[str, object]]:
    reference = summary.point_store
    assert reference is not None
    if _contains_link(path) or not path.is_file():
        raise CoverageCampaignStoreError("Coverage Point storage is missing or unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != reference.bytes:
            raise CoverageCampaignStoreError("Coverage Point storage byte count changed")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            documents, total, raw = _read_point_lines(source, summary)
        if not _same_file(before, os.fstat(descriptor)):
            raise CoverageCampaignStoreError("Coverage Point storage changed while reading")
        _verify_point_read(reference, documents, total, raw)
        return documents
    finally:
        os.close(descriptor)


def _load_v2(
    path: Path, document: Mapping[str, object], expected_target: DurableTargetIdentity
) -> LoadedCoverageCampaign:
    summary = _summary_v2(document, expected_target)
    points_path = path.parent / POINT_STORE_NAME
    points = _point_documents(points_path, summary)
    v1_document = dict(document)
    v1_document["$schema"] = CAMPAIGN_SCHEMA_V1
    del v1_document["point_store"]
    v1_document["points"] = points
    campaign = decode_coverage_campaign(v1_document, expected_target)
    return LoadedCoverageCampaign(summary, campaign)


def load_coverage_campaign(
    path: Path, expected_target: DurableTargetIdentity
) -> LoadedCoverageCampaign:
    """Deep-load one V1/V2 Campaign and accept no point evidence before validation."""
    try:
        document = _read_document(path)
        schema = document.get("$schema")
        if schema == CAMPAIGN_SCHEMA_V1:
            summary = _summary_v1(document, expected_target)
            return LoadedCoverageCampaign(
                summary, decode_coverage_campaign(document, expected_target)
            )
        if schema == CAMPAIGN_SCHEMA_V2:
            return _load_v2(path, document, expected_target)
        raise CoverageCampaignStoreError("Unsupported Coverage Campaign schema")
    except CoverageCampaignValidationError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeError, BoundaryError) as exc:
        raise CoverageCampaignStoreError(f"Cannot load Coverage Campaign: {exc}") from exc
