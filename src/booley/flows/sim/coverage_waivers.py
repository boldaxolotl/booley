"""Transactional loading of project-wide approved coverage waivers."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from booley.core.boundary import BoundaryError, as_str, require_dict, require_list, require_str
from booley.flows.sim.coverage_campaign import (
    CoverageFinding,
    DurableTargetIdentity,
    FrozenJson,
    decode_coverage_point_id,
)
from booley.flows.sim.coverage_waiver_files import (
    SecureFile,
    SecureFileScan,
    SecurePathError,
    SecurePathProblem,
    SecureTree,
)
from booley.flows.sim.coverage_waiver_matching import ApprovedWaiver, ApprovedWaiverSet
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
    text = as_str(value)
    if not text or "\\" in text or "\x00" in text:
        return False
    if unicodedata.normalize("NFC", text) != text:
        return False
    path = PurePosixPath(text)
    return (
        not path.is_absolute()
        and _WINDOWS_DRIVE_RE.match(text) is None
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == text
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


def _configured_directory_finding(error: SecurePathError) -> CoverageFinding:
    codes = {
        "missing": "COV_WAIVER_DIRECTORY_MISSING",
        "symlink": "COV_WAIVER_DIRECTORY_SYMLINK",
        "invalid": "COV_WAIVER_DIRECTORY_INVALID",
        "unreadable": "COV_WAIVER_DIRECTORY_UNREADABLE",
    }
    messages = {
        "missing": "Configured approved-waiver directory does not exist.",
        "symlink": "No configured directory component may be a symlink.",
        "invalid": "Configured approved-waiver path must be a directory.",
        "unreadable": "Configured approved-waiver directory is not readable.",
    }
    return _error(codes[error.kind], "/directory", messages[error.kind])


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


def _read_evidence(
    tree: SecureTree,
    relative: str,
    pointer: str,
    *,
    label: str,
) -> tuple[bytes | None, CoverageFinding | None]:
    try:
        return tree.read_file(relative), None
    except SecurePathError as error:
        code = f"COV_WAIVER_{label}_{error.kind.upper()}"
        noun = "RTL source" if label == "SOURCE" else "Approved proof artifact"
        return None, _error(code, pointer, f"{noun} is {error.kind}.")


def _scan_problem_finding(problem: SecurePathProblem) -> CoverageFinding:
    pointer = f"/files/{problem.relative_path}"
    if problem.kind == "symlink":
        code = (
            "COV_WAIVER_DIRECTORY_SYMLINK"
            if problem.entry_kind == "directory"
            else "COV_WAIVER_FILE_SYMLINK"
        )
        return _error(code, pointer, "Symlinks are forbidden.")
    if problem.entry_kind == "directory":
        code = "COV_WAIVER_DIRECTORY_UNREADABLE"
        return _error(code, pointer, "Approval directory is not safely readable.")
    code = "COV_WAIVER_FILE_INVALID" if problem.kind == "invalid" else "COV_WAIVER_FILE_UNREADABLE"
    return _error(code, pointer, "Approval file is not a safely readable regular file.")


def _point_source(point_id: object) -> str | None:
    identity = decode_coverage_point_id(point_id)
    if identity is None:
        return None
    location = require_dict(identity.get("location"), field="location")
    return require_str(location, "source")


def _timestamp_is_canonical(value: object) -> bool:
    text = as_str(value)
    if text is None:
        return False
    try:
        parsed = parse_timestamp(text)
    except ValueError:
        return False
    return rfc3339_from_epoch(parsed.timestamp()) == text


def _proof_findings(proof: object, pointer: str) -> list[CoverageFinding]:
    try:
        document = require_dict(proof, field="proof")
        kind = require_str(document, "kind")
        reference = require_str(document, "reference")
        fingerprint = require_str(document, "sha256")
    except BoundaryError:
        return [_error("COV_WAIVER_PROOF_INVALID", pointer, "Proof must be a table.")]
    if set(document) != {"kind", "reference", "sha256"}:
        return [_error("COV_WAIVER_PROOF_INVALID", pointer, "Proof fields are closed.")]
    valid = kind == "formal" and bool(reference) and _SHA256_RE.fullmatch(fingerprint) is not None
    return [] if valid else [_error("COV_WAIVER_PROOF_INVALID", pointer, "Invalid proof.")]


def _proof_evidence_finding(
    tree: SecureTree,
    record: Mapping[str, object],
    pointer: str,
) -> CoverageFinding | None:
    proof = record.get("proof")
    if record.get("reason") != "unreachable" or _proof_findings(proof, pointer):
        return None
    document = require_dict(proof, field="proof")
    reference = require_str(document, "reference")
    relative = reference.split("#", 1)[0]
    if not _safe_relative_posix(relative):
        return _error("COV_WAIVER_PROOF_INVALID", pointer, "Unsafe proof reference.")
    raw, finding = _read_evidence(tree, relative, pointer, label="PROOF")
    if finding is not None:
        return finding
    assert raw is not None
    if document["sha256"] != _fingerprint(raw):
        return _error("COV_WAIVER_PROOF_STALE", f"{pointer}/sha256", "Proof bytes changed.")
    return None


def _record_shape_findings(
    record: Mapping[str, object], pointer: str, known_targets: frozenset[str]
) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    unknown = set(record) - _APPROVAL_FIELDS
    missing = _REQUIRED_APPROVAL_FIELDS - set(record)
    try:
        for key in _REQUIRED_APPROVAL_FIELDS:
            require_str(record, key)
    except BoundaryError:
        empty = True
    else:
        empty = False
    if unknown or missing or empty:
        findings.append(
            _error(
                "COV_WAIVER_RECORD_INCOMPLETE", pointer, "Approval fields are closed and required."
            )
        )
    target = as_str(record.get("target"))
    if target is not None and target not in known_targets:
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
    try:
        document = require_dict(record, field="approval")
    except BoundaryError:
        return [_error("COV_WAIVER_RECORD_INVALID", pointer, "Approval must be a table.")]
    findings = _record_shape_findings(document, pointer, known_targets)
    findings.extend(_record_binding_findings(document, pointer, source))
    if not _timestamp_is_canonical(document.get("approved_at")):
        findings.append(
            _error(
                "COV_WAIVER_APPROVED_AT_INVALID",
                f"{pointer}/approved_at",
                "Canonical UTC RFC 3339 required.",
            )
        )
    findings.extend(_record_reason_findings(document, pointer))
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
    try:
        approvals = require_list(document.get("approval"), field="approval")
    except BoundaryError:
        approvals = []
    if not approvals:
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
    source = require_str(document, "source")
    source_fingerprint = require_str(document, "source_sha256")
    file_fingerprint = _fingerprint(raw)
    raw_approvals = require_list(document.get("approval"), field="approval")
    approvals_as_mappings = [require_dict(item, field="approval") for item in raw_approvals]
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
    raw: bytes, pointer: str
) -> tuple[bytes | None, Mapping[str, object] | None, tuple[CoverageFinding, ...]]:
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, None, (_error("COV_WAIVER_FILE_MALFORMED", pointer, str(exc)),)
    schema = as_str(document.get("schema"))
    if "waiver_candidate" in document or (schema is not None and "candidate" in schema):
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
    rtl_tree: SecureTree,
) -> list[CoverageFinding]:
    source = require_str(document, "source")
    findings: list[CoverageFinding] = []
    if relative != f"{source}.toml":
        findings.append(
            _error(
                "COV_WAIVER_SOURCE_FILE_MISMATCH",
                f"{pointer}/source",
                "Approval file path must mirror its RTL source and append .toml.",
            )
        )
    source_raw, source_finding = _read_evidence(
        rtl_tree, source, f"{pointer}/source", label="SOURCE"
    )
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
    approval_tree: SecureTree,
) -> list[CoverageFinding]:
    source = require_str(document, "source")
    approvals = require_list(document.get("approval"), field="approval")
    findings: list[CoverageFinding] = []
    for index, record in enumerate(approvals):
        record_pointer = f"{pointer}/approval/{index}"
        findings.extend(_record_findings(record, record_pointer, source, known_targets))
        try:
            record_document = require_dict(record, field="approval")
        except BoundaryError:
            continue
        proof = _proof_evidence_finding(approval_tree, record_document, f"{record_pointer}/proof")
        if proof is not None:
            findings.append(proof)
    return findings


def _approval_file_findings(
    document: Mapping[str, object],
    relative: str,
    pointer: str,
    rtl_tree: SecureTree,
    known_targets: frozenset[str],
    approval_tree: SecureTree,
) -> tuple[CoverageFinding, ...]:
    findings = _document_shape_findings(document, pointer)
    if not _safe_relative_posix(relative):
        findings.append(_error("COV_WAIVER_PATH_UNSAFE", pointer, "Unsafe approval-file path."))
    source = as_str(document.get("source"))
    if source is not None and _safe_relative_posix(source):
        findings.extend(_source_file_findings(document, relative, pointer, rtl_tree))
        try:
            require_list(document.get("approval"), field="approval")
        except BoundaryError:
            pass
        else:
            findings.extend(
                _approval_record_findings(document, pointer, known_targets, approval_tree)
            )
    return tuple(findings)


def _try_load_approval_file(
    file: SecureFile,
    rtl_tree: SecureTree,
    known_targets: frozenset[str],
    approval_tree: SecureTree,
) -> tuple[
    tuple[dict[str, object], list[ApprovedWaiver]] | None,
    tuple[CoverageFinding, ...],
    str | None,
]:
    relative = file.relative_path
    pointer = f"/files/{relative}"
    raw, document, read_findings = _read_approval_document(file.raw, pointer)
    if document is None or raw is None:
        return None, read_findings, None
    findings = _approval_file_findings(
        document, relative, pointer, rtl_tree, known_targets, approval_tree
    )
    source = as_str(document.get("source"))
    if findings:
        return None, tuple(findings), source
    assert source is not None
    return _load_approval_file(raw, document, relative), (), source


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


def _load_enabled_set(
    config: CoverageWaiverConfig,
    roots: CoverageRepositoryRoots,
    known_targets: Collection[DurableTargetIdentity],
) -> ApprovedWaiverSet:
    approval_root = getattr(roots, config.anchor)
    try:
        with (
            SecureTree(approval_root) as approval_tree,
            SecureTree(roots.rtl_repository) as rtl_tree,
        ):
            scan = approval_tree.scan_files(config.directory)
            return _load_scanned_set(config, scan, approval_tree, rtl_tree, known_targets)
    except SecurePathError as error:
        raise CoverageWaiverValidationError((_configured_directory_finding(error),)) from error


def _load_scanned_set(
    config: CoverageWaiverConfig,
    scan: SecureFileScan,
    approval_tree: SecureTree,
    rtl_tree: SecureTree,
    known_targets: Collection[DurableTargetIdentity],
) -> ApprovedWaiverSet:
    scan_findings = tuple(_scan_problem_finding(problem) for problem in scan.problems)
    if scan_findings:
        raise CoverageWaiverValidationError(scan_findings)
    loaded: list[tuple[dict[str, object], list[ApprovedWaiver]]] = []
    parse_findings: list[CoverageFinding] = []
    claims: list[tuple[str, str]] = []
    known = frozenset(str(target) for target in known_targets)
    for file in scan.files:
        result, file_findings, source = _try_load_approval_file(
            file, rtl_tree, known, approval_tree
        )
        parse_findings.extend(file_findings)
        if source is not None:
            claims.append((source, file.relative_path))
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
    return _load_enabled_set(config, roots, known_targets)
