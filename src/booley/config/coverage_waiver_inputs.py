"""Declarative configuration and path rules for approved coverage waivers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, cast

from booley.core.boundary import BoundaryError, as_str, require_dict, require_str

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
APPROVAL_DOCUMENT_FIELDS = frozenset({"schema", "source", "source_sha256", "approval"})
APPROVAL_RECORD_FIELDS = frozenset(
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
REQUIRED_APPROVAL_RECORD_FIELDS = APPROVAL_RECORD_FIELDS - {"proof"}
PROOF_FIELDS = frozenset({"kind", "reference", "sha256"})
_ANCHORS = frozenset({"rtl_repository", "project_data_repository"})


@dataclass(frozen=True)
class CoverageWaiverConfig:
    """Explicit repository anchor and safe relative approval directory."""

    anchor: Literal["rtl_repository", "project_data_repository"]
    directory: str


def parse_coverage_waiver_config(config: Mapping[str, object]) -> CoverageWaiverConfig | None:
    """Parse the optional closed ``coverage.waivers`` table."""
    coverage = require_dict(config.get("coverage", {}), field="coverage")
    raw = coverage.get("waivers")
    if raw is None:
        return None
    try:
        waiver_config = require_dict(raw, field="coverage.waivers")
        anchor = require_str(waiver_config, "anchor")
        directory = require_str(waiver_config, "directory")
    except BoundaryError as exc:
        raise ValueError("coverage.waivers requires anchor and directory") from exc
    if set(waiver_config) != {"anchor", "directory"}:
        raise ValueError("coverage.waivers requires anchor and directory")
    if anchor not in _ANCHORS:
        raise ValueError("coverage.waivers anchor is invalid")
    if not is_safe_relative_posix(directory):
        raise ValueError("coverage.waivers directory must be a safe relative POSIX path")
    return CoverageWaiverConfig(
        anchor=cast(Literal["rtl_repository", "project_data_repository"], anchor),
        directory=directory,
    )


def is_safe_relative_posix(value: object) -> bool:
    """Return whether *value* is one normalized relative POSIX identity."""
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


def is_sha256(value: object) -> bool:
    """Return whether *value* is one lowercase SHA-256 identity."""
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def approval_record_has_required_strings(record: Mapping[str, object]) -> bool:
    """Return whether all required approval fields are non-empty strings."""
    return all(
        isinstance(record.get(field), str) and bool(record[field])
        for field in REQUIRED_APPROVAL_RECORD_FIELDS
    )


def _approval_documents(document: Mapping[str, object]) -> list[object] | None:
    if document.get("schema") != "booley.coverage-waivers/v1":
        return None
    if set(document) != APPROVAL_DOCUMENT_FIELDS:
        return None
    if not is_safe_relative_posix(document.get("source")):
        return None
    if not is_sha256(document.get("source_sha256")):
        return None
    approvals = document.get("approval")
    if not isinstance(approvals, list) or not approvals:
        return None
    return approvals


def _formal_reference(proof: object) -> tuple[bool, str | None]:
    if not isinstance(proof, Mapping) or set(proof) != PROOF_FIELDS:
        return False, None
    reference = proof.get("reference")
    if proof.get("kind") != "formal" or not isinstance(reference, str):
        return False, None
    if not is_sha256(proof.get("sha256")):
        return False, None
    relative = reference.split("#", 1)[0]
    return (True, relative) if is_safe_relative_posix(relative) else (False, None)


def _approval_proof_reference(record: object) -> tuple[bool, str | None]:
    if not isinstance(record, Mapping):
        return False, None
    if set(record) - APPROVAL_RECORD_FIELDS or REQUIRED_APPROVAL_RECORD_FIELDS - set(record):
        return False, None
    if not approval_record_has_required_strings(record):
        return False, None
    reason = record.get("reason")
    proof = record.get("proof")
    if reason == "excluded":
        return proof is None, None
    if reason != "unreachable":
        return False, None
    return _formal_reference(proof)


def formal_proof_references(document: Mapping[str, object]) -> tuple[str, ...]:
    """Extract safe formal-proof file identities from one shaped approval document."""
    approvals = _approval_documents(document)
    if approvals is None:
        return ()
    references: set[str] = set()
    for record in approvals:
        valid, reference = _approval_proof_reference(record)
        if not valid:
            return ()
        if reference is not None:
            references.add(reference)
    return tuple(sorted(references))
