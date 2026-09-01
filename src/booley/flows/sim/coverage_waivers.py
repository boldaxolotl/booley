"""Transactional loading and matching of project-wide approved coverage waivers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import tomllib
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoverageFinding,
    DurableTargetIdentity,
    FrozenJson,
)
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_epoch

_SET_SCHEMA = "booley.approved-waiver-set/v1"
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOP_LEVEL_FIELDS = frozenset({"schema", "source", "source_sha256", "approval"})
_APPROVAL_FIELDS = frozenset(
    {
        "id",
        "target",
        "point_id",
        "reason",
        "justification",
        "approved_by",
        "approved_at",
        "approval_ref",
        "proof",
    }
)
_REQUIRED_APPROVAL_FIELDS = _APPROVAL_FIELDS - {"proof"}


@dataclass(frozen=True)
class CoverageWaiverConfig:
    """Explicit repository anchor and safe relative approval directory."""

    anchor: Literal["rtl_repository", "project_data_repository"]
    directory: str


@dataclass(frozen=True)
class CoverageRepositoryRoots:
    """Repository roots supplied by coverage orchestration."""

    rtl_repository: Path
    project_data_repository: Path


class CoverageWaiverValidationError(ValueError):
    """All stable findings produced while loading one invalid waiver set."""

    def __init__(self, findings: tuple[CoverageFinding, ...]) -> None:
        self.findings = findings
        super().__init__(f"approved waiver set is invalid ({len(findings)} findings)")


@dataclass(frozen=True)
class ApprovedWaiver:
    """One validated exact Target-and-point exclusion."""

    target: DurableTargetIdentity
    point_id: str
    reason: str
    waiver_id: str
    waiver_file: str
    waiver_fingerprint: str
    provenance: Mapping[str, FrozenJson]
    source: str


@dataclass(frozen=True)
class ApprovedWaiverSet:
    """Immutable project-wide approved waiver input."""

    configuration: Mapping[str, FrozenJson]
    digest: str
    waivers: tuple[ApprovedWaiver, ...]

    def match(self, campaign: CoverageCampaign) -> ApprovedWaiverMatch:
        """Resolve applicable exact matches transactionally for one Campaign."""
        return _match_campaign(self.waivers, campaign)


@dataclass(frozen=True)
class ApprovedWaiverMatch:
    """All applicable waivers, or findings and no waivers."""

    waivers: tuple[ApprovedWaiver, ...]
    findings: tuple[CoverageFinding, ...]


def _digest(document: object) -> str:
    encoded = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _fingerprint(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _error(code: str, pointer: str, message: str) -> CoverageFinding:
    return CoverageFinding(severity="error", code=code, pointer=pointer, message=message)


def _safe_relative_posix(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    if unicodedata.normalize("NFC", value) != value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and _WINDOWS_DRIVE_RE.match(value) is None
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == value
    )


def _config_findings(config: CoverageWaiverConfig) -> tuple[CoverageFinding, ...]:
    findings: list[CoverageFinding] = []
    if config.anchor not in {"rtl_repository", "project_data_repository"}:
        findings.append(
            _error(
                "COV_WAIVER_ANCHOR_INVALID",
                "/anchor",
                "Anchor must name exactly one supplied repository root.",
            )
        )
    if not _safe_relative_posix(config.directory):
        findings.append(
            _error(
                "COV_WAIVER_DIRECTORY_UNSAFE",
                "/directory",
                "Directory must be a normalized relative POSIX path without dot segments.",
            )
        )
    return tuple(findings)


def _directory_findings(root: Path, relative: str) -> tuple[CoverageFinding, ...]:
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return (
                _error(
                    "COV_WAIVER_DIRECTORY_MISSING",
                    "/directory",
                    "Configured approved-waiver directory does not exist.",
                ),
            )
        except OSError:
            return (
                _error(
                    "COV_WAIVER_DIRECTORY_UNREADABLE",
                    "/directory",
                    "Configured approved-waiver directory is not readable.",
                ),
            )
        if stat.S_ISLNK(mode):
            return (
                _error(
                    "COV_WAIVER_DIRECTORY_SYMLINK",
                    "/directory",
                    "No configured directory component may be a symlink.",
                ),
            )
        if not stat.S_ISDIR(mode):
            return (
                _error(
                    "COV_WAIVER_DIRECTORY_INVALID",
                    "/directory",
                    "Configured approved-waiver path must be a directory.",
                ),
            )
    return ()


def _freeze(value: object) -> FrozenJson:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in sorted(value.items())})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"waiver provenance contains non-JSON value {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, FrozenJson]:
    frozen = _freeze(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("path is not a regular file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _read_rtl_source(
    root: Path, relative: str, pointer: str
) -> tuple[bytes | None, CoverageFinding | None]:
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return None, _error(
                "COV_WAIVER_SOURCE_MISSING", pointer, "Approved RTL source is missing."
            )
        if stat.S_ISLNK(mode):
            return None, _error(
                "COV_WAIVER_SOURCE_SYMLINK",
                pointer,
                "RTL source path components cannot be symlinks.",
            )
        expected = stat.S_ISREG(mode) if index == len(parts) - 1 else stat.S_ISDIR(mode)
        if not expected:
            return None, _error(
                "COV_WAIVER_SOURCE_INVALID",
                pointer,
                "RTL source must be a regular file beneath the RTL repository.",
            )
    try:
        return _read_regular_file(current), None
    except OSError:
        return None, _error(
            "COV_WAIVER_SOURCE_UNREADABLE", pointer, "Approved RTL source is unreadable."
        )


def _enumerate_approval_files(
    directory: Path,
) -> tuple[tuple[Path, ...], tuple[CoverageFinding, ...]]:
    files: list[Path] = []
    findings: list[CoverageFinding] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        for entry in sorted(current.iterdir(), reverse=True):
            relative = entry.relative_to(directory).as_posix()
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                code = (
                    "COV_WAIVER_FILE_SYMLINK"
                    if entry.name.endswith(".toml")
                    else "COV_WAIVER_DIRECTORY_SYMLINK"
                )
                findings.append(_error(code, f"/files/{relative}", "Symlinks are forbidden."))
            elif stat.S_ISDIR(mode):
                pending.append(entry)
            elif stat.S_ISREG(mode):
                files.append(entry)
            else:
                findings.append(
                    _error(
                        "COV_WAIVER_FILE_INVALID",
                        f"/files/{relative}",
                        "Approval directory entries must be regular files or directories.",
                    )
                )
    return tuple(sorted(files)), tuple(sorted(findings, key=lambda item: item.pointer))


def _point_source(point_id: object) -> str | None:
    if not isinstance(point_id, str) or not re.fullmatch(r"cp1:[A-Za-z0-9_-]+", point_id):
        return None
    payload = point_id.removeprefix("cp1:")
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        identity = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(identity, Mapping) or set(identity) != {
        "metric",
        "location",
        "hierarchy",
        "subject",
        "collector",
    }:
        return None
    location = identity.get("location")
    source = location.get("source") if isinstance(location, Mapping) else None
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(canonical.encode()).decode().rstrip("=")
    return source if isinstance(source, str) and point_id == f"cp1:{encoded}" else None


def _timestamp_is_canonical(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = parse_timestamp(value)
    except ValueError:
        return False
    return rfc3339_from_epoch(parsed.timestamp()) == value


def _proof_findings(proof: object, pointer: str) -> list[CoverageFinding]:
    if not isinstance(proof, Mapping):
        return [_error("COV_WAIVER_PROOF_INVALID", pointer, "Proof must be a table.")]
    if set(proof) != {"kind", "reference", "sha256"}:
        return [_error("COV_WAIVER_PROOF_INVALID", pointer, "Proof fields are closed.")]
    valid = (
        proof.get("kind") == "formal"
        and isinstance(proof.get("reference"), str)
        and bool(proof.get("reference"))
        and _SHA256_RE.fullmatch(str(proof.get("sha256"))) is not None
    )
    return [] if valid else [_error("COV_WAIVER_PROOF_INVALID", pointer, "Invalid proof.")]


def _proof_evidence_finding(
    root: Path,
    record: Mapping[str, object],
    pointer: str,
) -> CoverageFinding | None:
    proof = record.get("proof")
    if record.get("reason") != "unreachable" or _proof_findings(proof, pointer):
        return None
    assert isinstance(proof, Mapping)
    reference = str(proof["reference"])
    relative = reference.split("#", 1)[0]
    if not _safe_relative_posix(relative):
        return _error("COV_WAIVER_PROOF_INVALID", pointer, "Unsafe proof reference.")
    raw, finding = _read_proof_artifact(root, relative, pointer)
    if finding is not None:
        return finding
    assert raw is not None
    if proof["sha256"] != _fingerprint(raw):
        return _error("COV_WAIVER_PROOF_STALE", f"{pointer}/sha256", "Proof bytes changed.")
    return None


def _read_proof_artifact(
    root: Path, relative: str, pointer: str
) -> tuple[bytes | None, CoverageFinding | None]:
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return None, _error(
                "COV_WAIVER_PROOF_MISSING", pointer, "Approved proof artifact is missing."
            )
        if stat.S_ISLNK(mode):
            return None, _error("COV_WAIVER_PROOF_SYMLINK", pointer, "Proof path is a symlink.")
        expected = stat.S_ISREG(mode) if index == len(parts) - 1 else stat.S_ISDIR(mode)
        if not expected:
            return None, _error(
                "COV_WAIVER_PROOF_INVALID", pointer, "Proof is not a regular file."
            )
    try:
        return _read_regular_file(current), None
    except OSError:
        return None, _error(
            "COV_WAIVER_PROOF_UNREADABLE", pointer, "Approved proof artifact is unreadable."
        )


def _record_shape_findings(
    record: Mapping[str, object], pointer: str, known_targets: frozenset[str]
) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    unknown = set(record) - _APPROVAL_FIELDS
    missing = _REQUIRED_APPROVAL_FIELDS - set(record)
    empty = [
        key
        for key in _REQUIRED_APPROVAL_FIELDS
        if not isinstance(record.get(key), str) or not record[key]
    ]
    if unknown or missing or empty:
        findings.append(
            _error(
                "COV_WAIVER_RECORD_INCOMPLETE", pointer, "Approval fields are closed and required."
            )
        )
    if isinstance(record.get("target"), str) and record["target"] not in known_targets:
        findings.append(
            _error("COV_WAIVER_TARGET_UNKNOWN", f"{pointer}/target", "Unknown Target.")
        )
    return findings


def _record_binding_findings(
    record: Mapping[str, object], pointer: str, source: str
) -> list[CoverageFinding]:
    point_source = _point_source(record.get("point_id"))
    if point_source is None:
        return [
            _error(
                "COV_WAIVER_BINDING_NOT_EXACT",
                f"{pointer}/point_id",
                "Exact cp1 point id required.",
            )
        ]
    if point_source != source:
        return [
            _error(
                "COV_WAIVER_POINT_SOURCE_MISMATCH",
                f"{pointer}/point_id",
                "Point source differs from approval source.",
            )
        ]
    return []


def _record_reason_findings(record: Mapping[str, object], pointer: str) -> list[CoverageFinding]:
    reason = record.get("reason")
    proof = record.get("proof")
    if reason not in {"excluded", "unreachable"}:
        return [
            _error(
                "COV_WAIVER_REASON_INVALID",
                f"{pointer}/reason",
                "Reason must be excluded or unreachable.",
            )
        ]
    if reason == "unreachable" and proof is None:
        return [
            _error(
                "COV_WAIVER_PROOF_REQUIRED",
                f"{pointer}/proof",
                "Unreachable approval requires proof.",
            )
        ]
    if reason == "excluded" and proof is not None:
        return [
            _error(
                "COV_WAIVER_PROOF_UNEXPECTED",
                f"{pointer}/proof",
                "Excluded approval cannot carry proof.",
            )
        ]
    return _proof_findings(proof, f"{pointer}/proof") if proof is not None else []


def _record_findings(
    record: object,
    pointer: str,
    source: str,
    known_targets: frozenset[str],
) -> list[CoverageFinding]:
    if not isinstance(record, Mapping):
        return [_error("COV_WAIVER_RECORD_INVALID", pointer, "Approval must be a table.")]
    findings = _record_shape_findings(record, pointer, known_targets)
    findings.extend(_record_binding_findings(record, pointer, source))
    if not _timestamp_is_canonical(record.get("approved_at")):
        findings.append(
            _error(
                "COV_WAIVER_APPROVED_AT_INVALID",
                f"{pointer}/approved_at",
                "Canonical UTC RFC 3339 required.",
            )
        )
    findings.extend(_record_reason_findings(record, pointer))
    return findings


def _approval_from_document(
    document: Mapping[str, object],
    *,
    source: str,
    waiver_file: str,
    waiver_fingerprint: str,
) -> ApprovedWaiver:
    provenance = {
        "justification": document["justification"],
        "approved_by": document["approved_by"],
        "approved_at": document["approved_at"],
        "approval_ref": document["approval_ref"],
    }
    if "proof" in document:
        provenance["proof"] = document["proof"]
    return ApprovedWaiver(
        target=DurableTargetIdentity(str(document["target"])),
        point_id=str(document["point_id"]),
        reason=str(document["reason"]),
        waiver_id=str(document["id"]),
        waiver_file=waiver_file,
        waiver_fingerprint=waiver_fingerprint,
        provenance=_freeze_mapping(provenance),
        source=source,
    )


def _document_shape_findings(
    document: Mapping[str, object],
    pointer: str,
) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    if document.get("schema") != "booley.coverage-waivers/v1":
        findings.append(
            _error(
                "COV_WAIVER_SCHEMA_UNSUPPORTED",
                f"{pointer}/schema",
                "Expected booley.coverage-waivers/v1.",
            )
        )
    if set(document) != _TOP_LEVEL_FIELDS:
        findings.append(
            _error(
                "COV_WAIVER_FILE_FIELDS_INVALID",
                pointer,
                "Approval-file fields are closed and required.",
            )
        )
    if not _safe_relative_posix(document.get("source")):
        findings.append(
            _error("COV_WAIVER_SOURCE_UNSAFE", f"{pointer}/source", "Unsafe RTL source path.")
        )
    if _SHA256_RE.fullmatch(str(document.get("source_sha256"))) is None:
        findings.append(
            _error(
                "COV_WAIVER_SOURCE_FINGERPRINT_INVALID",
                f"{pointer}/source_sha256",
                "Source fingerprint must be lowercase SHA-256.",
            )
        )
    approvals = document.get("approval")
    if not isinstance(approvals, list) or not approvals:
        findings.append(
            _error(
                "COV_WAIVER_APPROVALS_INVALID",
                f"{pointer}/approval",
                "Approval must be a non-empty array of tables.",
            )
        )
    return findings


def _load_approval_file(
    raw: bytes,
    document: Mapping[str, object],
    relative_path: str,
) -> tuple[dict[str, object], list[ApprovedWaiver]]:
    source = str(document["source"])
    source_fingerprint = str(document["source_sha256"])
    file_fingerprint = _fingerprint(raw)
    raw_approvals = document["approval"]
    assert isinstance(raw_approvals, list)
    approvals_as_mappings = [item for item in raw_approvals if isinstance(item, Mapping)]
    assert len(approvals_as_mappings) == len(raw_approvals)
    approval_documents = sorted(
        approvals_as_mappings,
        key=lambda item: (str(item["target"]), str(item["point_id"]), str(item["id"])),
    )
    approvals = [
        _approval_from_document(
            approval,
            source=source,
            waiver_file=relative_path,
            waiver_fingerprint=file_fingerprint,
        )
        for approval in approval_documents
    ]
    projection = {
        "path": relative_path,
        "file_sha256": file_fingerprint,
        "source": source,
        "source_sha256": source_fingerprint,
        "approvals": [dict(approval) for approval in approval_documents],
    }
    return projection, approvals


def _read_approval_document(
    path: Path, pointer: str
) -> tuple[bytes | None, Mapping[str, object] | None, tuple[CoverageFinding, ...]]:
    try:
        raw = _read_regular_file(path)
    except OSError:
        return (
            None,
            None,
            (
                _error(
                    "COV_WAIVER_FILE_UNREADABLE",
                    pointer,
                    "Approval file is not a readable regular file.",
                ),
            ),
        )
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, None, (_error("COV_WAIVER_FILE_MALFORMED", pointer, str(exc)),)
    schema = document.get("schema")
    if "waiver_candidate" in document or (isinstance(schema, str) and "candidate" in schema):
        return (
            None,
            None,
            (
                _error(
                    "COV_WAIVER_CANDIDATE_FORBIDDEN",
                    f"{pointer}/schema",
                    "Coverage Analyst candidate content cannot be approved implicitly.",
                ),
            ),
        )
    return raw, document, ()


def _source_file_findings(
    document: Mapping[str, object],
    relative: str,
    pointer: str,
    rtl_root: Path,
) -> list[CoverageFinding]:
    source = document["source"]
    assert isinstance(source, str)
    findings: list[CoverageFinding] = []
    if relative != f"{source}.toml":
        findings.append(
            _error(
                "COV_WAIVER_SOURCE_FILE_MISMATCH",
                f"{pointer}/source",
                "Approval file path must mirror its RTL source and append .toml.",
            )
        )
    source_raw, source_finding = _read_rtl_source(rtl_root, source, f"{pointer}/source")
    if source_finding is not None:
        findings.append(source_finding)
    elif source_raw is not None and document.get("source_sha256") != _fingerprint(source_raw):
        findings.append(
            _error(
                "COV_WAIVER_SOURCE_STALE",
                f"{pointer}/source_sha256",
                "RTL source bytes no longer match the approved fingerprint.",
            )
        )
    return findings


def _approval_record_findings(
    document: Mapping[str, object],
    pointer: str,
    known_targets: frozenset[str],
    approval_root: Path,
) -> list[CoverageFinding]:
    source = document["source"]
    approvals = document.get("approval")
    assert isinstance(source, str)
    assert isinstance(approvals, list)
    findings: list[CoverageFinding] = []
    for index, record in enumerate(approvals):
        record_pointer = f"{pointer}/approval/{index}"
        findings.extend(_record_findings(record, record_pointer, source, known_targets))
        if isinstance(record, Mapping):
            proof = _proof_evidence_finding(approval_root, record, f"{record_pointer}/proof")
            if proof is not None:
                findings.append(proof)
    return findings


def _approval_file_findings(
    document: Mapping[str, object],
    relative: str,
    pointer: str,
    roots: CoverageRepositoryRoots,
    known_targets: frozenset[str],
    approval_root: Path,
) -> tuple[CoverageFinding, ...]:
    findings = _document_shape_findings(document, pointer)
    if not _safe_relative_posix(relative):
        findings.append(_error("COV_WAIVER_PATH_UNSAFE", pointer, "Unsafe approval-file path."))
    source = document.get("source")
    if isinstance(source, str) and _safe_relative_posix(source):
        findings.extend(_source_file_findings(document, relative, pointer, roots.rtl_repository))
        if isinstance(document.get("approval"), list):
            findings.extend(
                _approval_record_findings(document, pointer, known_targets, approval_root)
            )
    return tuple(findings)


def _try_load_approval_file(
    path: Path,
    directory: Path,
    roots: CoverageRepositoryRoots,
    known_targets: frozenset[str],
    approval_root: Path,
) -> tuple[
    tuple[dict[str, object], list[ApprovedWaiver]] | None,
    tuple[CoverageFinding, ...],
    str | None,
]:
    relative = path.relative_to(directory).as_posix()
    pointer = f"/files/{relative}"
    raw, document, read_findings = _read_approval_document(path, pointer)
    if document is None or raw is None:
        return None, read_findings, None
    findings = _approval_file_findings(
        document, relative, pointer, roots, known_targets, approval_root
    )
    source = document.get("source")
    if findings:
        return None, tuple(findings), source if isinstance(source, str) else None
    return _load_approval_file(raw, document, relative), (), str(source)


def _duplicate_source_findings(
    claims: list[tuple[str, str]],
) -> tuple[CoverageFinding, ...]:
    seen: set[str] = set()
    findings: list[CoverageFinding] = []
    for source, relative in claims:
        if source in seen:
            findings.append(
                _error(
                    "COV_WAIVER_SOURCE_DUPLICATE",
                    f"/files/{relative}/source",
                    "More than one approval file claims the same RTL source.",
                )
            )
        seen.add(source)
    return tuple(findings)


def _duplicate_findings(
    loaded: list[tuple[dict[str, object], list[ApprovedWaiver]]],
) -> tuple[CoverageFinding, ...]:
    findings: list[CoverageFinding] = []
    waivers = [waiver for _, items in loaded for waiver in items]
    seen_ids: set[tuple[str, str]] = set()
    for waiver in waivers:
        key = (waiver.waiver_file, waiver.waiver_id)
        if key in seen_ids:
            findings.append(
                _error(
                    "COV_WAIVER_ID_DUPLICATE",
                    f"/files/{waiver.waiver_file}",
                    "Approval id must be unique within its file.",
                )
            )
        seen_ids.add(key)
    seen_bindings: set[tuple[str, str]] = set()
    for waiver in waivers:
        key = (str(waiver.target), waiver.point_id)
        if key in seen_bindings:
            findings.append(
                _error(
                    "COV_WAIVER_BINDING_DUPLICATE",
                    f"/files/{waiver.waiver_file}",
                    "Target-and-point approval binding must be project-wide unique.",
                )
            )
        seen_bindings.add(key)
    return tuple(findings)


def _match_campaign(
    waivers: tuple[ApprovedWaiver, ...],
    campaign: CoverageCampaign,
) -> ApprovedWaiverMatch:
    if campaign.normalization.get("status") not in {
        "complete",
        "complete_with_unknown_records",
    }:
        return ApprovedWaiverMatch(waivers=(), findings=())
    matched: list[ApprovedWaiver] = []
    findings: list[CoverageFinding] = []
    applicable = [item for item in waivers if str(item.target) == campaign.target.identity]
    for waiver in applicable:
        pointer = f"/approved_waivers/{waiver.waiver_file}#{waiver.waiver_id}"
        points = [point for point in campaign.points if point.id == waiver.point_id]
        if not points:
            findings.append(
                _error("COV_WAIVER_POINT_STALE", pointer, "Approval no longer matches a point.")
            )
        elif len(points) > 1:
            findings.append(
                _error("COV_WAIVER_MATCH_AMBIGUOUS", pointer, "Approval matches multiple points.")
            )
        elif points[0].identity.location.get("source") != waiver.source:
            findings.append(
                _error(
                    "COV_WAIVER_POINT_SOURCE_MISMATCH",
                    pointer,
                    "Matched point belongs to a different RTL source.",
                )
            )
        elif points[0].disposition.get("kind") != "eligible":
            findings.append(
                _error(
                    "COV_WAIVER_POINT_UNSCORABLE",
                    pointer,
                    "Only V1-scored eligible RTL points can be waived.",
                )
            )
        else:
            matched.append(waiver)
    if findings:
        return ApprovedWaiverMatch(waivers=(), findings=tuple(findings))
    return ApprovedWaiverMatch(waivers=tuple(matched), findings=())


def _load_enabled_set(
    config: CoverageWaiverConfig,
    roots: CoverageRepositoryRoots,
    known_targets: Collection[DurableTargetIdentity],
) -> ApprovedWaiverSet:
    root = getattr(roots, config.anchor)
    directory = root / config.directory
    files, findings = _enumerate_approval_files(directory)
    if findings:
        raise CoverageWaiverValidationError(findings)
    loaded: list[tuple[dict[str, object], list[ApprovedWaiver]]] = []
    parse_findings: list[CoverageFinding] = []
    claims: list[tuple[str, str]] = []
    known = frozenset(str(target) for target in known_targets)
    for path in files:
        result, file_findings, source = _try_load_approval_file(
            path, directory, roots, known, root
        )
        parse_findings.extend(file_findings)
        if source is not None:
            claims.append((source, path.relative_to(directory).as_posix()))
        if result is not None:
            loaded.append(result)
    parse_findings.extend(_duplicate_source_findings(claims))
    if parse_findings:
        raise CoverageWaiverValidationError(tuple(parse_findings))
    duplicate_findings = _duplicate_findings(loaded)
    if duplicate_findings:
        raise CoverageWaiverValidationError(duplicate_findings)
    projections = [item[0] for item in loaded]
    waivers = tuple(
        sorted(
            (waiver for _, file_waivers in loaded for waiver in file_waivers),
            key=lambda item: (str(item.target), item.point_id, item.waiver_id),
        )
    )
    configuration = {"anchor": config.anchor, "directory": config.directory}
    projection = {"schema": _SET_SCHEMA, "config": configuration, "files": projections}
    return ApprovedWaiverSet(
        configuration=_freeze_mapping(configuration),
        digest=_digest(projection),
        waivers=waivers,
    )


def load_approved_waiver_set(
    config: CoverageWaiverConfig | None,
    roots: CoverageRepositoryRoots,
    known_targets: Collection[DurableTargetIdentity],
) -> ApprovedWaiverSet:
    """Load the immutable project-wide approved waiver set."""
    if config is None:
        projection = {
            "schema": _SET_SCHEMA,
            "config": {"enabled": False},
            "files": [],
        }
        return ApprovedWaiverSet(
            configuration=MappingProxyType({"enabled": False}),
            digest=_digest(projection),
            waivers=(),
        )
    findings = _config_findings(config)
    if findings:
        raise CoverageWaiverValidationError(findings)
    root = getattr(roots, config.anchor)
    findings = _directory_findings(root, config.directory)
    if findings:
        raise CoverageWaiverValidationError(findings)
    return _load_enabled_set(config, roots, known_targets)
