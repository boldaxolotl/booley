"""Durable V3 Coverage Campaign persistence behind one filesystem seam."""

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
    require_finite_number,
    require_int,
    require_list,
    require_str,
)
from booley.runtime.regular_file import open_regular_nofollow

from .campaign_reports import is_report_link
from .coverage_campaign import (
    CoverageCampaign,
    CoverageCampaignValidationError,
    CoverageRollup,
    CoverageSourceRollup,
    CoverageTarget,
    DurableTargetIdentity,
    FrozenJson,
    coverage_metric_semantics,
    decode_coverage_campaign,
    derive_coverage_source_rollups,
    encode_coverage_campaign,
    encode_coverage_point,
    freeze_coverage_mapping,
    validate_coverage_campaign_summary,
)

CAMPAIGN_SCHEMA_V1 = "booley.coverage-campaign/v1"
CAMPAIGN_SCHEMA_V2 = "booley.coverage-campaign/v2"
CAMPAIGN_SCHEMA_V3 = "booley.coverage-campaign/v3"
POINT_STORE_SCHEMA_V1 = "booley.coverage-points/v1"
POINT_STORE_NAME = "coverage-points.jsonl.gz"
MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_POINTS = 1_000_000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_SHA256_PREFIX = "sha256:"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class CoveragePointStoreReference:
    """Integrity and resource limits for one compressed point store."""

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
    source_rollups: tuple[CoverageSourceRollup, ...]
    collection: Mapping[str, FrozenJson]
    evaluation: Mapping[str, FrozenJson]
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
    """Canonical files published for one V3 Campaign."""

    campaign: Path
    points: Path


class CoverageCampaignStoreError(ValueError):
    """Campaign persistence cannot produce or recover exact evidence."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if message is None:
            message, code = code, "COV_STORE_OPERATION"
        self.code = code
        super().__init__(f"{code}: {message}")


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
                    "COV_POINT_LIMIT", "Compressed Coverage Point storage exceeds V2 limit"
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
        raise CoverageCampaignStoreError("COV_POINT_LIMIT", "Coverage Point line exceeds V2 limit")
    total += len(line)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise CoverageCampaignStoreError(
            "COV_POINT_LIMIT", "Uncompressed Coverage Point storage exceeds V2 limit"
        )
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
        raise CoverageCampaignStoreError(
            "COV_POINT_LIMIT", "Coverage Point count exceeds V2 limit"
        )
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
            raise CoverageCampaignStoreError(
                "COV_POINT_LIMIT", "Compressed Coverage Point storage exceeds V2 limit"
            )
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
    document["$schema"] = CAMPAIGN_SCHEMA_V3
    del document["points"]
    document["point_store"] = _point_store_document(reference)
    document["source_rollups"] = [
        {
            "source": item.source,
            "rollups": [_rollup_document(rollup) for rollup in item.rollups],
        }
        for item in derive_coverage_source_rollups(campaign.points)
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


def _write_manifest(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".coverage-manifest-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _commit_new(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_coverage_campaign(target_dir: Path, campaign: CoverageCampaign) -> CampaignPaths:
    """Publish a complete V3 Campaign without replacing existing final files."""
    if _contains_link(target_dir):
        raise CoverageCampaignStoreError("Coverage Campaign path contains a link")
    target_dir.mkdir(parents=True, exist_ok=True)
    campaign_path = target_dir / "coverage.json"
    points_path = target_dir / POINT_STORE_NAME
    if campaign_path.exists() or points_path.exists():
        raise CoverageCampaignStoreError("Coverage Campaign publication cannot replace evidence")
    reference = _write_point_store(points_path, campaign)
    try:
        document = _manifest(campaign, reference)
        manifest_bytes = _manifest_bytes(document)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise CoverageCampaignStoreError(
                "COV_MANIFEST_LIMIT", "Coverage Campaign manifest exceeds V3 limit"
            )
        digest = f"{_SHA256_PREFIX}{hashlib.sha256(manifest_bytes).hexdigest()}"
        _summary_v3(document, DurableTargetIdentity(campaign.target.identity), digest)
        _write_manifest(campaign_path, manifest_bytes)
    finally:
        if not campaign_path.exists():
            points_path.unlink(missing_ok=True)
    return CampaignPaths(campaign_path, points_path)


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
        raise CoverageCampaignStoreError(
            "COV_POINT_REFERENCE", "Malformed V2 Coverage Point storage reference"
        )
    if require_str(record, "$schema") != POINT_STORE_SCHEMA_V1:
        raise CoverageCampaignStoreError(
            "COV_POINT_SCHEMA_UNSUPPORTED", "Unsupported Coverage Point storage schema"
        )
    if require_str(record, "path") != POINT_STORE_NAME:
        raise CoverageCampaignStoreError(
            "COV_POINT_PATH", "Coverage Point storage path is not canonical"
        )
    if require_str(record, "encoding") != "jsonl+gzip":
        raise CoverageCampaignStoreError(
            "COV_POINT_ENCODING_UNSUPPORTED", "Unsupported Coverage Point storage encoding"
        )
    digest = require_str(record, "sha256")
    if _SHA256_RE.fullmatch(digest) is None:
        raise CoverageCampaignStoreError(
            "COV_POINT_REFERENCE", "Malformed Coverage Point storage digest"
        )
    reference = CoveragePointStoreReference(
        POINT_STORE_NAME,
        digest,
        require_int(record["bytes"], field="bytes"),
        require_int(record["uncompressed_bytes"], field="uncompressed_bytes"),
        require_int(record["point_count"], field="point_count"),
    )
    _validate_reference_limits(reference)
    return reference


def _validate_reference_limits(reference: CoveragePointStoreReference) -> None:
    if not 0 <= reference.bytes <= MAX_COMPRESSED_BYTES:
        raise CoverageCampaignStoreError(
            "COV_POINT_LIMIT", "Compressed Coverage Point storage exceeds V2 limit"
        )
    if not 0 <= reference.uncompressed_bytes <= MAX_UNCOMPRESSED_BYTES:
        raise CoverageCampaignStoreError(
            "COV_POINT_LIMIT", "Uncompressed Coverage Point storage exceeds V2 limit"
        )
    if not 0 <= reference.point_count <= MAX_POINTS:
        raise CoverageCampaignStoreError(
            "COV_POINT_LIMIT", "Coverage Point count exceeds V2 limit"
        )


def _decode_source_rollups(value: object) -> tuple[CoverageSourceRollup, ...]:
    records = require_list(value, field="source_rollups")
    decoded: list[CoverageSourceRollup] = []
    previous_source: str | None = None
    for index, item in enumerate(records):
        record = require_dict(item, field=f"source_rollups[{index}]")
        if set(record) != {"source", "rollups"}:
            raise CoverageCampaignStoreError(
                "COV_MANIFEST_FORMAT", "Malformed V3 source coverage summary"
            )
        source = require_str(record, "source")
        if previous_source is not None and source <= previous_source:
            raise CoverageCampaignStoreError(
                "COV_SOURCE_ROLLUP_MISMATCH", "Source coverage summaries are not canonical"
            )
        previous_source = source
        rollup_values = require_list(record["rollups"], field="rollups")
        metrics = ("line", "branch", "expression", "toggle")
        if len(rollup_values) != len(metrics):
            raise CoverageCampaignStoreError(
                "COV_SOURCE_ROLLUP_MISMATCH", "Source coverage metric order is not canonical"
            )
        rollups = tuple(
            _decode_manifest_rollup(item, expected_metric=metric)
            for item, metric in zip(rollup_values, metrics, strict=True)
        )
        decoded.append(CoverageSourceRollup(source, rollups))
    return tuple(decoded)


def _decode_manifest_rollup(value: object, *, expected_metric: str) -> CoverageRollup:
    record = require_dict(value, field="source rollup")
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
        raise CoverageCampaignStoreError(
            "COV_MANIFEST_FORMAT", "Malformed V3 source metric summary"
        )
    metric = require_str(record, "metric")
    semantics = require_str(record, "semantics")
    if metric != expected_metric or semantics != coverage_metric_semantics(metric):
        raise CoverageCampaignStoreError(
            "COV_SOURCE_ROLLUP_MISMATCH",
            "Source coverage metric identity is not canonical",
        )
    raw_percent = record["percent"]
    percent = (
        require_finite_number(raw_percent, field="percent") if raw_percent is not None else None
    )
    rollup = CoverageRollup(
        metric,
        semantics,
        require_int(record["total_points"], field="total_points"),
        require_int(record["eligible_points"], field="eligible_points"),
        require_int(record["covered_points"], field="covered_points"),
        require_int(record["waived_points"], field="waived_points"),
        percent,
    )
    counts = (
        rollup.total_points,
        rollup.eligible_points,
        rollup.covered_points,
        rollup.waived_points,
    )
    expected_percent = (
        round(rollup.covered_points * 100 / rollup.eligible_points, 2)
        if rollup.eligible_points
        else None
    )
    if (
        any(count < 0 for count in counts)
        or rollup.covered_points > rollup.eligible_points
        or rollup.eligible_points + rollup.waived_points > rollup.total_points
        or rollup.percent != expected_percent
    ):
        raise CoverageCampaignStoreError(
            "COV_SOURCE_ROLLUP_MISMATCH", "Source coverage summary arithmetic is invalid"
        )
    return rollup


def _validate_source_reconciliation(
    source_rollups: tuple[CoverageSourceRollup, ...], rollups: tuple[CoverageRollup, ...]
) -> None:
    overall = {item.metric: item for item in rollups}
    metrics = ("line", "branch", "expression", "toggle")
    for index, metric in enumerate(metrics):
        matches = [source.rollups[index] for source in source_rollups]
        expected = overall.get(metric)
        totals = tuple(
            sum(getattr(item, field) for item in matches)
            for field in ("total_points", "eligible_points", "covered_points", "waived_points")
        )
        expected_totals = (
            (0, 0, 0, 0)
            if expected is None
            else (
                expected.total_points,
                expected.eligible_points,
                expected.covered_points,
                expected.waived_points,
            )
        )
        if totals != expected_totals:
            raise CoverageCampaignStoreError(
                "COV_SOURCE_ROLLUP_MISMATCH",
                "Source coverage summaries do not reconcile with overall rollups",
            )


def _summary_v3(
    document: Mapping[str, object],
    expected_target: DurableTargetIdentity,
    manifest_sha256: str,
) -> CoverageCampaignSummary:
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
        "source_rollups",
        "collection",
        "findings",
        "evaluation",
    }
    if set(document) != expected:
        raise CoverageCampaignStoreError(
            "COV_MANIFEST_FORMAT", "Malformed V3 Coverage Campaign manifest"
        )
    probe = dict(document)
    source_rollup_values = probe.pop("source_rollups")
    fields = validate_coverage_campaign_summary(probe, expected_target)
    source_rollups = _decode_source_rollups(source_rollup_values)
    _validate_source_reconciliation(source_rollups, fields.rollups)
    return CoverageCampaignSummary(
        source_schema=CAMPAIGN_SCHEMA_V3,
        campaign_id=fields.campaign_id,
        target=fields.target,
        rollups=fields.rollups,
        source_rollups=source_rollups,
        collection=fields.collection,
        evaluation=fields.evaluation,
        document=freeze_coverage_mapping(document),
        manifest_sha256=manifest_sha256,
        point_store=_decode_reference(document.get("point_store")),
    )


def _contains_link(path: Path) -> bool:
    return any(is_report_link(item) for item in (path, *path.parents))


def _open_regular(path: Path) -> int:
    try:
        return open_regular_nofollow(path)
    except OSError as exc:
        raise CoverageCampaignStoreError(
            "COV_PATH_UNSAFE", f"Cannot open retained regular file: {path}"
        ) from exc


def _read_document(path: Path) -> tuple[dict[str, object], str]:
    descriptor = _open_regular(path)
    try:
        before = os.fstat(descriptor)
        if before.st_size > MAX_MANIFEST_BYTES:
            raise CoverageCampaignStoreError(
                "COV_MANIFEST_LIMIT", "Coverage Campaign manifest exceeds V3 limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_MANIFEST_BYTES + 1)
        if not _same_file(before, os.fstat(descriptor)):
            raise CoverageCampaignStoreError(
                "COV_MANIFEST_INTEGRITY", "Coverage Campaign changed while reading"
            )
        value = json.loads(data)
        digest = f"{_SHA256_PREFIX}{hashlib.sha256(data).hexdigest()}"
        return dict(require_dict(value)), digest
    finally:
        os.close(descriptor)


def read_coverage_summary(
    path: Path, expected_target: DurableTargetIdentity
) -> CoverageCampaignSummary:
    """Read V3 manifest facts without opening Coverage Point storage."""
    try:
        document, digest = _read_document(path)
        schema = document.get("$schema")
        if schema == CAMPAIGN_SCHEMA_V3:
            return _summary_v3(document, expected_target, digest)
        raise CoverageCampaignStoreError(
            "COV_SCHEMA_VERSION_UNSUPPORTED",
            "Unsupported Coverage Campaign schema; recollect coverage with this Booley version",
        )
    except CoverageCampaignStoreError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeError, BoundaryError) as exc:
        raise CoverageCampaignStoreError(
            "COV_MANIFEST_FORMAT", f"Cannot read Coverage Campaign summary: {exc}"
        ) from exc


def _json_line(stream: gzip.GzipFile, total: int) -> tuple[dict[str, object] | None, int]:
    line = stream.readline(MAX_LINE_BYTES + 1)
    if not line:
        return None, total
    if len(line) > MAX_LINE_BYTES:
        raise CoverageCampaignStoreError("COV_POINT_LIMIT", "Coverage Point line exceeds V2 limit")
    total += len(line)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise CoverageCampaignStoreError(
            "COV_POINT_LIMIT", "Uncompressed Coverage Point storage exceeds V2 limit"
        )
    try:
        document = dict(require_dict(json.loads(line)))
        if line != _canonical_line(document):
            raise CoverageCampaignStoreError(
                "COV_POINT_CANONICAL", "Coverage Point JSON line is not canonical"
            )
        return document, total
    except (json.JSONDecodeError, UnicodeError, BoundaryError) as exc:
        raise CoverageCampaignStoreError(
            "COV_POINT_FORMAT", "Malformed Coverage Point JSON line"
        ) from exc


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
                raise CoverageCampaignStoreError(
                    "COV_POINT_HEADER_MISMATCH", "Coverage Point storage header mismatch"
                )
            while True:
                item, total = _json_line(stream, total)
                if item is None:
                    break
                documents.append(item)
                if len(documents) > MAX_POINTS:
                    raise CoverageCampaignStoreError(
                        "COV_POINT_LIMIT", "Coverage Point count exceeds V2 limit"
                    )
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise CoverageCampaignStoreError(
            "COV_POINT_FORMAT", "Malformed gzip Coverage Point storage"
        ) from exc
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
        raise CoverageCampaignStoreError(
            "COV_POINT_INTEGRITY", "Coverage Point storage integrity mismatch"
        )
    if total != reference.uncompressed_bytes:
        raise CoverageCampaignStoreError(
            "COV_POINT_INTEGRITY", "Coverage Point storage decoded byte count mismatch"
        )
    if len(documents) != reference.point_count:
        raise CoverageCampaignStoreError(
            "COV_POINT_COUNT_MISMATCH", "Coverage Point storage count mismatch"
        )


def _point_documents(path: Path, summary: CoverageCampaignSummary) -> list[dict[str, object]]:
    reference = summary.point_store
    assert reference is not None
    descriptor = _open_regular(path)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != reference.bytes:
            raise CoverageCampaignStoreError(
                "COV_POINT_INTEGRITY", "Coverage Point storage byte count changed"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            documents, total, raw = _read_point_lines(source, summary)
        if not _same_file(before, os.fstat(descriptor)):
            raise CoverageCampaignStoreError(
                "COV_POINT_INTEGRITY", "Coverage Point storage changed while reading"
            )
        _verify_point_read(reference, documents, total, raw)
        return documents
    finally:
        os.close(descriptor)


def _load_v3(
    path: Path,
    document: Mapping[str, object],
    expected_target: DurableTargetIdentity,
    manifest_sha256: str,
) -> LoadedCoverageCampaign:
    summary = _summary_v3(document, expected_target, manifest_sha256)
    points_path = path.parent / POINT_STORE_NAME
    points = _point_documents(points_path, summary)
    v1_document = dict(document)
    v1_document["$schema"] = CAMPAIGN_SCHEMA_V1
    del v1_document["point_store"]
    del v1_document["source_rollups"]
    v1_document["points"] = points
    campaign = decode_coverage_campaign(v1_document, expected_target)
    if derive_coverage_source_rollups(campaign.points) != summary.source_rollups:
        raise CoverageCampaignStoreError(
            "COV_SOURCE_ROLLUP_MISMATCH",
            "Source coverage summaries do not match the deterministic point derivation",
        )
    return LoadedCoverageCampaign(summary, campaign)


def load_coverage_campaign(
    path: Path, expected_target: DurableTargetIdentity | None = None
) -> LoadedCoverageCampaign:
    """Deep-load one V3 Campaign and accept no point evidence before validation."""
    try:
        document, digest = _read_document(path)
        schema = document.get("$schema")
        if schema != CAMPAIGN_SCHEMA_V3:
            raise CoverageCampaignStoreError(
                "COV_SCHEMA_VERSION_UNSUPPORTED",
                "Unsupported Coverage Campaign schema; recollect coverage with this Booley version",
            )
        if expected_target is None:
            target = require_dict(document.get("target"), field="target")
            expected_target = DurableTargetIdentity(require_str(target, "identity"))
        return _load_v3(path, document, expected_target, digest)
    except CoverageCampaignValidationError:
        raise
    except CoverageCampaignStoreError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeError, BoundaryError) as exc:
        raise CoverageCampaignStoreError(
            "COV_MANIFEST_FORMAT", f"Cannot load Coverage Campaign: {exc}"
        ) from exc
