"""Profile-aware mechanics for Simulation Campaign artifact references."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from booley.flows.sim.campaign_reports import is_report_link
from booley.runtime.regular_file import open_regular_nofollow

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FIELDS = {"path_base", "path", "bytes", "sha256", "kind", "owner"}


class ArtifactReferenceError(ValueError):
    """A typed artifact reference is malformed or fails authentication."""


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """One immutable, typed reference to a digest-bound artifact."""

    path_base: str
    path: str
    bytes: int
    sha256: str
    kind: str
    owner: str


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """One authenticated regular file selected below a named base."""

    path: Path
    raw: bytes


def build_artifact_reference(
    path: Path,
    *,
    base_name: str,
    base: Path,
    kind: str,
    owner: str,
    maximum: int,
) -> ArtifactReference:
    """Build a typed reference after authenticating an already published file."""
    base = base.absolute()
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(base).as_posix()
    except ValueError as exc:
        raise ArtifactReferenceError("artifact is outside its declared base") from exc
    contained = _contained_path(base, relative)
    raw = _read_regular(contained, maximum)
    return _artifact_reference(
        path_base=base_name,
        path=relative,
        bytes_=len(raw),
        sha256=_digest(raw),
        kind=kind,
        owner=owner,
    )


def encode_artifact_reference(reference: ArtifactReference) -> dict[str, object]:
    """Encode one validated artifact reference for JSON persistence."""
    return {
        "path_base": reference.path_base,
        "path": reference.path,
        "bytes": reference.bytes,
        "sha256": reference.sha256,
        "kind": reference.kind,
        "owner": reference.owner,
    }


def resolve_artifact_reference(
    value: object,
    *,
    bases: Mapping[str, Path],
    allowed_bases: Set[str],
    expected_kind: str,
    expected_owner: str,
    maximum: int,
) -> ResolvedArtifact:
    """Resolve and authenticate a reference under one profile's exact bases."""
    reference = _reference(value, allowed_bases)
    if reference.kind != expected_kind or reference.owner != expected_owner:
        raise ArtifactReferenceError("artifact kind or owner disagrees")
    try:
        base = bases[reference.path_base].absolute()
    except KeyError as exc:
        raise ArtifactReferenceError("artifact base is unavailable") from exc
    path = _contained_path(base, reference.path)
    raw = _read_regular(path, maximum)
    if len(raw) != reference.bytes or _digest(raw) != reference.sha256:
        raise ArtifactReferenceError("artifact bytes disagree with reference")
    return ResolvedArtifact(path, raw)


def resolve_report_artifact_reference(
    report_path: Path,
    value: object,
    *,
    expected_kind: str,
    expected_owner: str,
    maximum: int,
    external_origin_target: Path | None = None,
) -> ResolvedArtifact:
    """Resolve a report reference from the location of its containing document."""
    invocation = report_path.absolute().parent
    if report_path.name != "report.json" or not invocation.name.isdecimal():
        raise ArtifactReferenceError("expected a numbered invocation report.json")
    bases = {
        "report_invocation": invocation,
        "reports_root": invocation.parent.parent,
    }
    if external_origin_target is not None:
        bases["external_origin_target"] = external_origin_target
    return resolve_artifact_reference(
        value,
        bases=bases,
        allowed_bases={"report_invocation", "reports_root", "external_origin_target"},
        expected_kind=expected_kind,
        expected_owner=expected_owner,
        maximum=maximum,
    )


def _reference(value: object, allowed_bases: Set[str]) -> ArtifactReference:
    if isinstance(value, ArtifactReference):
        value = encode_artifact_reference(value)
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ArtifactReferenceError("artifact reference fields are invalid")
    reference = _artifact_reference(
        path_base=value["path_base"],
        path=value["path"],
        bytes_=value["bytes"],
        sha256=value["sha256"],
        kind=value["kind"],
        owner=value["owner"],
    )
    if reference.path_base not in allowed_bases:
        raise ArtifactReferenceError("artifact reference base is not allowed")
    return reference


def _artifact_reference(
    *, path_base: object, path: object, bytes_: object, sha256: object, kind: object, owner: object
) -> ArtifactReference:
    base = _nonempty(path_base, "path_base")
    relative = str(path) if isinstance(path, str) else ""
    _relative(relative)
    if type(bytes_) is not int or bytes_ < 1:
        raise ArtifactReferenceError("artifact byte count is invalid")
    if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
        raise ArtifactReferenceError("artifact digest is invalid")
    return ArtifactReference(
        base,
        relative,
        bytes_,
        sha256,
        _nonempty(kind, "kind"),
        _nonempty(owner, "owner"),
    )


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ArtifactReferenceError("artifact path is not normalized")
    return path


def _contained_path(base: Path, relative: str) -> Path:
    path = base.joinpath(*_relative(relative).parts)
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise ArtifactReferenceError("artifact path escapes its declared base") from exc
    current = path
    while True:
        if is_report_link(current):
            raise ArtifactReferenceError("artifact path contains a link")
        if current == base:
            break
        if current == current.parent:
            raise ArtifactReferenceError("artifact path does not reach its declared base")
        current = current.parent
    return path


def _read_regular(path: Path, maximum: int) -> bytes:
    try:
        descriptor = open_regular_nofollow(path)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError as exc:
        raise ArtifactReferenceError(f"artifact is not a regular file: {path}") from exc
    if len(raw) > maximum:
        raise ArtifactReferenceError("artifact exceeds size limit")
    return raw


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ArtifactReferenceError(f"artifact {field} is invalid")
    return value


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = [
    "ArtifactReference",
    "ArtifactReferenceError",
    "ResolvedArtifact",
    "build_artifact_reference",
    "encode_artifact_reference",
    "resolve_artifact_reference",
    "resolve_report_artifact_reference",
]
