"""Declarative configuration and path rules for approved coverage waivers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, cast

from booley.core.boundary import as_str

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
_PROOF_FIELDS = frozenset({"kind", "reference", "sha256"})


@dataclass(frozen=True)
class CoverageWaiverConfig:
    """Explicit repository anchor and safe relative approval directory."""

    anchor: Literal["rtl_repository", "project_data_repository"]
    directory: str


def parse_coverage_waiver_config(config: Mapping[str, object]) -> CoverageWaiverConfig | None:
    """Parse the optional closed ``coverage.waivers`` table."""
    coverage = config.get("coverage", {})
    if not isinstance(coverage, Mapping):
        raise ValueError("coverage must be a table")
    raw = coverage.get("waivers")
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"anchor", "directory"}:
        raise ValueError("coverage.waivers requires anchor and directory")
    anchor = raw.get("anchor")
    directory = raw.get("directory")
    if not isinstance(anchor, str) or not isinstance(directory, str):
        raise ValueError("coverage.waivers requires anchor and directory")
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


def _approval_documents(document: Mapping[str, object]) -> list[object] | None:
    if document.get("schema") != "booley.coverage-waivers/v1":
        return None
    if set(document) != _TOP_LEVEL_FIELDS:
        return None
    if not is_safe_relative_posix(document.get("source")):
        return None
    if _SHA256_RE.fullmatch(str(document.get("source_sha256"))) is None:
        return None
    approvals = document.get("approval")
    if not isinstance(approvals, list) or not approvals:
        return None
    return approvals


def _formal_reference(proof: object) -> tuple[bool, str | None]:
    if not isinstance(proof, Mapping) or set(proof) != _PROOF_FIELDS:
        return False, None
    reference = proof.get("reference")
    if proof.get("kind") != "formal" or not isinstance(reference, str):
        return False, None
    if _SHA256_RE.fullmatch(str(proof.get("sha256"))) is None:
        return False, None
    relative = reference.split("#", 1)[0]
    return (True, relative) if is_safe_relative_posix(relative) else (False, None)


def _approval_proof_reference(record: object) -> tuple[bool, str | None]:
    if not isinstance(record, Mapping):
        return False, None
    if set(record) - _APPROVAL_FIELDS or _REQUIRED_APPROVAL_FIELDS - set(record):
        return False, None
    if any(not isinstance(record.get(field), str) for field in _REQUIRED_APPROVAL_FIELDS):
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
